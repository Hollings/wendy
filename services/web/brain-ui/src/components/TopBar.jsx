import { clearToken, clearPassphrase } from '../auth'
import { agoShort } from '../events'

const STATUS = {
  connecting:   { text: 'connecting', cls: 'warn' },
  connected:    { text: 'live', cls: 'ok' },
  disconnected: { text: 'reconnecting', cls: 'warn' },
  full:         { text: 'server full', cls: 'down' },
  auth_error:   { text: 'auth failed', cls: 'down' },
}

export default function TopBar({ wsStatus, viewers, lastActivity, onLogout }) {
  const st = STATUS[wsStatus] ?? STATUS.connecting
  return (
    <header className="topbar">
      <div className="brand">wendy<em>.brain</em></div>
      <div className="topbar-meta">
        {viewers != null && <span>{viewers} watching</span>}
        <span>last event <strong className="mono">{agoShort(lastActivity)}</strong> ago</span>
      </div>
      <div className="topbar-right">
        <span className={`conn ${st.cls}`}><span className="dot" />{st.text}</span>
        <button
          className="logout"
          onClick={() => { clearToken(); clearPassphrase(); onLogout?.() }}
        >logout</button>
      </div>
    </header>
  )
}
