import { contextWindowFor, channelColor, channelHue, formatTokens, agoShort } from '../events'

/**
 * One row per channel session: name, last activity, context-window fill.
 * Click toggles the channel in/out of the feed filter.
 */
export default function SessionsPanel({ channelsMap, channelStats, hiddenChannels, onToggleChannel }) {
  const rows = Object.entries(channelStats)
    .map(([id, st]) => ({
      id,
      name: channelsMap[id] || String(id).slice(-4),
      ...st,
    }))
    .sort((a, b) => b.lastTs - a.lastTs)

  return (
    <div className="panel">
      <div className="panel-head">
        <h3>sessions</h3>
        <span className="meta">{rows.length} active</span>
      </div>
      <div className="panel-body">
        {rows.length === 0 && <div className="panel-empty">no activity yet</div>}
        {rows.map(s => {
          const pct = Math.min(100, (s.tokens / contextWindowFor(s.model)) * 100)
          const muted = hiddenChannels.has(s.id)
          const hue = channelHue(s.id)
          const warn = pct > 85 ? 'high' : pct > 65 ? 'mid' : ''
          return (
            <button
              key={s.id}
              className={'session-row' + (muted ? ' muted' : '')}
              onClick={() => onToggleChannel(s.id)}
              title={muted ? `show #${s.name} in feed` : `hide #${s.name} from feed`}
            >
              <div className="session-top">
                <span className="dot" style={{ background: channelColor(s.id) }} />
                <span className="name">#{s.name}</span>
                <span className="ago">{agoShort(s.lastTs)}</span>
              </div>
              <div className="session-meta">
                <span className="mono">{formatTokens(s.tokens)} ctx · {Math.round(pct)}%</span>
                <span className="mono dim">{s.count} events</span>
              </div>
              <div className={'bar ' + warn}>
                <div
                  className="fill"
                  style={{ width: pct + '%', background: warn ? undefined : `oklch(0.7 0.13 ${hue})` }}
                />
              </div>
            </button>
          )
        })}
      </div>
    </div>
  )
}
