import { useUsage } from '../hooks/useUsage'

function barClass(pct) {
  if (pct >= 80) return 'bar-fill--danger'
  if (pct >= 60) return 'bar-fill--warn'
  return 'bar-fill--ok'
}

function weekProgress(resetIso) {
  if (!resetIso) return 0
  const reset = new Date(resetIso)
  const sevenDays = 7 * 24 * 60 * 60 * 1000
  const until = reset - Date.now()
  return Math.max(0, Math.min(100, ((sevenDays - until) / sevenDays) * 100))
}

function Bar({ label, pct, resetIso }) {
  const progress = weekProgress(resetIso)
  return (
    <div className="usage-row">
      <div className="usage-row-label">
        <span>{label}</span>
        <span className="usage-pct">{pct}%</span>
      </div>
      <div className="usage-bar">
        <div className={`bar-fill ${barClass(pct)}`} style={{ width: `${Math.min(pct, 100)}%` }} />
        <div className="week-marker" style={{ left: `${progress}%` }} />
      </div>
    </div>
  )
}

export default function UsageBars() {
  const usage = useUsage()

  if (!usage) {
    return <div className="usage-empty">Usage data loading...</div>
  }

  return (
    <div className="usage-section">
      <Bar label="All models" pct={usage.week_all_percent ?? 0} resetIso={usage.week_all_resets} />
      <Bar label="Sonnet" pct={usage.week_sonnet_percent ?? 0} resetIso={usage.week_sonnet_resets} />
      {usage.updated_at && (
        <div className="usage-updated">
          Updated {new Date(usage.updated_at + (usage.updated_at.endsWith('Z') ? '' : 'Z')).toLocaleString([], {
            month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit',
          })}
        </div>
      )}
    </div>
  )
}
