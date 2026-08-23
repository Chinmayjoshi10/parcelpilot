/**
 * Application shell.
 *
 * Two contexts, one codebase. A customer sees Ask and Sources; staff also get
 * the operations console. That split is presentational only -- the actual
 * boundary is row-level security in the database, so a customer reaching a
 * staff endpoint gets 403 or an empty result regardless of what this file
 * renders.
 *
 * The header deliberately shows the active index version and retrieval mode.
 * Anyone watching a demo should be able to see which corpus produced an answer,
 * and whether the system is running degraded.
 */

import { useCallback, useEffect, useState } from 'react'
import ChatPanel from './components/ChatPanel'
import CitationInspector from './components/CitationInspector'
import LoginScreen from './components/LoginScreen'
import OpsConsole from './components/OpsConsole'
import SourcesPanel from './components/SourcesPanel'
import * as api from './lib/api'
import { Pill, Spinner } from './components/primitives'

const TABS = {
  ask: { label: 'Ask', staffOnly: false },
  operations: { label: 'Operations', staffOnly: true },
  sources: { label: 'Sources', staffOnly: false },
}

export default function App() {
  const [me, setMe] = useState(null)
  const [meta, setMeta] = useState(null)
  const [tab, setTab] = useState('ask')
  const [booting, setBooting] = useState(true)
  const [inspecting, setInspecting] = useState(null)

  useEffect(() => {
    // Meta is unauthenticated so the login screen can show what this
    // deployment is running.
    api.getMeta().then(setMeta).catch(() => {})

    // A token may still be in storage from a previous session. Validate it
    // rather than trusting it: it may have expired.
    if (!api.getToken()) {
      setBooting(false)
      return
    }
    api
      .whoami()
      .then(setMe)
      .catch(() => api.logout())
      .finally(() => setBooting(false))
  }, [])

  const signOut = useCallback(() => {
    api.logout()
    setMe(null)
    setTab('ask')
  }, [])

  if (booting) {
    return (
      <div className="flex min-h-full items-center justify-center">
        <Spinner label="starting" />
      </div>
    )
  }

  if (!me) {
    return <LoginScreen meta={meta} onSignedIn={setMe} />
  }

  const visibleTabs = Object.entries(TABS).filter(
    ([, config]) => !config.staffOnly || me.is_staff,
  )

  return (
    <div className="min-h-full">
      <header className="sticky top-0 z-40 border-b border-edge bg-base/85 backdrop-blur-xl">
        <div className="mx-auto flex max-w-7xl flex-wrap items-center gap-3 px-4 py-3">
          <div className="flex items-baseline gap-2">
            <span className="text-sm font-semibold tracking-tight text-verified">
              ParcelPilot
            </span>
            <span className="text-[10px] uppercase tracking-widest text-muted">
              {me.is_staff ? 'Operations' : 'Customer portal'}
            </span>
          </div>

          <nav className="flex items-center gap-1">
            {visibleTabs.map(([key, config]) => (
              <button
                key={key}
                onClick={() => setTab(key)}
                className={`rounded-md px-3 py-1.5 text-[12px] font-medium transition ${
                  tab === key
                    ? 'bg-raised text-ink'
                    : 'text-muted hover:bg-raised hover:text-ink'
                }`}
              >
                {config.label}
              </button>
            ))}
          </nav>

          <div className="ml-auto flex items-center gap-2">
            {meta?.retrieval_mode === 'lexical_only' && (
              <Pill
                className="border-warn/40 bg-warn/10 text-warn"
                title="Dense retrieval unavailable"
              >
                degraded
              </Pill>
            )}
            {meta?.active_index && (
              <span className="tabular hidden text-[10px] text-muted sm:inline">
                index v{meta.active_index.index_version_id}
              </span>
            )}
            <Pill
              className={
                me.is_staff
                  ? 'border-verified/40 bg-verified/10 text-verified'
                  : 'border-active/40 bg-active/10 text-active'
              }
            >
              {me.account_id ?? me.role.replace(/_/g, ' ')}
            </Pill>
            <button onClick={signOut} className="btn-ghost px-2 py-1 text-[11px]">
              Switch
            </button>
          </div>
        </div>
      </header>

      <main className="mx-auto max-w-7xl px-4 py-5">
        {tab === 'ask' && (
          <ChatPanel
            me={me}
            meta={meta}
            onInspect={({ runId, citation, index }) =>
              setInspecting({ runId, citation, index })
            }
          />
        )}
        {tab === 'operations' && me.is_staff && (
          <OpsConsole
            me={me}
            onInspectClause={(citation) =>
              // A dashboard clause has no run behind it, so there is no run to
              // scope the lookup to. Shown inline instead of opening the
              // inspector, which requires one.
              window.alert(citation.quote)
            }
          />
        )}
        {tab === 'sources' && <SourcesPanel me={me} />}
      </main>

      <CitationInspector
        open={Boolean(inspecting)}
        runId={inspecting?.runId}
        citation={inspecting?.citation}
        index={inspecting?.index}
        onClose={() => setInspecting(null)}
      />
    </div>
  )
}
