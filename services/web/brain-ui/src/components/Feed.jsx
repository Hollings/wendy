import { useState, useRef, useEffect, useCallback } from 'react'
import { KINDS, FILTER_GROUPS, toolPreview, clockTime, channelColor } from '../events'
import Icon from './Icon'

export default function Feed({
  events, totalCount, channelsMap,
  hiddenKinds, onToggleKind,
  focusedBead, onClearBead,
}) {
  const scrollRef = useRef(null)
  const [atBottom, setAtBottom] = useState(true)
  const [missed, setMissed] = useState(0)

  useEffect(() => {
    const s = scrollRef.current
    if (!s) return
    const onScroll = () => {
      const near = s.scrollHeight - s.scrollTop - s.clientHeight < 80
      setAtBottom(near)
      if (near) setMissed(0)
    }
    s.addEventListener('scroll', onScroll)
    return () => s.removeEventListener('scroll', onScroll)
  }, [])

  useEffect(() => {
    const s = scrollRef.current
    if (!s) return
    if (atBottom) {
      s.scrollTop = s.scrollHeight
    } else {
      setMissed(m => m + 1)
    }
  }, [events.length]) // eslint-disable-line react-hooks/exhaustive-deps

  const jumpToLive = useCallback(() => {
    const s = scrollRef.current
    if (s) s.scrollTop = s.scrollHeight
    setAtBottom(true)
    setMissed(0)
  }, [])

  return (
    <section className="feed">
      <div className="feed-header">
        <div className="filters">
          {FILTER_GROUPS.map(g => (
            <button
              key={g.id}
              className="chip"
              aria-pressed={!hiddenKinds.has(g.id)}
              onClick={() => onToggleKind(g.id)}
            >{g.label}</button>
          ))}
          {focusedBead && (
            <button className="chip bead-chip" aria-pressed="true" onClick={onClearBead}>
              bead {String(focusedBead.id).slice(0, 10)} ✕
            </button>
          )}
        </div>
        <span className="feed-count">{events.length}{events.length !== totalCount ? ` / ${totalCount}` : ''} events</span>
      </div>

      <div className="feed-scroll" ref={scrollRef}>
        {events.length === 0 && <div className="feed-empty">waiting for activity…</div>}
        {events.map(ev => <EventRow key={ev.id} ev={ev} channelsMap={channelsMap} />)}
      </div>

      {!atBottom && (
        <button className="jump-live" onClick={jumpToLive}>
          ↓ jump to live{missed > 0 ? ` · ${missed} new` : ''}
        </button>
      )}
    </section>
  )
}

// One generic row, fully driven by the KINDS registry.
function EventRow({ ev, channelsMap }) {
  const cfg = KINDS[ev.kind]
  if (!cfg) return null

  if (cfg.divider) {
    return (
      <div className={`divider tone-${cfg.tone}`}>
        <span className="rule" />
        <Icon name={cfg.icon(ev)} size={11} />
        <span>{cfg.label(ev)}</span>
        <span className="ts">{clockTime(ev.ts)}</span>
        <span className="rule" />
      </div>
    )
  }

  return (
    <div className={`row tone-${cfg.tone}`}>
      <span className="row-icon"><Icon name={cfg.icon(ev)} size={13} /></span>
      <div className="row-main">
        <div className="row-head">
          <span className="row-label">{cfg.label(ev)}</span>
          <SourceTag ev={ev} channelsMap={channelsMap} />
          <span className="ts">{clockTime(ev.ts)}</span>
        </div>
        <RowBody ev={ev} />
      </div>
    </div>
  )
}

function SourceTag({ ev, channelsMap }) {
  if (ev.bead_id) {
    return <span className="source bead">bead {String(ev.bead_id).slice(0, 10)}</span>
  }
  if (!ev.channel_id) return null
  const name = channelsMap[ev.channel_id] || String(ev.channel_id).slice(-4)
  return (
    <span className="source" style={{ color: channelColor(ev.channel_id) }}>
      #{name}
    </span>
  )
}

function RowBody({ ev }) {
  const [expanded, setExpanded] = useState(false)

  if (ev.kind === 'tool') {
    const preview = toolPreview(ev.tool, ev.input)
    const entries = Object.entries(ev.input ?? {})
    return (
      <div className="row-body mono clickable" onClick={() => setExpanded(e => !e)}>
        {!expanded && <div className="clamp-1">{preview || '(no input)'}</div>}
        {expanded && entries.map(([k, v]) => (
          <div key={k} className="kv">
            <span className="k">{k}</span>
            <span className="v">{typeof v === 'object' ? JSON.stringify(v, null, 2) : String(v)}</span>
          </div>
        ))}
      </div>
    )
  }

  const text = ev.kind === 'result' ? (ev.content ?? '') : (ev.text ?? '')
  const long = text.length > 280 || text.split('\n').length > 4
  return (
    <div
      className={'row-body' + (ev.kind === 'result' ? ' mono' : '') + (long ? ' clickable' : '')}
      onClick={() => long && setExpanded(e => !e)}
    >
      <div className={expanded || !long ? '' : 'clamp-4'}>{text}</div>
      {long && <span className="expand-hint">{expanded ? 'collapse' : 'expand'}</span>}
    </div>
  )
}
