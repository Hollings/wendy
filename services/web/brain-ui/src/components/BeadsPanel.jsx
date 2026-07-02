import { agoShort } from '../events'

const STATUS_ORDER = { in_progress: 0, open: 1, closed: 2, tombstone: 3 }

/**
 * Background task cards. Click focuses the feed on that bead's events.
 */
export default function BeadsPanel({ beads, beadSnippets, focusedBeadId, onBeadClick }) {
  const sorted = [...beads].sort(
    (a, b) => (STATUS_ORDER[a.status] ?? 4) - (STATUS_ORDER[b.status] ?? 4)
  )
  const active = sorted.filter(b => b.status === 'in_progress' || b.status === 'open')
  const recent = sorted.filter(b => b.status === 'closed').slice(0, 4)
  const shown = [...active, ...recent]
  const running = active.filter(b => b.status === 'in_progress').length

  return (
    <div className="panel">
      <div className="panel-head">
        <h3>beads</h3>
        <span className="meta">{running} running · {beads.length} total</span>
      </div>
      <div className="panel-body">
        {shown.length === 0 && <div className="panel-empty">no background tasks</div>}
        {shown.map(b => {
          const snippet = beadSnippets[b.id]
          return (
            <button
              key={b.id}
              className={'bead' + (b.id === focusedBeadId ? ' focused' : '')}
              data-status={b.status}
              onClick={() => onBeadClick(b)}
              title={b.title}
            >
              <div className="bead-top">
                <span className="status-pill">{b.status.replace('_', ' ')}</span>
                <span className="ago">{snippet ? agoShort(snippet.ts) : ''}</span>
              </div>
              <div className="bead-title">{b.title}</div>
              {snippet?.text && <div className="bead-snippet mono clamp-1">{snippet.text}</div>}
            </button>
          )
        })}
      </div>
    </div>
  )
}
