/**
 * Internal operations console: the proactive half.
 *
 * Two panels that belong together. The dashboard surfaces what operations
 * should look at before a customer asks; the approval queue is where anything
 * with a real-world effect passes through a human.
 *
 * Every dashboard row carries the clause that defines the threshold it
 * breached, because a breach warning without a citation is an opinion. And
 * every approval sends only an `action_id` -- the effect was frozen server-side
 * when it was prepared, so this UI cannot alter what executes even if it wanted
 * to.
 */

import { useCallback, useEffect, useState } from 'react'
import * as api from '../lib/api'
import {
  Card,
  Empty,
  ErrorNote,
  Pill,
  SeverityBadge,
  Spinner,
  Stat,
} from './primitives'

const SNAPSHOT = '2026-08-16T11:00:00+05:30'

export default function OpsConsole({ me, onInspectClause }) {
  const [board, setBoard] = useState(null)
  const [summary, setSummary] = useState(null)
  const [actions, setActions] = useState([])
  const [effects, setEffects] = useState(null)
  const [error, setError] = useState(null)
  const [busy, setBusy] = useState(false)
  const [asOf, setAsOf] = useState(SNAPSHOT)

  const refresh = useCallback(async () => {
    setBusy(true)
    setError(null)
    try {
      const [b, s, a, e] = await Promise.all([
        api.getDashboard(asOf || undefined),
        api.getSummary().catch(() => null),
        api.listActions().catch(() => []),
        api.getEffects().catch(() => null),
      ])
      setBoard(b)
      setSummary(s)
      setActions(a)
      setEffects(e)
    } catch (err) {
      setError(err.message)
    } finally {
      setBusy(false)
    }
  }, [asOf])

  useEffect(() => {
    refresh()
  }, [refresh])

  const prepare = async (issue) => {
    const suggestion = issue.suggested_action
    if (!suggestion) return
    setError(null)
    try {
      // A dashboard row proposes; it does not act. This queues an approval.
      // No run_id: the detector found this deterministically, with no
      // conversation behind it, so the ledger records origin='operator'.
      await api.prepareAction(
        suggestion.action_type,
        issue.account_id,
        suggestion.payload,
        suggestion.summary,
      )
      await refresh()
    } catch (err) {
      setError(err.message)
    }
  }

  const settle = async (actionId, approve) => {
    setError(null)
    try {
      if (approve) await api.confirmAction(actionId)
      else await api.rejectAction(actionId, 'declined from the operations console')
      await refresh()
    } catch (err) {
      setError(err.message)
    }
  }

  const counts = board?.counts ?? {}

  return (
    <div className="space-y-4">
      <div className="grid grid-cols-2 gap-3 md:grid-cols-5">
        <Stat
          label="P1 issues"
          value={counts.P1 ?? 0}
          tone={counts.P1 ? 'text-breach' : 'text-verified'}
        />
        <Stat label="SLA breaches" value={counts.sla_breach ?? 0} tone="text-breach" />
        <Stat
          label="Credits owed"
          value={counts.credit_eligible ?? 0}
          tone={counts.credit_eligible ? 'text-warn' : 'text-verified'}
        />
        <Stat
          label="Stale answers"
          value={counts.stale_answer ?? 0}
          tone={counts.stale_answer ? 'text-warn' : 'text-verified'}
        />
        <Stat
          label="Awaiting approval"
          value={actions.length}
          tone={actions.length ? 'text-active' : 'text-muted'}
        />
      </div>

      {error && <ErrorNote onDismiss={() => setError(null)}>{error}</ErrorNote>}

      <div className="grid gap-4 lg:grid-cols-[minmax(0,1fr)_380px]">
        <Card
          title="Proactive issue detection"
          subtitle="Deterministic detectors — every row cites the clause it breached"
          right={
            <div className="flex items-center gap-2">
              {busy && <Spinner label="scanning" />}
              <input
                value={asOf}
                onChange={(e) => setAsOf(e.target.value)}
                className="tabular w-52 rounded border border-edge bg-raised px-2 py-1 text-[11px]"
                title="Dataset is a snapshot; evaluate as of this instant"
              />
              <button onClick={refresh} className="btn-ghost px-2 py-1 text-xs">
                Rescan
              </button>
            </div>
          }
        >
          {!board?.issues?.length ? (
            <Empty>No issues detected.</Empty>
          ) : (
            <ul className="space-y-2">
              {board.issues.map((issue, index) => (
                <li
                  key={`${issue.kind}-${issue.subject_id}-${index}`}
                  className="rounded-lg border border-edge bg-raised px-3 py-2.5"
                >
                  <div className="flex items-start justify-between gap-3">
                    <div className="min-w-0">
                      <div className="flex flex-wrap items-center gap-1.5">
                        <SeverityBadge severity={issue.severity} />
                        <Pill className="border-edge text-muted">
                          {issue.kind.replace(/_/g, ' ')}
                        </Pill>
                        {issue.account_id && (
                          <span className="tabular text-[11px] text-muted">
                            {issue.account_id}
                          </span>
                        )}
                      </div>
                      <p className="mt-1.5 text-[13px] font-medium text-ink">
                        {issue.title}
                      </p>
                      <p className="mt-0.5 text-[12px] leading-relaxed text-muted">
                        {issue.detail}
                      </p>

                      {issue.citation && (
                        <button
                          onClick={() => onInspectClause?.(issue.citation)}
                          className="mt-1.5 block text-left text-[11px] italic text-verified/80 hover:text-verified"
                        >
                          “{issue.citation.quote.replace(/\s+/g, ' ').slice(0, 120)}”
                        </button>
                      )}

                      {issue.metrics?.business_hours_approximated && (
                        <p className="mt-1 text-[11px] text-warn/80">
                          Target stated in business hours; approximated as
                          wall-clock.
                        </p>
                      )}
                    </div>

                    {issue.suggested_action && (
                      <button
                        onClick={() => prepare(issue)}
                        className="btn-ghost shrink-0 text-xs"
                        title="Queue this for approval — it does not execute yet"
                      >
                        Propose
                      </button>
                    )}
                  </div>
                </li>
              ))}
            </ul>
          )}
        </Card>

        <div className="space-y-4">
          <Card
            title="Approval queue"
            subtitle="Nothing here has happened yet"
            right={
              actions.length ? (
                <Pill className="border-active/40 bg-active/10 text-active">
                  {actions.length} pending
                </Pill>
              ) : null
            }
          >
            {!actions.length ? (
              <Empty>Nothing awaiting approval.</Empty>
            ) : (
              <ul className="space-y-2">
                {actions.map((action) => (
                  <li
                    key={action.action_id}
                    className="rounded-lg border border-active/25 bg-active/[0.04] px-3 py-2.5"
                  >
                    <div className="flex items-center gap-1.5">
                      <Pill className="border-active/40 bg-active/10 text-active">
                        {action.action_type.replace(/_/g, ' ')}
                      </Pill>
                      <span className="tabular text-[11px] text-muted">
                        {action.account_id}
                      </span>
                    </div>
                    <p className="mt-1.5 text-[12px] leading-relaxed text-ink/90">
                      {action.summary}
                    </p>

                    {action.payload && (
                      <pre className="tabular mt-2 overflow-x-auto rounded border border-edge bg-raised p-2 text-[10px] text-muted">
                        {JSON.stringify(action.payload, null, 2)}
                      </pre>
                    )}

                    <p className="mt-1.5 text-[10px] text-muted">
                      prepared by {action.prepared_by} · expires{' '}
                      {new Date(action.expires_at).toLocaleTimeString()}
                    </p>

                    {me?.may_execute_actions ? (
                      <div className="mt-2 flex gap-2">
                        <button
                          onClick={() => settle(action.action_id, true)}
                          className="btn-primary flex-1 py-1.5 text-xs"
                        >
                          Approve &amp; execute
                        </button>
                        <button
                          onClick={() => settle(action.action_id, false)}
                          className="btn-danger py-1.5 text-xs"
                        >
                          Decline
                        </button>
                      </div>
                    ) : (
                      <p className="mt-2 text-[11px] text-warn/80">
                        Only an operations admin may approve this.
                      </p>
                    )}
                  </li>
                ))}
              </ul>
            )}

            <p className="mt-3 border-t border-edge pt-2.5 text-[11px] leading-relaxed text-muted">
              Approval sends only an action id. The effect was frozen when the
              action was prepared, so it cannot be altered here — and a second
              approval reports the existing outcome instead of executing twice.
            </p>
          </Card>

          {effects?.service_credits?.length > 0 && (
            <Card title="Credits issued" subtitle="Executed, and immutable">
              <ul className="space-y-1.5">
                {effects.service_credits.map((credit) => (
                  <li
                    key={credit.credit_id}
                    className="flex items-baseline justify-between gap-2 text-[12px]"
                  >
                    <span className="text-muted">
                      {credit.account_id}
                      {credit.order_id ? ` · ${credit.order_id}` : ''}
                    </span>
                    <span className="tabular text-verified">
                      {credit.currency} {credit.amount}
                    </span>
                  </li>
                ))}
              </ul>
            </Card>
          )}

          {summary?.active_index && (
            <Card title="Corpus">
              <dl className="space-y-1.5 text-[12px]">
                <Line label="documents" value={summary.active_index.document_count} />
                <Line label="chunks" value={summary.active_index.chunk_count} />
                <Line label="embedded" value={summary.active_index.embedded_count} />
                <Line label="index version" value={summary.active_index.index_version_id} />
                <Line label="open tickets" value={summary.open_tickets} />
                <Line label="pickups overdue" value={summary.pickups_overdue} />
              </dl>
            </Card>
          )}
        </div>
      </div>
    </div>
  )
}

function Line({ label, value }) {
  if (value == null) return null
  return (
    <div className="flex items-baseline justify-between gap-3">
      <dt className="text-muted">{label}</dt>
      <dd className="tabular text-ink">{String(value)}</dd>
    </div>
  )
}
