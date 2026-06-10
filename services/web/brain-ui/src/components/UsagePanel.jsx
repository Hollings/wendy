// API quota bars from /api/brain/usage. Declarative: BARS describes which
// fields render; adding a quota means adding a row here.
const BARS = [
  { label: 'session', pctKey: 'session_percent', resetKey: 'session_resets' },
  { label: 'week · all', pctKey: 'week_all_percent', resetKey: 'week_all_resets' },
  { label: 'week · sonnet', pctKey: 'week_sonnet_percent', resetKey: 'week_sonnet_resets' },
]

export default function UsagePanel({ usage }) {
  return (
    <div className="panel">
      <div className="panel-head">
        <h3>api usage</h3>
        {usage?.updated_at && <span className="meta">as of {shortDate(usage.updated_at)}</span>}
      </div>
      <div className="panel-body">
        {!usage?.available && <div className="panel-empty">no usage data</div>}
        {usage?.available && BARS.map(bar => {
          const pct = Math.min(100, usage[bar.pctKey] ?? 0)
          const warn = pct > 85 ? 'high' : pct > 65 ? 'mid' : ''
          return (
            <div key={bar.label} className="usage-row">
              <div className="usage-top">
                <span className="name">{bar.label}</span>
                <span className="mono">{pct}%</span>
              </div>
              <div className={'bar ' + warn}>
                <div className="fill" style={{ width: pct + '%' }} />
              </div>
              {usage[bar.resetKey] && (
                <div className="usage-reset dim">resets {shortDate(usage[bar.resetKey])}</div>
              )}
            </div>
          )
        })}
      </div>
    </div>
  )
}

function shortDate(iso) {
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return iso
  return d.toLocaleString('en-GB', { weekday: 'short', hour: '2-digit', minute: '2-digit' })
}
