import { useState, useMemo, useCallback, useEffect } from 'react'
import { useBrainStore } from '../useBrainStore'
import { FILTER_GROUPS } from '../events'
import { deriveFeed } from '../derive'
import TopBar from './TopBar'
import Feed from './Feed'
import SessionsPanel from './SessionsPanel'
import BeadsPanel from './BeadsPanel'

export default function Dashboard({ onLogout }) {
  const store = useBrainStore({ onAuthError: onLogout })
  const { events, channelsMap, channelStats, beads, beadSnippets, wsStatus, viewers } = store

  const [hiddenChannels, setHiddenChannels] = useState(new Set())
  const [hiddenKinds, setHiddenKinds] = useState(new Set())
  const [focusedBead, setFocusedBead] = useState(null)
  const [mobileView, setMobileView] = useState('feed')

  // Re-render every 10s so the "ago" labels stay fresh
  const [, setTick] = useState(0)
  useEffect(() => {
    const id = setInterval(() => setTick(t => t + 1), 10_000)
    return () => clearInterval(id)
  }, [])

  const hiddenKindSet = useMemo(() => {
    const kinds = new Set()
    for (const g of FILTER_GROUPS) {
      if (hiddenKinds.has(g.id)) g.kinds.forEach(k => kinds.add(k))
    }
    return kinds
  }, [hiddenKinds])

  // Source filters (channel / bead) apply first; kind filters apply inside
  // deriveFeed, after result-pairing and turn accounting, so tool outputs
  // still attach and turn summaries count what filters hide.
  const sourceEvents = useMemo(() => events.filter(ev => {
    if (focusedBead && ev.bead_id !== focusedBead.id) return false
    if (ev.channel_id && hiddenChannels.has(ev.channel_id)) return false
    return true
  }), [events, focusedBead, hiddenChannels])

  const derived = useMemo(
    () => deriveFeed(sourceEvents, hiddenKindSet),
    [sourceEvents, hiddenKindSet],
  )

  const toggleChannel = useCallback((id) => {
    setHiddenChannels(prev => {
      const next = new Set(prev)
      next.has(id) ? next.delete(id) : next.add(id)
      return next
    })
  }, [])

  const toggleKind = useCallback((id) => {
    setHiddenKinds(prev => {
      const next = new Set(prev)
      next.has(id) ? next.delete(id) : next.add(id)
      return next
    })
  }, [])

  const toggleBead = useCallback((bead) => {
    setFocusedBead(prev => prev?.id === bead.id ? null : { id: bead.id, title: bead.title })
  }, [])

  const lastActivity = events.length ? events[events.length - 1].ts : null

  return (
    <div className="app" data-mobile-view={mobileView}>
      <TopBar
        wsStatus={wsStatus}
        viewers={viewers}
        lastActivity={lastActivity}
        onLogout={onLogout}
      />
      <div className="main">
        <Feed
          rows={derived.rows}
          turns={derived.turns}
          lastTurnBySource={derived.lastTurnBySource}
          totalCount={events.length}
          channelsMap={channelsMap}
          hiddenKinds={hiddenKinds}
          onToggleKind={toggleKind}
          focusedBead={focusedBead}
          onClearBead={() => setFocusedBead(null)}
        />
        <aside className="sidebar">
          <SessionsPanel
            channelsMap={channelsMap}
            channelStats={channelStats}
            hiddenChannels={hiddenChannels}
            onToggleChannel={toggleChannel}
          />
          <BeadsPanel
            beads={beads}
            beadSnippets={beadSnippets}
            focusedBeadId={focusedBead?.id ?? null}
            onBeadClick={toggleBead}
          />
        </aside>
      </div>
      <button
        className="view-toggle"
        onClick={() => setMobileView(v => v === 'feed' ? 'sidebar' : 'feed')}
      >
        {mobileView === 'feed' ? 'status' : 'feed'}
      </button>
    </div>
  )
}
