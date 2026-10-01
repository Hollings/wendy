import { useState } from 'react'
import { agoShort, shortModel } from '../events'
import { eventText, isRunning, needsAttention, taskPhase } from '../timeline'
import Icon from './Icon'
export default function BeadsPanel({ beads, events, onSelect, filter, onFilter }) {
  const [query, setQuery] = useState('')
  const tasks = [...beads].filter(task => (filter !== 'running' || isRunning(task)) && (filter !== 'attention' || needsAttention(task)) && (filter !== 'completed' || taskPhase(task) === 'succeeded') && [task.title, task.id, task.model, task._channel, task.phase, task.close_reason].join(' ').toLowerCase().includes(query.toLowerCase())).sort((a, b) => Number(needsAttention(b)) - Number(needsAttention(a)) || Number(isRunning(b)) - Number(isRunning(a)))
  return <section className="tasks-panel" aria-label="Background tasks">
    <div className="tasks-toolbar"><div className="filters" aria-label="Task status">{[['all', 'All'], ['running', 'Running'], ['attention', 'Needs attention'], ['completed', 'Completed']].map(([id, label]) => <button key={id} className={filter === id ? 'filter active' : 'filter'} aria-pressed={filter === id} onClick={() => onFilter(id)}>{label}</button>)}</div><label className="search"><Icon name="Grep" size={14} /><input type="search" aria-label="Search tasks" placeholder="Filter ID, model, queue, status…" value={query} onChange={e => setQuery(e.target.value)} /></label></div>
    <div className="task-table-scroll"><table className="task-table">
      <thead><tr><th>Task</th><th>Phase</th><th>Model / queue</th><th>Description / latest activity</th><th>Last event</th></tr></thead>
      <tbody>{tasks.map(task => {
        const latest = events.findLast(event => event.bead_id === task.id)
        return <tr key={task.key || task.id}>
          <td><button className="task-link" onClick={() => onSelect(task.id)} aria-label={'Open task ' + task.id}>{task.id}<Icon name="ArrowRight" size={12} /></button></td>
          <td><span className={'phase-text phase-' + taskPhase(task)}>{taskPhase(task)}</span></td>
          <td><span>{task.model ? shortModel(task.model) : '—'}</span><span className="table-secondary">{task._channel ? '#' + task._channel : '—'}</span></td>
          <td className="task-description"><button onClick={() => onSelect(task.id)}>{task.title || task.id}</button><p title={task.close_reason || (latest ? eventText(latest) : '')}>{task.close_reason || (latest ? eventText(latest) : 'No events in live buffer; open to load saved log.')}</p></td>
          <td className="table-time">{latest ? latest.timestampEstimated ? 'saved log' : agoShort(latest.ts) + ' ago' : '—'}</td>
        </tr>
      })}</tbody>
    </table>{!tasks.length && <div className="empty-state"><h3>{beads.length ? 'No matching tasks' : 'No tasks reported'}</h3><p>{beads.length ? 'Clear the search or change the status filter.' : 'Waiting for task metadata.'}</p></div>}</div>
    <div className="feed-status"><span>{tasks.length} / {beads.length} tasks</span><span>Select a task to load its execution log</span></div>
  </section>
}
