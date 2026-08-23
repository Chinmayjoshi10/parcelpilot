/**
 * The corpus, as the signed-in identity can see it.
 *
 * This panel is the tenancy demonstration. Sign in as Northstar and their
 * agreement is listed; LumenWorks' is absent. Switch and it reverses. Nothing
 * here filters by account -- the query is unfiltered and row-level security
 * decides what comes back, which is why the list changes.
 *
 * It also makes the trust model legible: authority ordering, which sources may
 * ground a claim, and which are retained only to explain that a newer version
 * supersedes them.
 */

import { useEffect, useState } from 'react'
import * as api from '../lib/api'
import {
  AuthorityBadge,
  Card,
  EligibilityBadge,
  Empty,
  ErrorNote,
  FreshnessBadge,
  Pill,
  Spinner,
} from './primitives'

export default function SourcesPanel({ me }) {
  const [sources, setSources] = useState([])
  const [error, setError] = useState(null)
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    api
      .getSources()
      .then(setSources)
      .catch((e) => setError(e.message))
      .finally(() => setLoading(false))
  }, [])

  const groundable = sources.filter((s) => s.eligibility === 'groundable')
  const gated = sources.filter((s) => s.eligibility !== 'groundable')

  return (
    <div className="grid gap-4 lg:grid-cols-[minmax(0,1fr)_340px]">
      <div className="space-y-4">
        <Card
          title="Citable sources"
          subtitle="Ordered by authority — a signed agreement outranks general policy"
          right={loading ? <Spinner label="loading" /> : null}
        >
          {error && <ErrorNote>{error}</ErrorNote>}
          {!loading && !groundable.length && <Empty>No sources visible.</Empty>}
          <ul className="space-y-2">
            {groundable.map((source) => (
              <SourceRow key={source.filename} source={source} />
            ))}
          </ul>
        </Card>

        {gated.length > 0 && (
          <Card
            title="Retained but not citable"
            subtitle="Retrievable only to explain that something supersedes them"
          >
            <ul className="space-y-2">
              {gated.map((source) => (
                <SourceRow key={source.filename} source={source} />
              ))}
            </ul>
            <p className="mt-3 border-t border-edge pt-2.5 text-[11px] leading-relaxed text-muted">
              These are not merely ranked lower. Eligibility is a gate, so no
              similarity score can make one of them support a claim — which is
              what stops a superseded policy being quoted as current.
            </p>
          </Card>
        )}
      </div>

      <Card title="Why this list differs by identity">
        <p className="text-[12px] leading-relaxed text-muted">
          You are signed in as{' '}
          <span className="text-ink">{me?.account_id ?? me?.role?.replace(/_/g, ' ')}</span>
          .
        </p>
        <p className="mt-2 text-[12px] leading-relaxed text-muted">
          The query behind this panel has no account filter in it. Every row you
          see was permitted by row-level security in PostgreSQL, using a runtime
          role that is neither a superuser nor a table owner — because Postgres
          exempts both.
        </p>
        <p className="mt-2 text-[12px] leading-relaxed text-muted">
          A customer therefore cannot retrieve another customer's agreement even
          with a query crafted to match its exact wording. There is no code path
          that forgets the filter, because the filter is not in the code.
        </p>
        <div className="mt-3 flex flex-wrap gap-1.5 border-t border-edge pt-3">
          <Pill className="border-verified/40 bg-verified/10 text-verified">
            {groundable.length} citable
          </Pill>
          {gated.length > 0 && (
            <Pill className="border-warn/40 bg-warn/10 text-warn">
              {gated.length} gated
            </Pill>
          )}
          <Pill className="border-edge text-muted">
            {sources.reduce((total, s) => total + (s.chunks ?? 0), 0)} chunks
          </Pill>
        </div>
      </Card>
    </div>
  )
}

function SourceRow({ source }) {
  return (
    <li className="rounded-lg border border-edge bg-raised px-3 py-2.5">
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <p className="truncate text-[13px] font-medium text-ink">{source.title}</p>
          <p className="tabular mt-0.5 truncate text-[11px] text-muted">
            {source.filename} · {source.page_count}p · {source.chunks} chunks
          </p>
        </div>
        <div className="flex shrink-0 flex-wrap items-center justify-end gap-1.5">
          <AuthorityBadge
            authority={source.authority}
            sourceClass={source.source_class}
          />
          <FreshnessBadge freshness={source.freshness} />
          <EligibilityBadge eligibility={source.eligibility} />
        </div>
      </div>
      {(source.owner_account_id || source.policy_family) && (
        <div className="mt-1.5 flex flex-wrap gap-1.5">
          {source.owner_account_id && (
            <Pill className="border-verified/40 bg-verified/10 text-verified">
              governs {source.owner_account_id}
            </Pill>
          )}
          {source.policy_family && (
            <Pill className="border-edge text-muted">
              {source.policy_family}
              {source.version_label ? ` ${source.version_label}` : ''}
            </Pill>
          )}
        </div>
      )}
    </li>
  )
}
