/**
 * Context switcher / login.
 *
 * The identities come from the accounts table, not a hardcoded list, so the
 * switcher reflects what was actually ingested.
 *
 * Switching identity here is the clearest demonstration in the whole product:
 * sign in as Northstar and LumenWorks' agreement is simply not in the corpus
 * you can see. Not filtered out of a view -- invisible at the database, enforced
 * by row-level security. The copy says so, because it is the point.
 */

import { useEffect, useState } from 'react'
import * as api from '../lib/api'
import { ErrorNote, Pill, Spinner } from './primitives'

export default function LoginScreen({ onSignedIn, meta, onShowDesign }) {
  const [logins, setLogins] = useState([])
  const [busy, setBusy] = useState(null)
  const [error, setError] = useState(null)
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    api
      .listLogins()
      .then(setLogins)
      .catch((e) => setError(e.message))
      .finally(() => setLoading(false))
  }, [])

  const signIn = async (login) => {
    setBusy(login.user_id)
    setError(null)
    try {
      await api.login(login)
      const me = await api.whoami()
      onSignedIn(me)
    } catch (e) {
      setError(e.message)
    } finally {
      setBusy(null)
    }
  }

  const customers = logins.filter((l) => l.role === 'customer')
  const staff = logins.filter((l) => l.role !== 'customer')

  return (
    <div className="flex min-h-full items-center justify-center px-4 py-12">
      <div className="w-full max-w-2xl">
        <header className="mb-8 text-center">
          <p className="text-[10px] font-semibold uppercase tracking-[0.3em] text-verified">
            ParcelPilot
          </p>
          <h1 className="mt-2 text-2xl font-semibold tracking-tight text-ink">
            Agentic Intelligence Console
          </h1>
          <p className="mx-auto mt-3 max-w-lg text-[13px] leading-relaxed text-muted">
            Cited, verifiable reasoning over policies, contracts and operational
            data. Every claim is validated against the document it quotes, and
            tenancy is enforced in the database rather than in application code.
          </p>
          {onShowDesign && (
            <button
              onClick={onShowDesign}
              className="mt-4 rounded-lg border border-edge px-3 py-1.5 text-[12px] font-medium text-ink transition hover:border-verified/40 hover:text-verified"
            >
              How it works — architecture, agent design, and one real defect →
            </button>
          )}
        </header>

        {error && (
          <div className="mb-4">
            <ErrorNote onDismiss={() => setError(null)}>{error}</ErrorNote>
          </div>
        )}

        {loading ? (
          <div className="flex justify-center py-8">
            <Spinner label="loading identities" />
          </div>
        ) : (
          <div className="space-y-6">
            <Group
              title="Customer portal"
              note="Scoped to one account. Their own agreement plus global policies — nothing else exists for them."
            >
              {customers.map((login) => (
                <Option
                  key={login.user_id}
                  login={login}
                  busy={busy === login.user_id}
                  onClick={() => signIn(login)}
                />
              ))}
            </Group>

            <Group
              title="Internal operations"
              note="Tenant-wide visibility, proactive issue detection, and the approval queue."
            >
              {staff.map((login) => (
                <Option
                  key={login.user_id}
                  login={login}
                  busy={busy === login.user_id}
                  onClick={() => signIn(login)}
                />
              ))}
            </Group>
          </div>
        )}

        {meta && (
          <p className="tabular mt-8 text-center text-[10px] text-faint">
            {meta.provider} · {meta.synthesis_model} · retrieval{' '}
            {meta.retrieval_mode}
            {meta.active_index
              ? ` · index v${meta.active_index.index_version_id} (${meta.active_index.chunk_count} chunks)`
              : ''}
            {meta.ready ? '' : ' · NOT READY'}
          </p>
        )}
      </div>
    </div>
  )
}

function Group({ title, note, children }) {
  return (
    <section>
      <h2 className="text-[10px] font-semibold uppercase tracking-widest text-muted">
        {title}
      </h2>
      <p className="mt-1 text-[11px] leading-relaxed text-muted">{note}</p>
      <div className="mt-2.5 space-y-1.5">{children}</div>
    </section>
  )
}

function Option({ login, busy, onClick }) {
  const isCustomer = login.role === 'customer'
  return (
    <button
      onClick={onClick}
      disabled={busy}
      className="glass group flex w-full items-center justify-between gap-3 rounded-lg px-4 py-3 text-left transition hover:border-verified/40 disabled:opacity-50"
    >
      <div className="min-w-0">
        <p className="truncate text-[13px] font-medium text-ink">{login.label}</p>
        <p className="tabular mt-0.5 text-[11px] text-muted">{login.user_id}</p>
      </div>
      <div className="flex shrink-0 items-center gap-2">
        {login.account_id && (
          <Pill className="border-edge text-muted">{login.account_id}</Pill>
        )}
        <Pill
          className={
            isCustomer
              ? 'border-active/40 bg-active/10 text-active'
              : 'border-verified/40 bg-verified/10 text-verified'
          }
        >
          {login.role.replace(/_/g, ' ')}
        </Pill>
        {busy ? (
          <Spinner label="" />
        ) : (
          <span className="text-faint transition group-hover:text-verified">→</span>
        )}
      </div>
    </button>
  )
}
