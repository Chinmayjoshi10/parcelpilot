/**
 * Tests for the hand-rolled SSE parser.
 *
 * This file exists because of a total functional failure that every other gate
 * missed. 176 Python tests passed, the golden set passed 22/22, the frontend
 * built cleanly, and the product was unusable: `sse_starlette` separates frames
 * with CRLF, the parser split on '\n\n', so not a single frame was ever parsed.
 * No step rendered, `done` never fired, and every question sat on "Reasoning"
 * forever — while the finished answer was already committed to the database.
 *
 * The lesson worth keeping: the backend was correct and the tests that covered
 * it were correct. The defect lived in a byte-level contract between two
 * components, which is precisely the seam that per-component tests cannot see.
 * EventSource would have handled framing for free; it is unusable here because
 * it cannot send an Authorization header, and taking that framing in-house made
 * it ours to test.
 */

import { describe, expect, it } from 'vitest'
import { parseFrame, splitFrames } from './api'

const CRLF = '\r\n'

describe('splitFrames', () => {
  it('splits on CRLF CRLF, which is what the server actually sends', () => {
    const buffer =
      `id: 1${CRLF}event: step${CRLF}data: {"seq":1}${CRLF}${CRLF}` +
      `id: 2${CRLF}event: done${CRLF}data: {"status":"completed"}${CRLF}${CRLF}`

    const [frames, remainder] = splitFrames(buffer)

    expect(frames).toHaveLength(2)
    expect(remainder).toBe('')
  })

  it('splits on LF LF, the separator the spec also allows', () => {
    const [frames] = splitFrames('event: step\ndata: {"a":1}\n\nevent: done\ndata: {}\n\n')
    expect(frames).toHaveLength(2)
  })

  it('splits on CR CR', () => {
    const [frames] = splitFrames('data: {"a":1}\r\rdata: {"b":2}\r\r')
    expect(frames).toHaveLength(2)
  })

  it('prefers the longest separator so no stray newline leaks into the next frame', () => {
    // '\r\n\r\n' also matches '\r\r' is false, but it DOES contain '\n\n' at
    // offset 1. Consuming two bytes there would leave '\r' prefixing the next
    // frame, which then parses as an empty leading line.
    const [frames] = splitFrames(`data: {"a":1}${CRLF}${CRLF}data: {"b":2}${CRLF}${CRLF}`)
    expect(frames).toEqual(['data: {"a":1}', 'data: {"b":2}'])
  })

  it('keeps an incomplete trailing frame in the remainder', () => {
    const [frames, remainder] = splitFrames(
      `data: {"a":1}${CRLF}${CRLF}event: step${CRLF}data: {"par`,
    )
    expect(frames).toEqual(['data: {"a":1}'])
    expect(remainder).toBe(`event: step${CRLF}data: {"par`)
  })

  it('reassembles a frame delivered across two chunks', () => {
    // The real failure mode of a naive parser: a payload arriving split at an
    // arbitrary byte because that is how TCP works.
    const whole = `event: step${CRLF}data: {"seq":7,"kind":"validate"}${CRLF}${CRLF}`
    const cut = 22

    let buffer = whole.slice(0, cut)
    let [frames, remainder] = splitFrames(buffer)
    expect(frames).toEqual([])

    buffer = remainder + whole.slice(cut)
    ;[frames, remainder] = splitFrames(buffer)

    expect(frames).toHaveLength(1)
    expect(parseFrame(frames[0]).data.seq).toBe(7)
    expect(remainder).toBe('')
  })

  it('returns nothing for an empty buffer', () => {
    expect(splitFrames('')).toEqual([[], ''])
  })
})

describe('parseFrame', () => {
  it('reads the event name, id and JSON payload', () => {
    const parsed = parseFrame(`id: 5${CRLF}event: step${CRLF}data: {"seq":5,"kind":"synthesize"}`)

    expect(parsed.event).toBe('step')
    expect(parsed.id).toBe('5')
    expect(parsed.data).toEqual({ seq: 5, kind: 'synthesize' })
  })

  it('strips exactly one leading space from a value, not the whole indent', () => {
    // 'data:  {"a":1}' has a value of ' {"a":1}' per the spec. JSON.parse
    // tolerates that, but a value whose content is significant would not.
    expect(parseFrame('data: {"a":1}').data).toEqual({ a: 1 })
    expect(parseFrame('data:{"a":1}').data).toEqual({ a: 1 })
  })

  it('joins multi-line data with newlines', () => {
    const parsed = parseFrame(`event: step${CRLF}data: {"a":${CRLF}data: 1}`)
    expect(parsed.data).toEqual({ a: 1 })
  })

  it('defaults the event name to message when the frame omits it', () => {
    expect(parseFrame('data: {}').event).toBe('message')
  })

  it('ignores the keep-alive comment rather than treating it as data', () => {
    // sse_starlette sends ':ping' comments to hold the connection open. Parsing
    // one as a frame would push a null step into the trace.
    expect(parseFrame(': ping')).toBeNull()
  })

  it('returns null for a frame with no data field', () => {
    expect(parseFrame(`event: step${CRLF}id: 3`)).toBeNull()
  })

  it('returns null on malformed JSON rather than throwing', () => {
    // A truncated payload must not tear down the whole stream loop.
    expect(parseFrame('event: step\ndata: {"seq":')).toBeNull()
  })

  it('parses a real done frame, the one that was never firing', () => {
    const frame =
      `event: done${CRLF}` +
      `data: {"status": "completed", "answer": {"claims": [{"text": "x"}]}}`

    const parsed = parseFrame(frame)

    expect(parsed.event).toBe('done')
    expect(parsed.data.status).toBe('completed')
    expect(parsed.data.answer.claims).toHaveLength(1)
  })
})

describe('the exact server output that used to parse to nothing', () => {
  it('yields every step plus a terminal done', () => {
    // Captured verbatim from `GET /api/chat/{run_id}/stream`.
    const wire =
      `id: 1${CRLF}event: step${CRLF}data: {"seq": 1, "kind": "decompose", "label": "Decompose", "duration_ms": 0}${CRLF}${CRLF}` +
      `id: 2${CRLF}event: step${CRLF}data: {"seq": 2, "kind": "tool_result", "label": "data_query", "duration_ms": 0}${CRLF}${CRLF}` +
      `id: 3${CRLF}event: step${CRLF}data: {"seq": 3, "kind": "synthesize", "label": "Synthesize (attempt 1)", "duration_ms": 3671}${CRLF}${CRLF}` +
      `event: done${CRLF}data: {"status": "completed", "answer": {"claims": []}}${CRLF}${CRLF}`

    const [frames, remainder] = splitFrames(wire)
    const parsed = frames.map(parseFrame).filter(Boolean)

    expect(remainder).toBe('')
    expect(parsed.filter((p) => p.event === 'step')).toHaveLength(3)
    expect(parsed.at(-1).event).toBe('done')
    expect(parsed.at(-1).data.status).toBe('completed')
  })
})
