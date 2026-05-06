import { channelColor } from '../../channelColors'

function agoShort(ts) {
  if (!ts) return '—'
  const ms = Date.now() - ts
  const s = Math.max(0, Math.floor(ms / 1000))
  if (s < 60) return s + 's'
  const m = Math.floor(s / 60)
  if (m < 60) return m + 'm'
  return Math.floor(m / 60) + 'h'
}

export default function ChannelsPanel({ channels, activity, hiddenChannels, onToggle }) {
  if (channels.length === 0) return null
  return (
    <div className="panel">
      <div className="panel-head">
        <h3><span className="tick" /> channels</h3>
        <span className="meta mono">{channels.length} active</span>
      </div>
      <div className="panel-body">
        <div className="channels">
          {channels.map(ch => {
            const act = activity[ch.id] || { pct: 0, last: null }
            const isOn = !hiddenChannels.has(ch.id)
            const color = channelColor(ch.id)
            return (
              <button
                key={ch.id}
                className="channel"
                aria-pressed={isOn}
                onClick={() => onToggle(ch.id)}
                title={isOn ? 'mute #' + ch.name : 'show #' + ch.name}
              >
                <span className="hue" style={{ background: color, color }} />
                <span className="name"><span className="hash">#</span>{ch.name}</span>
                <span className="activity" style={{ color }}>
                  <span>{act.last ? agoShort(act.last) : '—'}</span>
                  <span className="mini-bar">
                    <span className="mf" style={{ right: `${100 - (act.pct || 0)}%` }} />
                  </span>
                </span>
              </button>
            )
          })}
        </div>
      </div>
    </div>
  )
}
