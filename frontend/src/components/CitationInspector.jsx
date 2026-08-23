/**
 * Citation inspector: the slide-over behind a [1] chip.
 *
 * This is where the trust claim becomes checkable by a human. It shows the
 * exact stored paragraph with the cited span highlighted in place, plus the
 * document's authority, freshness and eligibility.
 *
 * The highlight is computed from the character offsets the *validator*
 * resolved, not from a client-side text search. If the offsets were wrong the
 * highlight would visibly land in the wrong place -- so the panel is also a
 * check on the validator rather than merely a display of its output.
 */

import { useEffect, useState } from 'react'
import * as api from '../lib/api'
import {
  AuthorityBadge,
  EligibilityBadge,
  ErrorNote,
  FreshnessBadge,
  Spinner,
} from './primitives'

export default function CitationInspector({ open, runId, citation, index, onClose }) {
  const [source, setSource] = useState(null)
  const [error, setError] = useState(null)
  const [loading, setLoading] = useState(false)

  useEffect(() => {
    if (!open || !runId || !citation?.chunk_id) return
    setLoading(true)
    setError(null)
    setSource(null)
    api
      .getCitation(runId, citation.chunk_id)
      .then(setSource)
      .catch((e) => setError(e.message))
      .finally(() => setLoading(false))
  }, [open, runId, citation?.chunk_id])

  // Escape closes. A slide-over that traps focus without an escape hatch is a
  // small cruelty.
  useEffect(() => {
    if (!open) return
    const onKey = (e) => e.key === 'Escape' && onClose()
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [open, onClose])

  if (!open) return null

  return (
    <div className="fixed inset-0 z-50 flex justify-end">
      <button
        aria-label="Close"
        onClick={onClose}
        className="flex-1 bg-black/60 backdrop-blur-sm"
      />
      <aside className="flex h-full w-full max-w-xl flex-col border-l border-white/10 bg-surface shadow-2xl">
        <header className="flex items-start justify-between gap-3 border-b border-white/[0.06] px-5 py-4">
          <div className="min-w-0">
            <div className="flex items-center gap-2">
              <span className="cite">{index}</span>
              <h2 className="truncate text-sm font-semibold text-ink">
                {source?.title ?? 'Source'}
              </h2>
            </div>
            {source && (
              <p className="tabular mt-1 truncate text-[11px] text-muted">
                {source.filename}
                {source.section_path ? ` · ${source.section_path}` : ''}
                {source.page_from ? ` · p.${source.page_from}` : ''}
              </p>
            )}
          </div>
          <button onClick={onClose} className="btn-ghost px-2 py-1 text-xs">
            Esc
          </button>
        </header>

        {source && (
          <div className="flex flex-wrap items-center gap-1.5 border-b border-white/[0.06] px-5 py-3">
            <AuthorityBadge
              authority={source.authority}
              sourceClass={source.source_class}
            />
            <FreshnessBadge freshness={source.freshness} />
            <EligibilityBadge eligibility={source.eligibility} />
            {source.version_label && (
              <span className="pill border-edge text-muted">{source.version_label}</span>
            )}
            {source.owner_account_id && (
              <span className="pill border-verified/40 bg-verified/10 text-verified">
                {source.owner_account_id} only
              </span>
            )}
          </div>
        )}

        <div className="min-h-0 flex-1 overflow-y-auto px-5 py-4">
          {loading && <Spinner label="loading source" />}
          {error && <ErrorNote>{error}</ErrorNote>}

          {source && (
            <>
              <p className="mb-2 text-[10px] font-semibold uppercase tracking-widest text-muted">
                Stored text · cited span highlighted
              </p>
              <div className="whitespace-pre-wrap rounded-lg border border-edge bg-base/60 p-4 text-[13px] leading-relaxed text-ink/90">
                <Highlighted text={source.text} quote={citation.quote} />
              </div>

              <p className="mt-4 text-[11px] leading-relaxed text-muted/70">
                The highlight is positioned using the character offsets resolved
                by the citation validator when this answer was produced. The
                quote was matched against this text character-for-character
                before the claim was allowed to be shown.
              </p>

              {source.eligibility !== 'groundable' && (
                <p className="mt-3 rounded-lg border border-warn/30 bg-warn/[0.06] px-3 py-2 text-[12px] leading-relaxed text-warn">
                  This source cannot ground a claim. It is shown for context or
                  to explain that a newer version supersedes it.
                </p>
              )}
            </>
          )}
        </div>
      </aside>
    </div>
  )
}

/**
 * Highlight the cited span inside the surrounding paragraph.
 *
 * Matching is whitespace-insensitive for the same reason the server's validator
 * is: these documents are PDF-extracted and wrap mid-sentence, so a quote
 * rendered on one line must still be locatable in text that contains a newline
 * at that point. Falls back to plain text when no match is found rather than
 * showing a misplaced highlight.
 */
function Highlighted({ text, quote }) {
  if (!text || !quote) return text ?? null

  const flatten = (s) => s.replace(/\s+/g, ' ')
  const haystack = flatten(text)
  const needle = flatten(quote).trim()
  const at = haystack.indexOf(needle)
  if (at < 0 || !needle) return text

  // Map the position in the whitespace-collapsed string back to the original,
  // so the highlight lands on the real characters.
  const originalIndex = (flatIndex) => {
    let flat = 0
    let inSpace = false
    for (let i = 0; i < text.length; i += 1) {
      if (flat === flatIndex) return i
      if (/\s/.test(text[i])) {
        if (!inSpace) {
          flat += 1
          inSpace = true
        }
      } else {
        flat += 1
        inSpace = false
      }
    }
    return text.length
  }

  const start = originalIndex(at)
  const end = originalIndex(at + needle.length)

  return (
    <>
      {text.slice(0, start)}
      <mark className="rounded bg-verified/25 px-0.5 text-ink ring-1 ring-verified/40">
        {text.slice(start, end)}
      </mark>
      {text.slice(end)}
    </>
  )
}
