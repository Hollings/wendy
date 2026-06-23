function relTime(iso) {
  if (!iso) return ''
  const diff = Date.now() - new Date(iso).getTime()
  const m = Math.floor(diff / 60000)
  const h = Math.floor(diff / 3600000)
  const d = Math.floor(diff / 86400000)
  if (m < 1) return 'now'
  if (m < 60) return `${m}m`
  if (h < 24) return `${h}h`
  return `${d}d`
}

const STATUS_ICONS = {
  in_progress: { char: '▶', cls: 'bead-icon--running' },
  open:        { char: '●', cls: 'bead-icon--open' },
  closed:      { char: '✓', cls: 'bead-icon--closed' },
  tombstone:   { char: '—', cls: 'bead-icon--dead' },
}

export default function BeadCard({ bead, snippet, focused, onClick }) {
  const icon = STATUS_ICONS[bead.status] ?? STATUS_ICONS.open

  return (
    <div
      className={`bead-card${focused ? ' bead-card--focused' : ''}`}
      onClick={onClick}
      title={bead.title}
    >
      <div className="bead-card-header">
        <span className={`bead-icon ${icon.cls}`}>{icon.char}</span>
        <span className="bead-card-time">{relTime(bead.updated || bead.created)}</span>
      </div>
      <div className="bead-card-title">{bead.title}</div>
      {snippet && (
        <div className="bead-card-snippet">{snippet.text.slice(0, 70)}</div>
      )}
    </div>
  )
}
