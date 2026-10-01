import { useLayoutEffect, useRef, useState } from 'react'
import { CATEGORIES } from '../timeline'
import EventRow from './EventRow'
import Icon from './Icon'
export default function Feed({ rows, category, onCategory, query, onQuery, channelsMap, beads, selectedId, onSelect, paused, pending, onPause, onResume, source, isConnected }) {
  const scroll = useRef(null)
  const follow = useRef(true)
  const [nearBottom, setNearBottom] = useState(true)
  const previousPaused = useRef(paused)
  const lastSource = useRef(source)
  const visibleTurns = new Set()
  useLayoutEffect(() => {
    if (lastSource.current !== source || (previousPaused.current && !paused)) follow.current = true
    lastSource.current = source
    previousPaused.current = paused
    if (scroll.current && follow.current && !paused) {
      scroll.current.scrollTop = scroll.current.scrollHeight
      setNearBottom(true)
    }
  }, [rows, paused, source])
  function onScroll() {
    const node = scroll.current
    const near = node.scrollHeight - node.scrollTop - node.clientHeight < 40
    setNearBottom(near)
    if (!near && follow.current) { follow.current = false; onPause() }
  }
  function resume() {
    follow.current = true
    onResume()
    if (scroll.current) scroll.current.scrollTop = scroll.current.scrollHeight
  }
  return <section className="feed" aria-label="Activity timeline">
    <div className="feed-controls">
    <div className="feed-toolbar">
      <label className="search"><Icon name="Grep" size={14} /><input type="search" aria-label="Search activity" placeholder="Filter text, command, result, ID…" value={query} onChange={e => onQuery(e.target.value)} />{query && <button className="icon-button" onClick={() => onQuery('')} aria-label="Clear search"><Icon name="Close" size={14} /></button>}</label>
      <button className={'follow-button' + (paused ? ' is-paused' : '')} onClick={paused ? resume : onPause}><Icon name={paused ? 'Play' : 'Pause'} size={14} />{paused ? 'Resume live' : 'Pause'}</button>
    </div>
    <div className="filter-bar"><div className="filters" aria-label="Event type">{CATEGORIES.map(([id, label]) => <button key={id} aria-pressed={category === id} className={category === id ? 'filter active' : 'filter'} onClick={() => onCategory(id)}>{label}</button>)}</div><span className="event-count">{rows.length} {rows.length === 1 ? 'event' : 'events'}</span></div>
    </div>
    <div className="event-columns" aria-hidden="true"><span>TIME / Δ</span><span>SOURCE / TYPE</span><span>CONTENT / RESULT</span><span>STATE</span></div>
    <div className="feed-scroll" ref={scroll} onScroll={onScroll} tabIndex={0} aria-label="Scrollable activity">
      {!rows.length && <div className="empty-state"><h3>{query || category !== 'all' ? 'No matching events' : 'No events received'}</h3><p>{query || category !== 'all' ? 'Change the search or event type filter.' : isConnected ? 'Waiting for activity on this source.' : 'Waiting for the stream to reconnect.'}</p>{(query || category !== 'all') && <button className="text-button" onClick={() => { onQuery(''); onCategory('all') }}>Reset filters</button>}</div>}
      {rows.map((ev, index) => {
        const showTurn = index > 0 && !visibleTurns.has(ev.turnId)
        visibleTurns.add(ev.turnId)
        return <EventRow key={ev.id} ev={ev} channelsMap={channelsMap} beads={beads} selected={ev.id === selectedId} onSelect={() => onSelect(ev)} showTurn={showTurn} showDate={index === 0 || new Date(rows[index - 1].ts).toDateString() !== new Date(ev.ts).toDateString()} />
      })}
    </div>
    <div className={'feed-status' + (paused ? ' paused' : '')}><span>{paused ? 'PAUSED · incoming events buffered' : 'FOLLOW · buffer limit 1,200'}<span className="time-note"> · local time</span></span>{paused ? <button onClick={resume}>{pending > 0 ? '+' + pending + ' events · ' : ''}Jump to live ↓</button> : !nearBottom ? <button onClick={resume}>Jump to live ↓</button> : <span>Inspect for full payload</span>}</div>
  </section>
}
