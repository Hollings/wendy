import Icon from '../Icon'

export default function BeadsPanel({ beads, beadSnippets, focusedBeadId, onBeadClick }) {
  const sorted = [...beads.values()].sort((a, b) => {
    const order = { in_progress: 0, open: 1, closed: 2, tombstone: 3 }
    return (order[a.status] ?? 4) - (order[b.status] ?? 4)
  })
  const active = sorted.filter(b => b.status === 'in_progress' || b.status === 'open')
  const recent = sorted.filter(b => b.status === 'closed').slice(0, 4)
  const shown = [...active, ...recent]
  const running = active.filter(b => b.status === 'in_progress').length

  return (
    <div className="panel beads-panel">
      <div className="panel-head">
        <h3><span className="tick" /> beads</h3>
        <span className="meta mono">{running} running · {beads.size} total</span>
      </div>
      <div className={'panel-body ' + (shown.length === 0 ? 'empty' : '')}>
        {shown.length === 0 ? (
          'no beads'
        ) : (
          <div className="beads">
            {shown.map(b => (
              <BeadCard
                key={b.id}
                bead={b}
                snippet={beadSnippets.get(b.id)}
                focused={b.id === focusedBeadId}
                onClick={() => onBeadClick(b)}
              />
            ))}
          </div>
        )}
      </div>
    </div>
  )
}

function BeadCard({ bead, snippet, focused, onClick }) {
  const activityText = snippet?.text
    ? snippet.text.slice(0, 80)
    : bead.status === 'in_progress' ? 'running · awaiting event' : 'idle'
  return (
    <div
      className={'bead ' + (focused ? 'focused' : '')}
      data-status={bead.status}
      onClick={onClick}
      title={bead.title}
    >
      <div className="bead-head">
        <span className="bead-id mono">#{String(bead.id).slice(0, 7)}</span>
        <span className="bead-status">{bead.status.replace('_', ' ')}</span>
      </div>
      <div className="bead-title">{bead.title}</div>
      <div className="bead-activity mono">
        <span className="glyph">
          {bead.status === 'in_progress' ? <Icon name="Tool" size={10} /> : <Icon name="End" size={10} />}
        </span>
        <span className="live-text truncate">{activityText}</span>
      </div>
    </div>
  )
}
