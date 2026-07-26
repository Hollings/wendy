import { useState, useRef, useEffect, useCallback } from 'react'
import { FILTER_GROUPS, rowSpec, bodyText, toolPreview, clockTime, channelColor } from '../events'
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
  const spec = rowSpec(ev)

  if (spec.divider) {
    return (
      <div className={`divider tone-${spec.tone}`}>
        <span className="rule" />
        <Icon name={spec.icon} size={11} />
        <span>{spec.label}</span>
        <SourceTag ev={ev} channelsMap={channelsMap} />
        <span className="ts">{clockTime(ev.ts)}</span>
        <span className="rule" />
      </div>
    )
  }

  return (
    <div className={`row tone-${spec.tone}${ev.mergeOpen ? ' pending' : ''}`}>
      <span className="row-icon"><Icon name={spec.icon} size={13} /></span>
      <div className="row-main">
        <div className="row-head">
          <span className="row-label">{spec.label}</span>
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

// Tool-specific expanded views; anything not listed falls back to key/value.
const TOOL_DETAIL = {
  Edit: EditDiff,
  Write: WriteDetail,
}

function EditDiff({ input }) {
  return (
    <div className="expanded-body">
      <div className="diff-file">{input.file_path}</div>
      <div className="diff-block old">{input.old_string ?? ''}</div>
      <div className="diff-block new">{input.new_string ?? ''}</div>
    </div>
  )
}

function WriteDetail({ input }) {
  return (
    <div className="expanded-body">
      <div className="diff-file">{input.file_path}</div>
      <div className="diff-block new">{input.content ?? ''}</div>
    </div>
  )
}

// Kinds whose body is a raw payload dump rather than prose.
const BODY_COMPONENTS = {
  tool: ToolBody,
  unknown: UnknownBody,
}

// Kinds rendered in the mono face (machine output rather than prose).
const MONO_KINDS = new Set(['result', 'task', 'rate_limit', 'unknown'])

function RowBody({ ev }) {
  const Custom = BODY_COMPONENTS[ev.kind]
  if (Custom) return <Custom ev={ev} />
  return <TextBody ev={ev} text={bodyText(ev)} />
}

function ToolBody({ ev }) {
  const [expanded, setExpanded] = useState(false)
  const preview = toolPreview(ev.tool, ev.input)
  const entries = Object.entries(ev.input ?? {})
  const Detail = TOOL_DETAIL[ev.tool]
  return (
    <div className="row-body mono clickable" onClick={() => setExpanded(e => !e)}>
      {!expanded && <div className="clamp-1">{preview || '(no input)'}</div>}
      {expanded && Detail && <Detail input={ev.input ?? {}} />}
      {expanded && !Detail && (
        <div className="expanded-body">
          {entries.map(([k, v]) => (
            <div key={k} className="kv">
              <span className="k">{k}</span>
              <span className="v">{typeof v === 'object' ? JSON.stringify(v, null, 2) : String(v)}</span>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}

// Catch-all for event shapes the parser does not recognize -- shows the raw
// payload so a new CLI event type is visible in the feed the day it appears.
function UnknownBody({ ev }) {
  const [expanded, setExpanded] = useState(false)
  const json = JSON.stringify(ev.raw ?? {}, null, 2)
  return (
    <div className="row-body mono clickable" onClick={() => setExpanded(e => !e)}>
      {expanded
        ? <div className="expanded-body">{json}</div>
        : <div className="clamp-1">{json}</div>}
      <span className="expand-hint">{expanded ? 'collapse' : 'raw payload'}</span>
    </div>
  )
}

function TextBody({ ev, text }) {
  const [expanded, setExpanded] = useState(false)
  if (!text) return null
  const long = text.length > 280 || text.split('\n').length > 4
  const cls = [
    'row-body',
    MONO_KINDS.has(ev.kind) ? 'mono' : '',
    long ? 'clickable' : '',
  ].filter(Boolean).join(' ')
  return (
    <div className={cls} onClick={() => long && setExpanded(e => !e)}>
      <div className={expanded ? 'expanded-body' : long ? 'clamp-4' : ''}>{text}</div>
      {long && <span className="expand-hint">{expanded ? 'collapse' : 'expand'}</span>}
    </div>
  )
}
