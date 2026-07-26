// The event feed: derived rows grouped into collapsible turns.
//
// deriveFeed (in Dashboard) assigns every row a turnId; here each
// session_start renders as a clickable turn header, and the rows of a
// collapsed turn are skipped. Closed turns default to collapsed except the
// most recent one per source, so history reads as a list of turn summaries
// while the live turn stays fully visible.

import { useState, useRef, useEffect, useCallback } from 'react'
import { FILTER_GROUPS, rowSpec, clockTime, formatDuration, channelColor } from '../events'
import EventRow, { GapRow } from './EventRow'
import Icon from './Icon'

export default function Feed({
  rows, turns, lastTurnBySource, totalCount, channelsMap,
  hiddenKinds, onToggleKind,
  focusedBead, onClearBead,
}) {
  const scrollRef = useRef(null)
  const [atBottom, setAtBottom] = useState(true)
  const [missed, setMissed] = useState(0)
  // Turn ids the user has toggled away from their default collapse state.
  const [toggledTurns, setToggledTurns] = useState(() => new Set())

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
  }, [rows.length]) // eslint-disable-line react-hooks/exhaustive-deps

  const jumpToLive = useCallback(() => {
    const s = scrollRef.current
    if (s) s.scrollTop = s.scrollHeight
    setAtBottom(true)
    setMissed(0)
  }, [])

  const toggleTurn = useCallback((turnId) => {
    setToggledTurns(prev => {
      const next = new Set(prev)
      next.has(turnId) ? next.delete(turnId) : next.add(turnId)
      return next
    })
  }, [])

  const isCollapsed = useCallback((turn) => {
    const byDefault = turn.closed && lastTurnBySource.get(turn.src) !== turn.id
    return toggledTurns.has(turn.id) ? !byDefault : byDefault
  }, [toggledTurns, lastTurnBySource])

  const rendered = []
  let visibleCount = 0
  for (const row of rows) {
    if (row.kind === 'gap') {
      rendered.push(<GapRow key={row.id} ev={row} />)
      continue
    }
    const turn = row.turnId ? turns.get(row.turnId) : null
    const collapsed = turn ? isCollapsed(turn) : false
    if (row.kind === 'session_start' && turn) {
      rendered.push(
        <TurnHeader
          key={row.id} ev={row} turn={turn} collapsed={collapsed}
          channelsMap={channelsMap} onToggle={() => toggleTurn(turn.id)}
        />,
      )
      visibleCount++
      continue
    }
    if (collapsed) continue
    rendered.push(<EventRow key={row.id} ev={row} channelsMap={channelsMap} />)
    visibleCount++
  }

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
        <span className="feed-count">{visibleCount}{visibleCount !== totalCount ? ` / ${totalCount}` : ''} events</span>
      </div>

      <div className="feed-scroll" ref={scrollRef}>
        {rendered.length === 0 && <div className="feed-empty">waiting for activity…</div>}
        {rendered}
      </div>

      {!atBottom && (
        <button className="jump-live" onClick={jumpToLive}>
          ↓ jump to live{missed > 0 ? ` · ${missed} new` : ''}
        </button>
      )}
    </section>
  )
}

// A turn's session_start divider, upgraded to a collapse toggle. Collapsed it
// carries the whole turn's summary; expanded it is just the boundary line.
function TurnHeader({ ev, turn, collapsed, channelsMap, onToggle }) {
  const spec = rowSpec(ev)
  const live = !turn.closed
  return (
    <button
      className={`divider turn-header tone-${turn.errors > 0 ? 'error' : 'system'}${live ? ' live' : ''}`}
      onClick={onToggle}
      title={collapsed ? 'expand turn' : 'collapse turn'}
    >
      <span className="rule" />
      <span className="caret">{collapsed ? '▸' : '▾'}</span>
      <span>{spec.label}</span>
      {live && <span className="live-dot" />}
      <SourceInline ev={ev} channelsMap={channelsMap} />
      {collapsed && <TurnSummary turn={turn} />}
      <span className="ts" title={new Date(ev.ts).toLocaleString()}>{clockTime(ev.ts)}</span>
      <span className="rule" />
    </button>
  )
}

function TurnSummary({ turn }) {
  const { counts, endEv, errors } = turn
  const bits = []
  if (counts.said) bits.push(`${counts.said} said`)
  if (counts.thoughts) bits.push(`${counts.thoughts} thoughts`)
  if (counts.tools) bits.push(`${counts.tools} tools`)
  if (!bits.length && counts.events) bits.push(`${counts.events} events`)
  if (errors > 0) bits.push(`${errors} errors`)
  if (endEv?.durationMs) bits.push(formatDuration(endEv.durationMs))
  if (endEv?.cost) bits.push(`$${endEv.cost < 0.01 ? endEv.cost.toFixed(4) : endEv.cost.toFixed(2)}`)
  return <span className="turn-summary">{bits.join(' · ') || 'empty'}</span>
}

function SourceInline({ ev, channelsMap }) {
  if (ev.bead_id) return <span className="source bead">bead {String(ev.bead_id).slice(0, 10)}</span>
  if (!ev.channel_id) return null
  const name = channelsMap[ev.channel_id] || String(ev.channel_id).slice(-4)
  return <span className="source" style={{ color: channelColor(ev.channel_id) }}>#{name}</span>
}
