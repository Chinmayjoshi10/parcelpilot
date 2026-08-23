/**
 * The conversation.
 *
 * This was a form: one question box, one result panel, and asking again wiped
 * what came before. That is a search box wearing a chat costume, and it made the
 * product feel like a query tool rather than someone you are talking to. The
 * rewrite is a real thread — every turn stays on screen, the composer lives at
 * the bottom, and the answer streams into the turn it belongs to.
 *
 * Two things about the design are deliberate and worth defending:
 *
 * 1. **The reasoning stream lives inside its turn and collapses when done.**
 *    While the agent works, you watch the durable run log being tailed — which
 *    tools ran, what was retrieved, that citations were validated. When the
 *    answer lands it folds into one line you can reopen. Transparency that never
 *    gets out of the way is just noise, and noise is what people learn to
 *    ignore.
 *
 * 2. **A refusal renders as a message, not an error.** It is a turn in the
 *    conversation like any other. Styling refusals as failures teaches a system
 *    to guess, which is the opposite of what this one is for.
 *
 * The thread is durable, not just React state: every turn shares one
 * `conversation_id`, so the runs are linked in the database and an auditor reads
 * the same thread months later.
 */

import {
  forwardRef,
  useCallback,
  useEffect,
  useImperativeHandle,
  useLayoutEffect,
  useRef,
  useState,
} from 'react'
import * as api from '../lib/api'
import {
  Card,
  ErrorNote,
  Pill,
  Spinner,
  StepIcon,
  formatMs,
} from './primitives'

const SUGGESTIONS = {
  'ACCT-001': [
    'Can I cancel ORD-1001 without a cancellation fee? Explain why.',
    'What is my first-response target for a P1 outage?',
    'Our shipment ORD-1002 was already picked up — can we still cancel it?',
  ],
  'ACCT-002': [
    'A pickup for ORD-2002 is late because of carrier fault. Do we owe a credit, and how much?',
    'Why did our 4,200-row CSV upload fail?',
    'What is the cancellation fee on ORD-2001?',
  ],
  default: [
    'What is the standard first-response target for a P1 on Enterprise?',
    'When is a customer eligible for a failed-pickup service credit?',
    'What is the supported bulk upload row limit?',
  ],
}

let turnSeq = 0
const nextTurnId = () => `turn-${++turnSeq}`

export default function ChatPanel({ me, meta, onInspect }) {
  const [turns, setTurns] = useState([])
  const [draft, setDraft] = useState('')
  const [error, setError] = useState(null)
  const [conversationId, setConversationId] = useState(null)
  const [running, setRunning] = useState(false)
  // Bumped whenever a run finishes, so the history panel re-fetches. It used to
  // load once with an empty dependency array: ask ten questions and the panel
  // still listed whatever existed when the tab opened, so your own conversation
  // did not appear in your own history until a reload. The runs were persisted
  // correctly all along -- which is the version of this bug most likely to be
  // read as "it did not save".
  const [historyToken, setHistoryToken] = useState(0)

  const abortRef = useRef(null)
  const threadEnd = useRef(null)
  const composer = useRef(null)

  // Staff can reproduce snapshot-time decisions; the dataset is a snapshot, so
  // "now" would otherwise drift away from the numbers in the documents.
  const [asOf, setAsOf] = useState(
    me?.is_staff ? '2026-08-16T11:00:00+05:30' : '',
  )

  useEffect(() => () => abortRef.current?.(), [])

  // useLayoutEffect, not useEffect: scrolling after paint rather than after the
  // browser has already shown the un-scrolled frame avoids a visible jump on
  // every streamed step.
  useLayoutEffect(() => {
    threadEnd.current?.scrollIntoView({ behavior: 'smooth', block: 'end' })
  }, [turns])

  const patchTurn = useCallback((id, patch) => {
    setTurns((current) =>
      current.map((turn) =>
        turn.id === id
          ? { ...turn, ...(typeof patch === 'function' ? patch(turn) : patch) }
          : turn,
      ),
    )
  }, [])

  const ask = useCallback(
    async (text) => {
      const question = (text ?? draft).trim()
      if (!question || running) return

      abortRef.current?.()
      setError(null)
      setDraft('')
      setRunning(true)

      const answerId = nextTurnId()
      const startedAt = Date.now()
      setTurns((current) => [
        ...current,
        { id: nextTurnId(), role: 'user', text: question },
        { id: answerId, role: 'assistant', steps: [], running: true },
      ])

      try {
        const started = await api.startChat(question, asOf || undefined, conversationId)
        // First message of the thread: adopt the id the server minted. Sending
        // it from here on is what makes this one conversation rather than a
        // series of unrelated runs.
        if (started.conversation_id) setConversationId(started.conversation_id)
        patchTurn(answerId, { runId: started.run_id })

        abortRef.current = api.streamRun(started.run_id, {
          onStep: (step) =>
            patchTurn(answerId, (turn) => ({
              // Guard against a duplicate frame after a reconnect: sequence
              // numbers are database-assigned, so identity is reliable.
              steps: turn.steps.some((s) => s.seq === step.seq)
                ? turn.steps
                : [...turn.steps, step],
            })),
          onDone: async ({ status }) => {
            setRunning(false)
            try {
              // The final payload is read back from the persisted run rather
              // than trusted from the stream frame: one source of truth, and it
              // also carries the retrieval candidates.
              const detail = await api.getRun(started.run_id)
              patchTurn(answerId, {
                running: false,
                elapsedMs: Date.now() - startedAt,
                run: { ...detail.run, status },
                pending: detail.pending_action ?? null,
              })
              setHistoryToken((n) => n + 1)
            } catch (e) {
              patchTurn(answerId, { running: false, error: e.message })
            }
          },
          onError: ({ detail }) => {
            setRunning(false)
            patchTurn(answerId, {
              running: false,
              error: detail || 'the stream ended unexpectedly',
            })
          },
        })
      } catch (e) {
        setRunning(false)
        patchTurn(answerId, { running: false, error: e.message })
      }
    },
    [draft, running, asOf, conversationId, patchTurn],
  )

  const newThread = () => {
    abortRef.current?.()
    setTurns([])
    setConversationId(null)
    setError(null)
    setRunning(false)
    composer.current?.focus()
  }

  const suggestions = SUGGESTIONS[me?.account_id] ?? SUGGESTIONS.default

  return (
    <div className="grid gap-4 lg:grid-cols-[minmax(0,1fr)_340px]">
      {/* A column of fixed height with one scrolling region, so the composer
          stays reachable no matter how long the conversation gets. */}
      <div className="flex h-[calc(100vh-9rem)] min-h-[30rem] flex-col">
        <div className="mb-2 flex items-center justify-between gap-3 px-1">
          <p className="text-[11px] text-muted">
            {me?.account_id
              ? `Scoped to ${me.account_id} — you can only see your own records`
              : 'Tenant-wide visibility'}
          </p>
          <div className="flex items-center gap-3">
            {me?.is_staff && (
              <label className="flex items-center gap-1.5 text-[11px] text-muted">
                as of
                <input
                  value={asOf}
                  onChange={(e) => setAsOf(e.target.value)}
                  placeholder="ISO timestamp"
                  className="tabular w-48 rounded border border-edge bg-raised px-2 py-1 text-[11px]"
                />
              </label>
            )}
            {turns.length > 0 && (
              <button
                onClick={newThread}
                className="rounded-md border border-edge px-2 py-1 text-[11px] text-muted transition hover:bg-raised hover:text-ink"
              >
                New thread
              </button>
            )}
          </div>
        </div>

        <div className="min-h-0 flex-1 overflow-y-auto pr-1">
          {turns.length === 0 ? (
            <Welcome suggestions={suggestions} onPick={ask} />
          ) : (
            <div className="space-y-4 pb-2">
              {turns.map((turn) =>
                turn.role === 'user' ? (
                  <UserTurn key={turn.id} text={turn.text} replayed={turn.replayed} />
                ) : (
                  <AssistantTurn
                    key={turn.id}
                    turn={turn}
                    me={me}
                    onInspect={onInspect}
                    onError={setError}
                    onActionSettled={async () => {
                      patchTurn(turn.id, { pending: null })
                      try {
                        const detail = await api.getRun(turn.runId)
                        patchTurn(turn.id, { pending: detail.pending_action ?? null })
                      } catch {
                        /* the drawer is already dismissed */
                      }
                    }}
                  />
                ),
              )}
            </div>
          )}
          <div ref={threadEnd} />
        </div>

        {error && (
          <div className="mt-2">
            <ErrorNote onDismiss={() => setError(null)}>{error}</ErrorNote>
          </div>
        )}

        <Composer
          ref={composer}
          value={draft}
          onChange={setDraft}
          onSend={() => ask()}
          running={running}
          staff={Boolean(me?.is_staff)}
        />
      </div>

      <div className="space-y-4">
        <RunContext meta={meta} answer={lastRun(turns)} steps={lastSteps(turns)} />
        <RecentRuns
          refreshToken={historyToken}
          // The whole run, not just its answer. `GET /api/chat/{run_id}` already
          // returns `steps` and `pending_action`; this passed only `d.run` and
          // hard-coded `steps: []`. Since Trace renders only when steps exist, a
          // replayed answer showed NO reasoning at all under a card titled
          // "Every answer is replayable" -- and a replayed run that was awaiting
          // approval lost its confirmation drawer, so the approval vanished from
          // the interface while still pending in the ledger.
          onOpen={(detail) =>
            setTurns((current) => [
              ...current,
              {
                id: nextTurnId(),
                role: 'user',
                text: detail.run.query,
                replayed: true,
              },
              {
                id: nextTurnId(),
                role: 'assistant',
                steps: detail.steps ?? [],
                running: false,
                run: detail.run,
                runId: detail.run.run_id,
                pending: detail.pending_action ?? null,
              },
            ])
          }
        />
      </div>
    </div>
  )
}

const lastRun = (turns) =>
  [...turns].reverse().find((t) => t.role === 'assistant' && t.run)?.run ?? null

const lastSteps = (turns) =>
  [...turns].reverse().find((t) => t.role === 'assistant' && t.steps?.length)?.steps ?? []

function Welcome({ suggestions, onPick }) {
  return (
    <div className="flex h-full flex-col items-center justify-center px-6 text-center">
      <p className="text-[15px] text-ink/90">
        Ask about a policy, an order, or an entitlement.
      </p>
      <p className="mt-1.5 max-w-sm text-[12px] leading-relaxed text-muted">
        Every answer quotes the clause it relies on, and declines when the
        evidence is not there.
      </p>
      <div className="mt-6 w-full max-w-md space-y-1.5">
        {suggestions.map((s) => (
          <button
            key={s}
            onClick={() => onPick(s)}
            className="block w-full rounded-lg border border-edge/60 px-3 py-2 text-left text-[13px] text-muted transition hover:border-edge hover:bg-raised hover:text-ink"
          >
            {s}
          </button>
        ))}
      </div>
    </div>
  )
}

function UserTurn({ text, replayed }) {
  return (
    <div className="flex justify-end">
      <div className="max-w-[85%] rounded-2xl rounded-br-md border border-edge bg-raised px-3.5 py-2.5 text-[13.5px] leading-relaxed text-ink">
        {replayed && (
          <span className="mr-1.5 text-[10px] uppercase tracking-widest text-muted">
            replay
          </span>
        )}
        {text}
      </div>
    </div>
  )
}

/**
 * One agent turn: the reasoning trace, then the answer, then — if the agent
 * proposed a state change — the confirmation gate.
 */
function AssistantTurn({ turn, me, onInspect, onError, onActionSettled }) {
  const parsed = turn.run?.answer_json ?? null

  return (
    <div className="space-y-2">
      {(turn.steps?.length > 0 || turn.running) && (
        <Trace steps={turn.steps} running={turn.running} elapsedMs={turn.elapsedMs} />
      )}

      {turn.error && <ErrorNote>{turn.error}</ErrorNote>}

      {parsed && <AnswerView answer={parsed} runId={turn.runId} onInspect={onInspect} />}

      {/* The records the answer is about. Server-authored and uncited on
          purpose: a row IS its source, so quoting it against itself would be
          circular. Before this existed, "show me all open P1 tickets" answered
          with the DEFINITION of a P1 — every sentence correctly cited, and not
          one ticket named — because policy text was the only material the model
          could cite. */}
      {(turn.run?.tables ?? []).map((table, i) => (
        <ResultTable key={i} table={table} />
      ))}

      {/* Something the user asked to be DONE was not done. Rendered as a system
          notice rather than inside the answer, because it is a fact about this
          system and not a claim about the world — there is no clause in the
          corpus to cite for "you are not authorised", so it cannot be a Claim
          at all. Styled unlike the assistant's prose for the same reason: the
          user should be able to see that the machine is telling them this. */}
      {turn.run?.action_notice && (
        <div className="flex items-start gap-2 rounded-lg border border-warn/30 bg-warn/[0.06] px-3 py-2.5">
          <span className="mt-[1px] shrink-0 text-warn">!</span>
          <p className="text-[12.5px] leading-relaxed text-ink/90">
            {turn.run.action_notice}
          </p>
        </div>
      )}

      {turn.pending && (
        <ConfirmationDrawer
          action={turn.pending}
          me={me}
          onSettled={onActionSettled}
          onError={onError}
        />
      )}
    </div>
  )
}

/**
 * The reasoning stream: expanded while working, one collapsed line afterwards.
 *
 * Auto-collapsing is the point. Watching the trace is what makes the system's
 * central claim checkable, but a permanent wall of steps above every answer
 * trains people to scroll past it — and a trace nobody reads verifies nothing.
 */
function Trace({ steps, running, elapsedMs }) {
  const [open, setOpen] = useState(true)

  // Collapse on completion, and only then, so a user who reopened a finished
  // trace is not fighting the component.
  useEffect(() => {
    if (!running) setOpen(false)
  }, [running])

  if (!running && !open) {
    return (
      <button
        onClick={() => setOpen(true)}
        className="flex items-center gap-2 rounded-lg border border-edge/50 px-2.5 py-1.5 text-[11px] text-muted transition hover:bg-raised hover:text-ink"
      >
        <span className="text-verified">✓</span>
        <span>
          {steps.length} step{steps.length === 1 ? '' : 's'}
        </span>
        {elapsedMs != null && <span className="tabular">{formatMs(elapsedMs)}</span>}
        <span className="text-muted">· show reasoning</span>
      </button>
    )
  }

  return (
    <div className="rounded-xl border border-edge/60 bg-surface px-3 py-2.5">
      <div className="mb-1.5 flex items-center justify-between">
        <p className="text-[10px] font-semibold uppercase tracking-widest text-muted">
          Reasoning
        </p>
        {running ? (
          <Spinner label="working" />
        ) : (
          <button
            onClick={() => setOpen(false)}
            className="text-[10px] text-muted transition hover:text-ink"
          >
            hide
          </button>
        )}
      </div>
      <ol className="space-y-0.5">
        {steps.map((step) => (
          <li
            key={step.seq}
            className="flex items-start gap-2 rounded px-0.5 py-0.5 text-[12.5px]"
          >
            <StepIcon kind={step.kind} />
            <span className="min-w-0 flex-1">
              <span className="text-ink/90">{step.label}</span>
              <StepDetail detail={step.detail} kind={step.kind} />
            </span>
            <span className="tabular shrink-0 text-[11px] text-muted">
              {formatMs(step.duration_ms)}
            </span>
          </li>
        ))}
        {running && <PendingStep steps={steps} />}
      </ol>
    </div>
  )
}

/**
 * What the agent is doing right now, with a running clock.
 *
 * A step is only written to the run log once it has finished, with its measured
 * duration — which is right for an audit trail and wrong for a person waiting.
 * The gap is real: synthesis is a single ~4s model call, and during it the UI
 * showed a heading and a spinner and nothing else. "It just says reasoning" is
 * exactly what that looks like from the outside.
 *
 * So this is inferred client-side rather than added to the log: retrieval is the
 * last thing the agent does before it calls the model, so once a `doc_search` or
 * `policy_decide` step has landed and no `synthesize` step has, the model call is
 * in flight. Nothing is invented — the label names the stage we know it is in,
 * and the timer is the honest wait.
 */
function PendingStep({ steps }) {
  const [elapsed, setElapsed] = useState(0)

  useEffect(() => {
    const startedAt = Date.now()
    const timer = setInterval(() => setElapsed(Date.now() - startedAt), 100)
    return () => clearInterval(timer)
  }, [])

  const kinds = steps.map((s) => s.kind)
  const retrieved = kinds.includes('tool_result')
  const synthesised = kinds.includes('synthesize')

  const label = synthesised
    ? 'Validating every citation against its source'
    : retrieved
      ? 'Writing a cited answer from the retrieved clauses'
      : steps.length
        ? 'Gathering records and policy text'
        : 'Planning which sources to check'

  return (
    <li className="flex items-start gap-2 rounded px-0.5 py-0.5 text-[12.5px]">
      {/* Matches the width of StepIcon so the list does not shift when the real
          step arrives and replaces this row. */}
      <span className="mt-[3px] flex h-3 w-3 shrink-0 items-center justify-center">
        <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-active" />
      </span>
      <span className="min-w-0 flex-1 text-muted">{label}…</span>
      <span className="tabular shrink-0 text-[11px] text-faint">
        {(elapsed / 1000).toFixed(1)}s
      </span>
    </li>
  )
}

/**
 * The composer.
 *
 * Enter sends, Shift+Enter adds a line. That is the convention every chat
 * interface uses, and the old Ctrl+Enter binding was the clearest single signal
 * that this was a form: nobody reaches for a modifier to send a message.
 */
const Composer = forwardRef(function Composer(
  { value, onChange, onSend, running, staff },
  ref,
) {
  const area = useRef(null)
  useImperativeHandle(ref, () => ({ focus: () => area.current?.focus() }), [])

  // Grow with the text, to a ceiling. Past that it scrolls, so the thread never
  // gets pushed off screen by a pasted wall of text.
  useLayoutEffect(() => {
    const el = area.current
    if (!el) return
    el.style.height = 'auto'
    el.style.height = `${Math.min(el.scrollHeight, 160)}px`
  }, [value])

  return (
    <div className="mt-3 rounded-2xl border border-edge bg-surface px-3 py-2.5 focus-within:border-active/60">
      <div className="flex items-end gap-2">
        <textarea
          ref={area}
          rows={1}
          value={value}
          onChange={(e) => onChange(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter' && !e.shiftKey) {
              e.preventDefault()
              onSend()
            }
          }}
          // Staff read tenant-wide, so "your account" was the wrong scope for
          // exactly the reader who can ask across all of them.
          placeholder={
            running
              ? 'Working…'
              : staff
                ? 'Ask across every account…'
                : 'Ask anything about your account…'
          }
          className="max-h-40 flex-1 resize-none border-0 bg-transparent p-0 text-[13.5px] leading-relaxed text-ink placeholder-faint focus:outline-none focus:ring-0"
        />
        <button
          onClick={onSend}
          disabled={running || !value.trim()}
          aria-label="Send"
          className="btn-primary shrink-0 rounded-xl px-3 py-1.5 text-xs"
        >
          {running ? '⋯' : '↑'}
        </button>
      </div>
      <p className="mt-1.5 text-[10px] text-faint">
        Enter to send · Shift+Enter for a new line
      </p>
    </div>
  )
})

function StepDetail({ detail, kind }) {
  if (!detail || typeof detail !== 'object') return null

  const bits = []
  if (detail.tools?.length) bits.push(detail.tools.join(' + '))
  if (detail.query) bits.push(`“${String(detail.query).slice(0, 60)}”`)
  if (detail.groundable != null) bits.push(`${detail.groundable} citable`)
  if (detail.conflict) bits.push(`${detail.conflict} superseded`)
  if (detail.verdict) bits.push(`verdict: ${detail.verdict}`)
  if (detail.claims != null) bits.push(`${detail.claims} claims`)
  if (detail.citations_rejected) bits.push(`${detail.citations_rejected} rejected`)
  if (detail.rows != null) bits.push(`${detail.rows} rows`)
  if (detail.winning) bits.push(`${detail.winning} governs`)

  if (!bits.length) return null
  return (
    <span
      className={`ml-2 text-[11px] ${kind === 'conflict' ? 'text-warn/80' : 'text-muted'}`}
    >
      {bits.join(' · ')}
    </span>
  )
}

function AnswerView({ answer, runId, onInspect }) {
  // The answer IS the table below. Rendering an empty "Answer" card above it
  // would read as something having gone wrong.
  if (answer.is_table_only) return null

  if (answer.refusal) {
    return (
      <Card title="Declined" subtitle="An honest refusal, not a failure">
        <div className="rounded-lg border border-warn/30 bg-warn/[0.06] px-3 py-3">
          <div className="mb-1.5 flex items-center gap-2">
            <Pill className="border-warn/40 bg-warn/10 text-warn">
              {String(answer.refusal.reason).replace(/_/g, ' ')}
            </Pill>
          </div>
          <p className="text-[13px] leading-relaxed text-ink/90">
            {answer.refusal.message}
          </p>
        </div>
        <p className="mt-3 text-[11px] leading-relaxed text-muted">
          The system refuses rather than composing an answer it cannot trace to a
          source. Every claim it does make is validated against the document it
          quotes, so a refusal here means the evidence genuinely was not there.
        </p>
        <HandoffButton runId={runId} />
      </Card>
    )
  }

  // Markers are numbered per unique citation, in first-use order, so [1] in the
  // prose and [1] in the list are the same span.
  const ordered = []
  const seen = new Map()
  for (const claim of answer.claims ?? []) {
    for (const citation of claim.citations ?? []) {
      const key = `${citation.chunk_id}:${citation.start ?? 0}`
      if (!seen.has(key)) {
        ordered.push(citation)
        seen.set(key, ordered.length)
      }
    }
  }

  return (
    <Card
      title="Answer"
      subtitle={`${answer.claims?.length ?? 0} validated claims · ${ordered.length} sources`}
      right={
        <Pill className="border-verified/40 bg-verified/10 text-verified">
          citations verified
        </Pill>
      }
    >
      <div className="space-y-2 text-[14px] leading-relaxed text-ink/95">
        {(answer.claims ?? []).map((claim, index) => (
          <p key={index}>
            {claim.text}
            {(claim.citations ?? []).map((citation, n) => {
              const key = `${citation.chunk_id}:${citation.start ?? 0}`
              return (
                <button
                  key={n}
                  className="cite"
                  onClick={() => onInspect?.({ runId, citation, index: seen.get(key) })}
                  title="Show the exact source paragraph"
                >
                  {seen.get(key)}
                </button>
              )
            })}
          </p>
        ))}
      </div>

      {answer.conflicts?.length > 0 && (
        <div className="mt-4 space-y-1.5 rounded-lg border border-warn/25 bg-warn/[0.05] px-3 py-2.5">
          <p className="text-[10px] font-semibold uppercase tracking-widest text-warn">
            Conflicts resolved
          </p>
          {answer.conflicts.map((conflict, index) => (
            <p key={index} className="text-[12px] leading-relaxed text-ink/80">
              {conflict.explanation}
            </p>
          ))}
        </div>
      )}

      <div className="mt-4 space-y-1 border-t border-edge pt-3">
        <p className="text-[10px] font-semibold uppercase tracking-widest text-muted">
          Sources
        </p>
        {ordered.map((citation, index) => (
          <button
            key={index}
            onClick={() => onInspect?.({ runId, citation, index: index + 1 })}
            className="flex w-full items-start gap-2 rounded-md px-1 py-1 text-left text-[12px] text-muted transition hover:bg-raised hover:text-ink"
          >
            <span className="cite shrink-0">{index + 1}</span>
            <span className="line-clamp-2 italic">
              “{String(citation.quote).replace(/\s+/g, ' ').slice(0, 160)}”
            </span>
          </button>
        ))}
      </div>
    </Card>
  )
}

function RunContext({ meta, answer, steps }) {
  const tokens =
    (answer?.prompt_tokens ?? 0) + (answer?.completion_tokens ?? 0) || null

  return (
    <Card title="Run context">
      <dl className="space-y-2 text-[12px]">
        <Row label="provider" value={meta?.provider} />
        <Row label="routing" value={meta?.routing_model} />
        <Row label="synthesis" value={meta?.synthesis_model} />
        <Row
          label="retrieval"
          value={meta?.retrieval_mode}
          tone={meta?.retrieval_mode === 'lexical_only' ? 'text-warn' : undefined}
        />
        <Row label="index" value={meta?.active_index?.index_version_id} />
        <Row label="chunks" value={meta?.active_index?.chunk_count} />
        {answer?.index_version_id && (
          <Row label="answered by index" value={answer.index_version_id} />
        )}
        {tokens && <Row label="tokens" value={tokens} />}
        {steps.length > 0 && <Row label="steps" value={steps.length} />}
      </dl>
      {meta?.retrieval_mode === 'lexical_only' && (
        <p className="mt-3 text-[11px] leading-relaxed text-warn/80">
          Dense retrieval is unavailable, so search is lexical-only. Answers are
          still cited and validated, but recall on paraphrased questions is
          reduced.
        </p>
      )}
    </Card>
  )
}

function Row({ label, value, tone = 'text-ink' }) {
  if (value == null || value === '') return null
  return (
    <div className="flex items-baseline justify-between gap-3">
      <dt className="text-muted">{label}</dt>
      <dd className={`tabular text-right ${tone}`}>{String(value)}</dd>
    </div>
  )
}

function RecentRuns({ onOpen, refreshToken }) {
  const [runs, setRuns] = useState([])

  useEffect(() => {
    api.listRuns().then(setRuns).catch(() => setRuns([]))
  }, [refreshToken])

  if (!runs.length) return null

  return (
    <Card title="Recent runs" subtitle="Every answer is replayable">
      <ul className="space-y-1">
        {runs.slice(0, 8).map((run) => (
          <li key={run.run_id}>
            <button
              onClick={() =>
                api.getRun(run.run_id).then(onOpen).catch(() => {})
              }
              className="w-full rounded-md px-1.5 py-1.5 text-left transition hover:bg-raised"
            >
              <span className="line-clamp-1 text-[12px] text-ink/85">{run.query}</span>
              <span className="mt-0.5 flex items-center gap-2 text-[10px] text-muted">
                {run.refusal_reason ? (
                  <span className="text-warn/80">declined</span>
                ) : (
                  <span className="text-verified/80">answered</span>
                )}
                <span className="tabular">{run.account_id ?? 'tenant-wide'}</span>
              </span>
            </button>
          </li>
        ))}
      </ul>
    </Card>
  )
}


/**
 * The confirmation gate, in the chat.
 *
 * This is the requirement's own example: the agent prepared an escalation and
 * is asking before creating it. What the user reads is the `summary` the agent
 * wrote plus the clauses that justify it -- not the payload, which the client
 * never receives. Approval sends only the id.
 */
function ConfirmationDrawer({ action, me, onSettled, onError }) {
  const [busy, setBusy] = useState(false)

  const settle = async (approve) => {
    setBusy(true)
    try {
      if (approve) await api.confirmAction(action.action_id)
      else await api.rejectAction(action.action_id, 'declined in chat')
      onSettled?.()
    } catch (e) {
      onError?.(e.message)
    } finally {
      setBusy(false)
    }
  }

  const expires = new Date(action.expires_at)

  return (
    <Card
      title="Confirmation required"
      subtitle="Nothing has happened yet"
      right={
        <Pill className="border-warn/40 bg-warn/10 text-warn">
          {action.action_type.replace(/_/g, ' ')}
        </Pill>
      }
    >
      <p className="text-[13px] leading-relaxed text-ink/90">{action.summary}</p>

      {action.justification?.length > 0 && (
        <div className="mt-3 space-y-1 rounded-lg border border-verified/20 bg-verified/[0.04] px-3 py-2">
          <p className="text-[10px] font-semibold uppercase tracking-widest text-verified">
            Justified by
          </p>
          {action.justification.map((citation, index) => (
            <p key={index} className="text-[11px] italic leading-relaxed text-ink/75">
              “{String(citation.quote ?? '').replace(/\s+/g, ' ').slice(0, 180)}”
            </p>
          ))}
        </div>
      )}

      <p className="tabular mt-2 text-[10px] text-muted">
        expires {expires.toLocaleTimeString()} · account {action.account_id}
      </p>

      {me?.may_execute_actions ? (
        <div className="mt-3 flex gap-2">
          <button
            onClick={() => settle(true)}
            disabled={busy}
            className="btn-primary flex-1 py-2 text-xs"
          >
            {busy ? 'Working…' : 'Confirm & execute'}
          </button>
          <button
            onClick={() => settle(false)}
            disabled={busy}
            className="btn-danger py-2 text-xs"
          >
            Decline
          </button>
        </div>
      ) : (
        <p className="mt-3 rounded-lg border border-warn/30 bg-warn/[0.06] px-3 py-2 text-[11px] leading-relaxed text-warn">
          This is prepared and waiting. Only an operations admin may approve it,
          so it has been queued for the support team rather than executed.
        </p>
      )}

      <p className="mt-3 border-t border-edge pt-2.5 text-[11px] leading-relaxed text-muted">
        Approving sends only this action's id. The effect was frozen server-side
        when it was prepared, so nothing in this interface can change what runs —
        and a second approval reports the existing outcome instead of running
        twice.
      </p>
    </Card>
  )
}

/**
 * Accept the escalation the refusal just offered.
 *
 * Every refusal in this product ends with "I can pass this to a human support
 * agent with everything gathered so far" — and there was no control that did it.
 * This system refuses often, by design, so a dead end at every refusal quietly
 * undercuts the behaviour it is proudest of. An offer you cannot accept reads as
 * a brush-off.
 *
 * Deliberately one button and no form. Someone who has just been told "I could
 * not answer that" should not then be asked to fill anything in: the question,
 * the reason and the whole retrieval trace are already on the run, and the
 * server attaches them.
 */
function HandoffButton({ runId }) {
  const [state, setState] = useState('idle')
  const [error, setError] = useState(null)

  if (!runId) return null

  if (state === 'sent') {
    return (
      <div className="mt-3 flex items-start gap-2 rounded-lg border border-verified/30 bg-verified/[0.06] px-3 py-2.5">
        <span className="mt-[1px] text-verified">✓</span>
        <p className="text-[12.5px] leading-relaxed text-ink/90">
          Sent to a support agent, along with your question and everything the
          assistant looked at. Nothing was answered for you.
        </p>
      </div>
    )
  }

  return (
    <div className="mt-3">
      <button
        onClick={async () => {
          setState('sending')
          setError(null)
          try {
            await api.requestHandoff(runId)
            setState('sent')
          } catch (e) {
            setState('idle')
            setError(e.message)
          }
        }}
        disabled={state === 'sending'}
        className="btn-primary w-full py-2 text-xs"
      >
        {state === 'sending' ? 'Sending…' : 'Send this to a person'}
      </button>
      {error && (
        <p className="mt-1.5 text-[11px] text-breach/90">
          Could not hand this over: {error}
        </p>
      )}
    </div>
  )
}

/**
 * A result table: the records an answer is about.
 *
 * Deliberately styled as data rather than as prose, and carrying no citation
 * chips. That absence is meaningful — everything the assistant *says* is cited,
 * and everything here came straight from the database, so a reader can tell the
 * two apart at a glance.
 */
function ResultTable({ table }) {
  if (!table?.rows?.length) return null

  return (
    <div className="overflow-hidden rounded-2xl border border-edge bg-surface shadow-card">
      <div className="flex items-baseline justify-between gap-3 border-b border-edge px-4 py-3">
        <h3 className="text-[13px] font-semibold text-ink">{table.title}</h3>
        <span className="tabular shrink-0 text-[11px] text-faint">
          {table.rows.length} {table.rows.length === 1 ? 'row' : 'rows'}
        </span>
      </div>

      {/* Its own scroll container, so a wide table never makes the page scroll
          sideways. */}
      <div className="overflow-x-auto">
        <table className="w-full min-w-[560px] border-collapse text-[13px]">
          <thead>
            <tr>
              {table.columns.map((col) => (
                <th
                  key={col}
                  className="whitespace-nowrap border-b border-edge px-4 py-2 text-left text-[10px]
                             font-semibold uppercase tracking-widest text-faint"
                >
                  {col}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {table.rows.map((row, r) => (
              <tr key={r} className="hover:bg-raised">
                {row.map((cell, cIdx) => (
                  <td
                    key={cIdx}
                    className={`border-b border-edge/60 px-4 py-2 align-top ${
                      // First column is the identifier: monospace so a column of
                      // ids reads as a column. Later ones hold figures.
                      cIdx === 0
                        ? 'tabular font-medium text-ink whitespace-nowrap'
                        : /^[+\d]/.test(String(cell))
                          ? 'tabular text-ink/90 whitespace-nowrap'
                          : 'text-muted'
                    }`}
                  >
                    {cell}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {table.note && (
        <p className="border-t border-edge px-4 py-2.5 text-[11px] leading-relaxed text-faint">
          {table.note}
        </p>
      )}
    </div>
  )
}
