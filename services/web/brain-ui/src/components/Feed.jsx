import { useEffect, useRef, useState } from 'react'
import EventCard from './EventCard'

export default function Feed({ events, wsStatus, focusedBead, onBack }) {
  const sentinelRef = useRef(null)
  const [isLive, setIsLive] = useState(true)

  // IntersectionObserver: sentinel visible = autoscroll active
  useEffect(() => {
    const sentinel = sentinelRef.current
    if (!sentinel) return
    const obs = new IntersectionObserver(
      ([entry]) => setIsLive(entry.isIntersecting),
      { threshold: 0 },
    )
    obs.observe(sentinel)
    return () => obs.disconnect()
  }, [])

  // Scroll to bottom when new events arrive and we're in live mode
  useEffect(() => {
    if (isLive && sentinelRef.current) {
      sentinelRef.current.scrollIntoView({ behavior: 'instant' })
    }
  }, [events.length, isLive])

  const statusLabel = {
    connecting: 'Connecting...',
    connected: 'Live',
    disconnected: 'Reconnecting...',
    full: 'Server full',
  }[wsStatus] ?? wsStatus

  return (
    <div className="feed-col">
      <div className="feed-header">
        <span className={`ws-status ws-status--${wsStatus}`}>
          {statusLabel}
        </span>
      </div>

      {focusedBead && (
        <div className="bead-focus-bar">
          <span className="bead-focus-label">Bead</span>
          <span className="bead-focus-title">{focusedBead.title}</span>
          <button className="bead-focus-back" onClick={onBack}>
            &larr; Back to feed
          </button>
        </div>
      )}

      <div className="feed-scroll">
        {events.length === 0 && (
          <div className="feed-empty">Waiting for activity...</div>
        )}
        {events.map(ev => <EventCard key={ev.id} event={ev} />)}
        <div ref={sentinelRef} className="feed-sentinel" />
      </div>

      {!isLive && (
        <button
          className="jump-live"
          onClick={() => sentinelRef.current?.scrollIntoView({ behavior: 'smooth' })}
        >
          &darr; live
        </button>
      )}
    </div>
  )
}
