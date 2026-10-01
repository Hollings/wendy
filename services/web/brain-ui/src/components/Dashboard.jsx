import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useBrainStore } from '../useBrainStore'
import { useTaskLog } from '../useTaskLog'
import { buildTimeline, filterTimeline, isRunning, mergeHistory, needsAttention, taskPhase } from '../timeline'
import { clearAuth } from '../auth'
import { agoShort, shortModel } from '../events'
import TopBar from './TopBar'
import SessionsPanel from './SessionsPanel'
import BeadsPanel from './BeadsPanel'
import Feed from './Feed'
import Inspector from './Inspector'
import Icon from './Icon'

export default function Dashboard({ onLogout }) {
  const store = useBrainStore({ onAuthError: onLogout })
  return <DashboardView store={store} onLogout={onLogout} />
}

export function DashboardView({ store, onLogout, demo = false }) {
  const [view, setView] = useState('activity')
  const [taskFilter, setTaskFilter] = useState('all')
  const [source, setSource] = useState('all')
  const [category, setCategory] = useState('all')
  const [query, setQuery] = useState('')
  const [selected, setSelected] = useState(null)
  const selectionTrigger = useRef(null)
  const selectEvent = ev => { selectionTrigger.current = document.activeElement; setSelected(ev) }
  const closeDetails = () => { setSelected(null); requestAnimationFrame(() => selectionTrigger.current?.focus({ preventScroll: true })) }
  const [paused, setPaused] = useState(null)
  const [mobileNav, setMobileNav] = useState(false)
  const [theme, setTheme] = useState(() => { try { return localStorage.getItem('brain_theme') || 'dark' } catch { return 'dark' } })
  const [, tick] = useState(0)
  useEffect(() => {
    if (!mobileNav) return
    const close = event => { if (event.key === 'Escape') { setMobileNav(false); document.querySelector('.menu-button')?.focus() } }
    document.addEventListener('keydown', close)
    return () => document.removeEventListener('keydown', close)
  }, [mobileNav])
  useEffect(() => { const timer = setInterval(() => tick(value => value + 1), 10_000); return () => clearInterval(timer) }, [])
  useEffect(() => { try { localStorage.setItem('brain_theme', theme) } catch { /* Optional preference. */ } }, [theme])
  const { events, channelsMap, channelStats, beads, wsStatus, viewers, syncError } = store
  const taskId = source.startsWith('task:') ? source.slice(5) : null
  const task = beads.find(item => item.id === taskId)
  const history = useTaskLog(demo ? null : taskId, onLogout)
  const allEvents = useMemo(() => mergeHistory(events, history.events), [events, history.events])
  const allRows = useMemo(() => buildTimeline(allEvents), [allEvents])
  const displayRows = paused?.rows || allRows
  const rows = useMemo(() => filterTimeline(displayRows, { source, category, query }), [displayRows, source, category, query])
  const selectedRow = selected ? displayRows.find(ev => ev.id === selected.id) || selected : null
  const pending = paused ? allRows.filter(ev => !paused.ids.has(ev.id)).length : 0
  const pause = useCallback(() => setPaused(current => current || { rows: allRows, ids: new Set(allRows.map(ev => ev.id)) }), [allRows])
  const chooseSource = value => {
    setSource(value); setView('activity'); setCategory('all'); setQuery(''); setSelected(null); setPaused(null); setMobileNav(false)
  }
  const showView = (value, filter) => { setView(value); if (filter) setTaskFilter(filter); setSelected(null); setMobileNav(false) }
  const sourceName = source === 'all' ? 'All sources' : taskId || '#' + (channelsMap[source.slice(8)] || source.slice(8))
  const running = beads.filter(isRunning).length
  const attention = beads.filter(needsAttention).length
  const activeTasks = beads.filter(item => !['succeeded', 'cancelled'].includes(taskPhase(item)))
  const selectedChannel = source.startsWith('channel:') ? channelStats[source.slice(8)] : null

  return (
    <div className="brain-app" data-theme={theme} data-nav-open={mobileNav}>
      <a href="#activity-content" className="skip-link">Skip to activity</a>
      <aside id="brain-navigation" className="navigation" aria-label="Brain navigation">
        <a className="console-name" href="#" onClick={e => { e.preventDefault(); chooseSource('all') }}>wendy<span>/</span>brain</a>
        <SessionsPanel channelsMap={channelsMap} channelStats={channelStats} events={displayRows} source={source} onSelect={chooseSource} />
        <nav className="task-navigation" aria-label="Active tasks">
          <div className="nav-section-label">TASKS<span>{activeTasks.length} active</span></div>
          <div className="active-task-list">{activeTasks.map(item => <button key={item.key || item.id} className={'task-source' + (source === 'task:' + item.id ? ' selected' : '')} onClick={() => chooseSource('task:' + item.id)} aria-pressed={source === 'task:' + item.id} title={item.title}>
            <span className="task-source-meta"><code>{item.id}</code><span className={'phase-text phase-' + taskPhase(item)}>{taskPhase(item)}</span></span><span className="task-source-title">{item.title}</span>
          </button>)}</div>
          {!activeTasks.length && <p className="nav-empty">No active tasks</p>}
          <button className="nav-all-tasks" onClick={() => showView('tasks')}>Task table <Icon name="ArrowRight" size={12} /></button>
        </nav>
        <div className="nav-footer">
          <button className="text-button" onClick={() => setTheme(value => value === 'light' ? 'dark' : 'light')}><Icon name="Theme" size={13} />{theme === 'light' ? 'Dark' : 'Light'}</button>
          {!demo && <button className="text-button" onClick={() => { clearAuth(); onLogout?.() }}>Sign out</button>}
        </div>
      </aside>
      {mobileNav && <button className="nav-scrim" aria-label="Close navigation" onClick={() => setMobileNav(false)} />}
      <div className="workspace">
        <TopBar wsStatus={wsStatus} viewers={viewers} demo={demo} menuOpen={mobileNav} onMenu={() => setMobileNav(value => !value)} onReconnect={store.reconnect} view={view} onView={showView} running={running} attention={attention} />
        <main id="activity-content" tabIndex={-1} className="main-content">
          {syncError && <div className="notice" role="status"><Icon name="Info" size={14} />{syncError}</div>}
          {wsStatus !== 'connected' && wsStatus !== 'demo' && <div className="notice" role="status"><Icon name="Live" size={14} />{wsStatus === 'full' ? 'Connection limit reached. Retrying.' : 'Stream disconnected. Retrying; existing events retained.'}<button onClick={store.reconnect}>Reconnect</button></div>}
          {view === 'tasks'
            ? <BeadsPanel beads={beads} events={allRows} onSelect={id => chooseSource('task:' + id)} filter={taskFilter} onFilter={setTaskFilter} />
            : <div className="activity-shell" data-inspecting={!!selectedRow}>
                <div className="timeline-column">
                  <div className="timeline-heading"><h1>{sourceName}</h1>{task && <span className="scope-title" title={task.title}>{task.title}</span>}{source !== 'all' && <button className="text-button" onClick={() => chooseSource('all')}>Clear scope ×</button>}<span className="scope-label">EVENT LOG</span></div>
                  {taskId && <div className="task-context">
                    <div className="task-context-meta"><span className={'phase-text phase-' + taskPhase(task || {})}>{taskPhase(task || {})}</span>{task?.model && <span>model: {shortModel(task.model)}</span>}{task?._channel && <span>queue: #{task._channel}</span>}</div>
                    {task?.close_reason && <p>{task.close_reason}</p>}
                    {history.error && <p role="status" className="error-text">{history.error}</p>}
                    {history.loading && <p className="muted" role="status">Loading task log…</p>}
                    {history.truncated && <p className="muted">Log truncated to recent history.</p>}
                  </div>}
                  {selectedChannel && <div className="source-context"><span>model: {selectedChannel.model ? shortModel(selectedChannel.model) : 'unknown'}</span><span>last event: {agoShort(selectedChannel.lastTs)} ago</span></div>}
                  <Feed rows={rows} category={category} onCategory={setCategory} query={query} onQuery={setQuery} channelsMap={channelsMap} beads={beads} selectedId={selected?.id} onSelect={selectEvent} paused={!!paused} pending={pending} onPause={pause} onResume={() => setPaused(null)} source={source} isConnected={['connected', 'demo'].includes(wsStatus)} />
                </div>
                <Inspector event={selectedRow} channelsMap={channelsMap} beads={beads} onClose={closeDetails} />
              </div>}
        </main>
      </div>
    </div>
  )
}
