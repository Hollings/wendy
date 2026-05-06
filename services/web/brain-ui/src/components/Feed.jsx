import { useCallback, useEffect, useRef, useState } from 'react'
import { ThinkingBubble, ToolBubble, ResultBubble, SystemLine } from './bubbles'
import { channelColor } from '../channelColors'
import Icon from './Icon'

export default function Feed({
  events,
  visibleEvents,
  channelsMap,
  focusedBead,
  onClearFocus,
}) {
  const scrollRef = useRef(null)
  const [atBottom, setAtBottom] = useState(true)

  useEffect(() => {
    const s = scrollRef.current
    if (!s) return
    const onScroll = () => {
      setAtBottom(s.scrollHeight - s.scrollTop - s.clientHeight < 80)
    }
    s.addEventListener('scroll', onScroll)
    return () => s.removeEventListener('scroll', onScroll)
  }, [])

  useEffect(() => {
    const s = scrollRef.current
    if (!s || !atBottom) return
    s.scrollTop = s.scrollHeight
  }, [visibleEvents, atBottom])

  const jumpToLive = useCallback(() => {
    const s = scrollRef.current
    if (!s) return
    s.scrollTop = s.scrollHeight
    setAtBottom(true)
  }, [])

  return (
    <section className="feed-col">
      <div className="feed-header">
        <div className="feed-title">
          <span>Live feed</span>
          <span className="count">{visibleEvents.length} / {events.length} events</span>
          {focusedBead && (
            <button className="filter-chip" aria-pressed="true" onClick={onClearFocus}>
              bead #{String(focusedBead.id).slice(0, 7)} · clear
            </button>
          )}
        </div>
      </div>

      <div className="feed-scroll" ref={scrollRef}>
        {visibleEvents.length === 0 && (
          <div className="feed-empty">waiting for activity…</div>
        )}
        <div className="feed-rail">
          {visibleEvents.map(ev => {
            const channel = ev.channel_id && channelsMap[ev.channel_id]
              ? { name: channelsMap[ev.channel_id], color: channelColor(ev.channel_id) }
              : null
            if (ev.kind === 'thinking') return <ThinkingBubble key={ev.id} event={ev} channel={channel} />
            if (ev.kind === 'tool')     return <ToolBubble     key={ev.id} event={ev} channel={channel} />
            if (ev.kind === 'result')   return <ResultBubble   key={ev.id} event={ev} channel={channel} />
            if (ev.kind === 'session_end' || ev.kind === 'system') return <SystemLine key={ev.id} event={ev} />
            return null
          })}
        </div>
      </div>

      <div className="feed-footer">
        <span>stream.jsonl · tail -f · {events.length} / 200 buffered</span>
        <button className={'jump-live ' + (atBottom ? 'hidden' : '')} onClick={jumpToLive}>
          <Icon name="Live" size={12} /> jump to live
        </button>
      </div>
    </section>
  )
}
