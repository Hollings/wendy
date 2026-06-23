import { useCallback, useEffect, useRef, useState } from 'react'
import EventCard from './EventCard'

export default function Feed({ events, wsStatus, focusedBead, onBack }) {
  const scrollRef = useRef(null)
  const isLiveRef = useRef(true)
  const [isLive, setIsLive] = useState(true)

  // Scroll to bottom on new events if we're in live mode.
  // Reads isLiveRef (not state) to avoid stale-closure / re-render race.
  useEffect(() => {
    if (isLiveRef.current && scrollRef.current) {
      scrollRef.current.scrollTop = scrollRef.current.scrollHeight
    }
  }, [events.length])

  // Initial scroll to bottom on mount.
  useEffect(() => {
    if (scrollRef.current) {
      scrollRef.current.scrollTop = scrollRef.current.scrollHeight
    }
  }, [])

  const handleScroll = useCallback(() => {
    const el = scrollRef.current
    if (!el) return
    const atBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 80
    isLiveRef.current = atBottom
    setIsLive(atBottom)
  }, [])

  const jumpToLive = useCallback(() => {
    if (scrollRef.current) {
      scrollRef.current.scrollTop = scrollRef.current.scrollHeight
    }
    isLiveRef.current = true
    setIsLive(true)
  }, [])

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

      <div className="feed-scroll" ref={scrollRef} onScroll={handleScroll}>
        {events.length === 0 && (
          <div className="feed-empty">Waiting for activity...</div>
        )}
        {events.map(ev => <EventCard key={ev.id} event={ev} />)}
      </div>

      {!isLive && (
        <button className="jump-live" onClick={jumpToLive}>
          &darr; live
        </button>
      )}
    </div>
  )
}
