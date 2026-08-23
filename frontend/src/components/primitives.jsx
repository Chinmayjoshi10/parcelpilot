/**
 * Shared visual primitives.
 *
 * These encode the trust model in the UI. `AuthorityBadge` and
 * `EligibilityBadge` are not decoration: a reader needs to see at a glance
 * whether an answer rests on a negotiated contract or on a superseded policy,
 * and colour carries that faster than text.
 */

const ELIGIBILITY_STYLE = {
  groundable: 'border-verified/40 bg-verified/10 text-verified',
  // Amber, not red: a superseded source is not an error, it is context. Red
  // would imply something went wrong.
  conflict_only: 'border-warn/40 bg-warn/10 text-warn',
  context_only: 'border-muted/30 bg-muted/10 text-muted',
}

const ELIGIBILITY_LABEL = {
  groundable: 'citable',
  conflict_only: 'superseded',
  context_only: 'context only',
}

const SEVERITY_STYLE = {
  P1: 'border-breach/40 bg-breach/10 text-breach',
  P2: 'border-warn/40 bg-warn/10 text-warn',
  P3: 'border-muted/30 bg-muted/10 text-muted',
}

const KIND_STYLE = {
  decompose: 'text-active',
  tool_result: 'text-active',
  tool_call: 'text-active',
  reason: 'text-muted',
  synthesize: 'text-verified',
  validate: 'text-verified',
  conflict: 'text-warn',
  refuse: 'text-warn',
  error: 'text-breach',
}

export function Pill({ children, className = '' }) {
  return <span className={`pill ${className}`}>{children}</span>
}

export function EligibilityBadge({ eligibility }) {
  return (
    <Pill className={ELIGIBILITY_STYLE[eligibility] ?? ELIGIBILITY_STYLE.context_only}>
      {ELIGIBILITY_LABEL[eligibility] ?? eligibility}
    </Pill>
  )
}

export function AuthorityBadge({ authority, sourceClass }) {
  // A contract outranks general policy; showing the number makes the ordering
  // legible rather than asking the reader to trust a sort order.
  const tone =
    authority >= 100
      ? 'border-verified/40 bg-verified/10 text-verified'
      : authority === 0
        ? 'border-breach/40 bg-breach/10 text-breach'
        : 'border-active/30 bg-active/10 text-active'
  return (
    <Pill className={tone}>
      <span className="tabular">{authority}</span>
      <span className="opacity-60">{(sourceClass || '').replace(/_/g, ' ')}</span>
    </Pill>
  )
}

export function FreshnessBadge({ freshness }) {
  if (!freshness || freshness === 'unknown') return null
  const deprecated = freshness === 'deprecated'
  return (
    <Pill
      className={
        deprecated
          ? 'border-breach/40 bg-breach/10 text-breach'
          : 'border-verified/30 bg-verified/5 text-verified/80'
      }
    >
      {freshness}
    </Pill>
  )
}

export function SeverityBadge({ severity }) {
  return (
    <Pill className={SEVERITY_STYLE[severity] ?? SEVERITY_STYLE.P3}>{severity}</Pill>
  )
}

export function StepIcon({ kind }) {
  const glyph =
    {
      decompose: '◆',
      tool_call: '▸',
      tool_result: '▸',
      reason: '∴',
      synthesize: '✎',
      validate: '✓',
      conflict: '⚠',
      refuse: '⊘',
      error: '✕',
    }[kind] ?? '·'
  return (
    <span className={`w-4 text-center ${KIND_STYLE[kind] ?? 'text-muted'}`}>{glyph}</span>
  )
}

export function Card({ title, subtitle, right, children, className = '' }) {
  return (
    <section className={`glass rounded-xl ${className}`}>
      {(title || right) && (
        <header className="flex items-start justify-between gap-3 border-b border-white/[0.06] px-4 py-3">
          <div>
            {title && (
              <h2 className="text-[11px] font-semibold uppercase tracking-widest text-muted">
                {title}
              </h2>
            )}
            {subtitle && <p className="mt-0.5 text-xs text-muted/70">{subtitle}</p>}
          </div>
          {right}
        </header>
      )}
      <div className="px-4 py-3">{children}</div>
    </section>
  )
}

export function Stat({ label, value, tone = 'text-ink', hint }) {
  return (
    <div className="glass rounded-xl px-4 py-3">
      <p className="text-[10px] font-semibold uppercase tracking-widest text-muted">
        {label}
      </p>
      <p className={`tabular mt-1 text-2xl font-semibold ${tone}`}>{value}</p>
      {hint && <p className="mt-0.5 text-[11px] text-muted/70">{hint}</p>}
    </div>
  )
}

export function Empty({ children }) {
  return (
    <p className="py-8 text-center text-sm text-muted/60">{children}</p>
  )
}

export function Spinner({ label }) {
  return (
    <span className="inline-flex items-center gap-2 text-xs text-muted">
      <span className="working h-1.5 w-1.5 rounded-full bg-active" />
      {label}
    </span>
  )
}

export function ErrorNote({ children, onDismiss }) {
  return (
    <div className="flex items-start justify-between gap-3 rounded-lg border border-breach/40 bg-breach/10 px-3 py-2 text-sm text-breach">
      <span>{children}</span>
      {onDismiss && (
        <button onClick={onDismiss} className="text-breach/60 hover:text-breach">
          ✕
        </button>
      )}
    </div>
  )
}

export function formatMs(ms) {
  if (ms == null) return ''
  return ms < 1000 ? `${ms}ms` : `${(ms / 1000).toFixed(1)}s`
}
