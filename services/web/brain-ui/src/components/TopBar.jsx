import Icon from './Icon'
const STATUS = { connected: 'connected', connecting: 'connecting', disconnected: 'reconnecting', full: 'at capacity', auth_error: 'auth expired', demo: 'fixture data' }
export default function TopBar({ wsStatus, viewers, demo, menuOpen, onMenu, onReconnect, view, onView, running, attention }) {
  return <header className="topbar">
    <button className="icon-button menu-button" onClick={onMenu} aria-label="Toggle navigation" aria-expanded={!!menuOpen} aria-controls="brain-navigation"><Icon name="Menu" size={18} /></button>
    <nav className="view-tabs" aria-label="Dashboard views">
      <button className={view === 'activity' ? 'active' : ''} onClick={() => onView('activity')} aria-current={view === 'activity' ? 'page' : undefined}><Icon name="Live" size={14} />Event log</button>
      <button className={view === 'tasks' ? 'active' : ''} onClick={() => onView('tasks')} aria-current={view === 'tasks' ? 'page' : undefined}><Icon name="Task" size={14} />Tasks{running > 0 && <span className="tab-count" title="Running tasks">{running}</span>}</button>
    </nav>
    {attention > 0 && <button className="attention-link" onClick={() => onView('tasks', 'attention')}>{attention} {attention === 1 ? 'needs' : 'need'} attention</button>}
    <div className="topbar-status">
      {import.meta.env.VITE_BRAIN_LIVE_DEMO === '1' && <span title="Read-only connection to live Wendy over SSH">live Wendy</span>}
      {!demo && viewers != null && <span className="viewers" title="Connected viewer clients">{viewers} clients</span>}
      <span className={'connection ' + (wsStatus === 'connected' ? 'online' : '')} role="status" title={demo ? 'Fictional local fixtures. No tasks are executed.' : 'WebSocket connection'}><span className="tiny-signal" />{STATUS[wsStatus] || 'connecting'}</span>
      {!demo && !['connected', 'connecting'].includes(wsStatus) && <button className="icon-button" onClick={onReconnect} aria-label="Reconnect"><Icon name="Refresh" size={14} /></button>}
    </div>
  </header>
}
