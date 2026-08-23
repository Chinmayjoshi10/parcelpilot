/**
 * API client.
 *
 * The interesting part is `streamRun`. The browser's built-in `EventSource`
 * cannot set request headers, so the usual workaround is putting the bearer
 * token in the query string -- where it lands in access logs, proxy logs and
 * browser history.
 *
 * So the stream is read with `fetch` plus a ReadableStream and the SSE wire
 * format is parsed by hand. It is about forty lines, and it keeps the token in
 * an Authorization header where it belongs. It also gives us explicit control
 * over reconnection, which matters because the server supports resuming from a
 * step cursor.
 */

const TOKEN_KEY = 'parcelpilot.token'

export function getToken() {
  try {
    return localStorage.getItem(TOKEN_KEY)
  } catch {
    // Private browsing, or site data blocked. Treat as signed out rather than
    // crashing the app on load.
    return null
  }
}

export function setToken(token) {
  try {
    if (token) localStorage.setItem(TOKEN_KEY, token)
    else localStorage.removeItem(TOKEN_KEY)
  } catch {
    /* non-fatal: the session simply will not survive a reload */
  }
}

class ApiError extends Error {
  constructor(status, detail) {
    super(detail || `request failed (${status})`)
    this.status = status
    this.detail = detail
  }
}

async function request(path, { method = 'GET', body, auth = true } = {}) {
  const headers = { 'Content-Type': 'application/json' }
  if (auth) {
    const token = getToken()
    if (token) headers.Authorization = `Bearer ${token}`
  }

  const response = await fetch(path, {
    method,
    headers,
    body: body === undefined ? undefined : JSON.stringify(body),
  })

  if (response.status === 401) {
    // The token is gone or expired. Clear it so the UI falls back to the login
    // screen instead of looping on 401s.
    setToken(null)
    throw new ApiError(401, 'your session has expired')
  }

  if (!response.ok) {
    let detail
    try {
      detail = (await response.json()).detail
    } catch {
      detail = await response.text()
    }
    throw new ApiError(response.status, detail)
  }

  if (response.status === 204) return null
  return response.json()
}

// --- auth -------------------------------------------------------------------

export const listLogins = () => request('/api/auth/logins', { auth: false })

export async function login({ role, account_id, user_id }) {
  const result = await request('/api/auth/login', {
    method: 'POST',
    body: { role, account_id, user_id },
    auth: false,
  })
  setToken(result.access_token)
  return result
}

export const whoami = () => request('/api/auth/me')
export const logout = () => setToken(null)

// --- meta -------------------------------------------------------------------

export const getMeta = () => request('/api/meta', { auth: false })

// --- chat -------------------------------------------------------------------

// `conversationId` threads this question onto an existing conversation. Omitting
// it starts a new one; the server returns the id either way, so the client never
// invents one. A conversation the caller cannot see is a 404, not a silent
// reparent -- see `_resolve_conversation` for why the foreign key is not enough.
export const startChat = (question, asOf, conversationId) =>
  request('/api/chat', {
    method: 'POST',
    body: {
      question,
      as_of: asOf || undefined,
      conversation_id: conversationId || undefined,
    },
  })

// Returns the run, its steps, its retrieval candidates, and -- when the agent
// proposed a state change -- the pending action awaiting confirmation.
export const getRun = (runId) => request(`/api/chat/${runId}`)
export const listRuns = () => request('/api/chat')

// Accept the escalation every refusal offers. Not staff-gated on the server:
// asking a person for help grants the asker nothing, and an offer only staff can
// accept is not an offer.
export const requestHandoff = (runId, note) =>
  request(`/api/chat/${runId}/handoff`, {
    method: 'POST',
    body: { note: note || undefined },
  })
export const getCitation = (runId, chunkId) =>
  request(`/api/chat/${runId}/citation/${chunkId}`)

/**
 * Split an SSE buffer into complete frames, returning [frames, remainder].
 *
 * The separator is the reason this is its own function with its own test. The
 * spec allows a frame boundary to be CRLF CRLF, LF LF, **or** CR CR, and
 * `sse_starlette` — the server here — emits CRLF. An earlier version split on
 * '\n\n' only, which matched nothing at all: no step ever rendered, `done` never
 * fired, and the UI sat on "Reasoning" forever while the finished answer was
 * already in the database. Refreshing and opening the run from history showed
 * it, which is what made the bug look like a rendering problem rather than a
 * parsing one.
 *
 * EventSource would have handled this for free. It is unusable here because it
 * cannot send an Authorization header, so the framing is ours to get right.
 */
export function splitFrames(buffer) {
  const frames = []
  let start = 0

  for (let i = 0; i < buffer.length; i += 1) {
    // Longest separator first: '\r\n\r\n' also matches the '\r\r' test at the
    // same index, and taking the shorter one would leave a stray '\n' that
    // silently becomes an empty first line of the next frame.
    const width =
      buffer.startsWith('\r\n\r\n', i) ? 4
      : buffer.startsWith('\n\n', i) || buffer.startsWith('\r\r', i) ? 2
      : 0
    if (!width) continue

    frames.push(buffer.slice(start, i))
    i += width - 1
    start = i + 1
  }

  return [frames, buffer.slice(start)]
}

/**
 * Parse one SSE frame into { event, id, data }, or null if it carries no data.
 *
 * Field values may be preceded by a single optional space, and lines may end in
 * a stray CR when the buffer was split on LF LF. Both are stripped here rather
 * than at the call site.
 */
export function parseFrame(frame) {
  let event = 'message'
  let id = null
  const dataLines = []

  for (const raw of frame.split(/\r\n|\n|\r/)) {
    const colon = raw.indexOf(':')
    // A line with no colon is a bare field name; a leading colon is a comment
    // (the keep-alive ping), and both are ignored.
    if (colon <= 0) continue
    const field = raw.slice(0, colon)
    // Exactly one leading space is part of the framing, not the value.
    const value = raw.slice(colon + 1).replace(/^ /, '')

    if (field === 'event') event = value.trim()
    else if (field === 'id') id = value.trim()
    else if (field === 'data') dataLines.push(value)
  }

  if (!dataLines.length) return null
  try {
    return { event, id, data: JSON.parse(dataLines.join('\n')) }
  } catch {
    return null
  }
}

/**
 * Tail a run's steps.
 *
 * Calls `onStep` for each reasoning step as it is written, `onDone` with the
 * final answer, `onError` on failure. Returns an abort function.
 *
 * `lastEventId` lets a caller resume after a dropped connection: the server
 * replays only steps beyond that sequence number, so a reconnect never repeats
 * or skips work.
 */
export function streamRun(runId, { onStep, onDone, onError, lastEventId } = {}) {
  const controller = new AbortController()

  ;(async () => {
    try {
      const headers = { Accept: 'text/event-stream' }
      const token = getToken()
      if (token) headers.Authorization = `Bearer ${token}`
      if (lastEventId) headers['Last-Event-ID'] = String(lastEventId)

      const response = await fetch(`/api/chat/${runId}/stream`, {
        headers,
        signal: controller.signal,
      })
      if (!response.ok || !response.body) {
        throw new ApiError(response.status, 'could not open the stream')
      }

      const reader = response.body.getReader()
      const decoder = new TextDecoder()
      let buffer = ''

      let finished = false

      while (true) {
        const { done, value } = await reader.read()
        if (done) break
        buffer += decoder.decode(value, { stream: true })

        // Anything after the last separator is an incomplete frame and stays in
        // the buffer until the next chunk completes it.
        const [frames, remainder] = splitFrames(buffer)
        buffer = remainder

        for (const frame of frames) {
          const parsed = parseFrame(frame)
          if (!parsed) continue
          const { event, id, data } = parsed

          if (event === 'step') onStep?.({ ...data, id })
          else if (event === 'done') {
            finished = true
            onDone?.(data)
          } else if (event === 'error' || event === 'timeout') {
            finished = true
            onError?.(data)
          }
        }
      }

      // The socket closed without a terminal frame -- a proxy timing the
      // connection out, or the server going away mid-run. Silence here is what
      // leaves a turn spinning forever, so it is reported rather than ignored.
      if (!finished) {
        onError?.({ detail: 'the stream ended before the run finished' })
      }
    } catch (error) {
      // An abort is the caller navigating away, not a failure.
      if (error.name !== 'AbortError') onError?.({ detail: error.message })
    }
  })()

  return () => controller.abort()
}

// --- dashboard --------------------------------------------------------------

export const getDashboard = (asOf) =>
  request(`/api/dashboard${asOf ? `?as_of=${encodeURIComponent(asOf)}` : ''}`)
export const getSummary = () => request('/api/dashboard/summary')
export const getAccounts = () => request('/api/dashboard/accounts')
export const getSources = () => request('/api/dashboard/sources')

// --- actions ----------------------------------------------------------------

export const listActions = () => request('/api/actions')

// `runId` is omitted for a console-initiated action: it has no run behind it,
// and the server records origin='operator' rather than attributing it to one.
export const prepareAction = (actionType, accountId, payload, summary, runId) =>
  request('/api/actions', {
    method: 'POST',
    body: {
      action_type: actionType,
      account_id: accountId,
      payload,
      summary,
      ...(runId ? { run_id: runId } : {}),
    },
  })

// Note what is NOT sent: the payload. Confirmation carries only the id, so the
// client cannot alter what executes.
export const confirmAction = (actionId) =>
  request(`/api/actions/${actionId}/confirm`, { method: 'POST' })

export const rejectAction = (actionId, reason) =>
  request(`/api/actions/${actionId}/reject`, { method: 'POST', body: { reason } })

export const getEffects = () => request('/api/actions/history/effects')
export const getAudit = () => request('/api/actions/history/audit')

export { ApiError }
