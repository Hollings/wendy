import { useState, useEffect } from 'react'
import BeadCard from './BeadCard'
import UsageBars from './UsageBars'

const INACTIVE_MS = 10 * 60 * 1000 // 10 minutes

function relativeTime(ts) {
  const diff = (Date.now() - ts) / 1000
  if (diff < 60) return 'Active now'
  if (diff < 3600) return `${Math.floor(diff / 60)}m ago`
  if (diff < 86400) return `${Math.floor(diff / 3600)}h ago`
  return `${Math.floor(diff / 86400)}d ago`
}

function ChannelList({ activeChannels, channelsMap, channelContextPct, lastActivity, hiddenChannels, onToggleChannel }) {
  const [showInactive, setShowInactive] = useState(false)
  const [, setTick] = useState(0)

  useEffect(() => {
    const id = setInterval(() => setTick(t => t + 1), 15_000)
    return () => clearInterval(id)
  }, [])

  const now = Date.now()
  // Only auto-hide a channel from the sidebar if it's also disabled in the feed.
  // Channels enabled in the feed stay visible regardless of inactivity so that
  // when they become active again the feed doesn't surprise the user.
  const isInactive = id => {
    if (!hiddenChannels.has(id)) return false
    const ts = lastActivity[id]
    return !ts || (now - ts) > INACTIVE_MS
  }

  const visibleChannels = showInactive
    ? activeChannels
    : activeChannels.filter(id => !isInactive(id))

  const inactiveCount = activeChannels.filter(isInactive).length

  if (activeChannels.length === 0) return null

  return (
    <div className="sidebar-channels">
      <div className="sidebar-section-label">Channels</div>
      {visibleChannels.map(id => {
        const name = channelsMap[id] || `#${id.slice(-4)}`
        const shown = !hiddenChannels.has(id)
        const ts = lastActivity[id]
        const pct = channelContextPct?.[id] ?? 0
        return (
          <button
            key={id}
            className={`channel-chip channel-chip--sidebar${shown ? ' channel-chip--active' : ''}`}
            style={{ '--chip-fill': `${pct}%` }}
            data-tooltip={pct > 0 ? `${pct}% context used` : null}
            onClick={() => onToggleChannel(id)}
          >
            <span className="chip-check">{shown ? '✓' : '—'}</span>
            <span className="chip-info">
              <span className="chip-name">{name}</span>
              {ts && <span className="chip-time">{relativeTime(ts)}</span>}
            </span>
          </button>
        )
      })}
      {inactiveCount > 0 && (
        <button className="inactive-toggle" onClick={() => setShowInactive(s => !s)}>
          {showInactive ? 'Hide inactive' : `${inactiveCount} inactive`}
        </button>
      )}
    </div>
  )
}

export default function Sidebar({
  activeChannels, channelsMap, channelContextPct, lastActivity,
  hiddenChannels, onToggleChannel,
  beads, beadSnippets, focusedBeadId, onBeadClick,
}) {
  const sorted = [...beads.values()].sort((a, b) => {
    const order = { in_progress: 0, open: 1, closed: 2, tombstone: 3 }
    return (order[a.status] ?? 4) - (order[b.status] ?? 4)
  })
  const active = sorted.filter(b => b.status === 'in_progress' || b.status === 'open')
  const recent = sorted.filter(b => b.status === 'closed').slice(0, 4)
  const shown = [...active, ...recent]

  return (
    <div className="sidebar-col">
      <ChannelList
        activeChannels={activeChannels}
        channelsMap={channelsMap}
        channelContextPct={channelContextPct}
        lastActivity={lastActivity}
        hiddenChannels={hiddenChannels}
        onToggleChannel={onToggleChannel}
      />

      <div className="sidebar-section-label">Weekly usage</div>
      <UsageBars />

      {shown.length > 0 && (
        <>
          <div className="sidebar-section-label">
            Beads {active.length > 0 && <span className="bead-count">{active.length} active</span>}
          </div>
          <div className="bead-grid">
            {shown.map(bead => (
              <BeadCard
                key={bead.id}
                bead={bead}
                snippet={beadSnippets.get(bead.id)}
                focused={bead.id === focusedBeadId}
                onClick={() => onBeadClick(bead)}
              />
            ))}
          </div>
        </>
      )}
    </div>
  )
}
