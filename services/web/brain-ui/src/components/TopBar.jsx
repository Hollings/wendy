import { clearToken, clearPassphrase } from '../auth'

function agoShort(ts) {
  if (!ts) return '—'
  const ms = Date.now() - new Date(ts).getTime()
  const s = Math.max(0, Math.floor(ms / 1000))
  if (s < 60) return s + 's'
  const m = Math.floor(s / 60)
  if (m < 60) return m + 'm'
  return Math.floor(m / 60) + 'h'
}

const STATUS_TEXT = {
  connecting: 'connecting · /ws/brain',
  connected: 'live · /ws/brain',
  disconnected: 'reconnecting · /ws/brain',
  full: 'server full · /ws/brain',
  auth_error: 'auth failed · /ws/brain',
}

export default function TopBar({ sessionId, viewers, lastActivity, wsStatus, onLogout }) {
  const pillClass = wsStatus === 'connected' ? '' : wsStatus === 'full' || wsStatus === 'auth_error' ? 'down' : 'warn'
  return (
    <header className="topbar">
      <div className="brand">
        <div className="brand-mark" aria-hidden />
        <div className="brand-wordmark">wendy<em>.brain</em></div>
        <span className="brand-tag">obs · v2</span>
      </div>
      <div className="topbar-meta">
        {sessionId && (
          <span><span className="m-key">session</span><span className="m-val mono">{sessionId}</span></span>
        )}
        {viewers != null && (
          <span><span className="m-key">viewers</span><span className="m-val mono">{viewers}</span></span>
        )}
        <span><span className="m-key">last event</span><span className="m-val mono accent">{agoShort(lastActivity)} ago</span></span>
      </div>
      <div style={{ display: 'flex', alignItems: 'center' }}>
        <span className={`conn-pill ${pillClass}`}>
          <span className="dot" />
          <span>{STATUS_TEXT[wsStatus] ?? 'live · /ws/brain'}</span>
        </span>
        <button
          className="topbar-logout"
          onClick={() => { clearToken(); clearPassphrase(); onLogout?.(); }}
          title="Clear auth and reload"
        >logout</button>
      </div>
    </header>
  )
}
