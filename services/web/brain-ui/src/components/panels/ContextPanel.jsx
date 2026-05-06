import { channelHue } from '../../channelColors'

const fmt = new Intl.NumberFormat('en-US')
const CONTEXT_WINDOW = 200_000

export default function ContextPanel({ sessions }) {
  return (
    <div className="panel">
      <div className="panel-head">
        <h3><span className="tick" /> context · per session</h3>
        <span className="meta mono">{sessions.length} live</span>
      </div>
      <div className="panel-body">
        <div className="ctx-list">
          {sessions.length === 0 && (
            <div className="ctx-empty">no usage yet</div>
          )}
          {sessions.map(s => {
            const pct = Math.min(100, (s.tokens / CONTEXT_WINDOW) * 100)
            const tone = pct > 85 ? 'rose' : pct > 65 ? 'amber' : null
            const h = channelHue(s.id)
            const fill = tone === 'rose'
              ? 'linear-gradient(to right, var(--rose-dim), var(--rose))'
              : tone === 'amber'
              ? 'linear-gradient(to right, var(--amber-dim), var(--amber))'
              : `linear-gradient(to right, oklch(0.55 0.10 ${h}), oklch(0.78 0.14 ${h}))`
            return (
              <div key={s.id} className="ctx-row">
                <div className="ctx-top">
                  <span className="hue" style={{ background: `oklch(0.78 0.15 ${h})`, color: `oklch(0.78 0.15 ${h})` }} />
                  <span className="name"><span className="hash">#</span>{s.name}</span>
                  <span className="tokens mono">{fmt.format(s.tokens)}</span>
                  <span className="pct mono">{Math.round(pct)}%</span>
                </div>
                <div className="bar">
                  <div className="fill" style={{ width: pct + '%', background: fill }} />
                  <div className="ticks" />
                </div>
              </div>
            )
          })}
        </div>
      </div>
    </div>
  )
}
