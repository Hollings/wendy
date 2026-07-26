// One feed row. Rendering is driven by the KINDS registry (label/icon/tone)
// plus per-kind body components below. Tool rows carry their paired output
// (attached by deriveFeed) and render call + response as a single card.

import { useState } from 'react'
import {
  rowSpec, bodyText, toolPreview, clockTime, channelColor,
  formatDuration, parseMsgsOutput, reactionEmoji,
} from '../events'
import { TOOL_DETAIL } from './ToolDetails'
import Markdown from './Markdown'
import Icon from './Icon'

// Prose kinds get the markdown treatment; everything else stays plain text.
const MARKDOWN_KINDS = new Set(['thinking', 'speech', 'discord'])
// Kinds rendered in the mono face (machine output rather than prose).
const MONO_KINDS = new Set(['result', 'task', 'rate_limit', 'unknown'])

export default function EventRow({ ev, channelsMap }) {
  const spec = rowSpec(ev)

  if (spec.divider) {
    return (
      <div className={`divider tone-${spec.tone}`}>
        <span className="rule" />
        <Icon name={spec.icon} size={11} />
        <span>{spec.label}</span>
        <SourceTag ev={ev} channelsMap={channelsMap} />
        <span className="ts" title={new Date(ev.ts).toLocaleString()}>{clockTime(ev.ts)}</span>
        <span className="rule" />
      </div>
    )
  }

  const pending = ev.mergeOpen || ev.awaiting
  return (
    <div className={`row tone-${spec.tone}${pending ? ' pending' : ''}${ev.kind === 'discord' ? ' discord' : ''}`}>
      <span className="row-icon"><Icon name={spec.icon} size={13} /></span>
      <div className="row-main">
        <div className="row-head">
          <span className="row-label">{spec.label}</span>
          {ev.stackCount > 1 && <span className="stack" title="consecutive identical calls">×{ev.stackCount}</span>}
          {ev.durationMs != null && ev.output && (
            <span className="dur">{formatDuration(ev.durationMs)}</span>
          )}
          {ev.blocked && <span className="blocked-chip">blocked</span>}
          <SourceTag ev={ev} channelsMap={channelsMap} />
          <span className="ts" title={new Date(ev.ts).toLocaleString()}>{clockTime(ev.ts)}</span>
        </div>
        <RowBody ev={ev} />
      </div>
    </div>
  )
}

export function GapRow({ ev }) {
  return <div className="gap-row">{formatDuration(ev.ms)} quiet</div>
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
  switch (ev.kind) {
    case 'discord': return ev.sub === 'reaction' ? <ReactionBody ev={ev} /> : <DiscordBody ev={ev} />
    case 'tool':    return ev.flavor === 'msgs' ? <MsgsBody ev={ev} /> : <ToolBody ev={ev} />
    case 'unknown': return <UnknownBody ev={ev} />
    default:        return <TextBody ev={ev} text={bodyText(ev)} />
  }
}

// ---------------------------------------------------------------------------
// Discord: outgoing message / reaction
// ---------------------------------------------------------------------------

function DiscordBody({ ev }) {
  const [expanded, setExpanded] = useState(false)
  return (
    <div className="row-body">
      <div className={`bubble${ev.blocked ? ' blocked' : ''}`}>
        {ev.replyTo && <div className="bubble-meta">reply to {ev.replyTo}</div>}
        {ev.text
          ? <Markdown text={ev.text} />
          : <span className="dim">{ev.awaiting ? 'sending…' : '(content not captured)'}</span>}
        {ev.attachment && <div className="bubble-meta"><Icon name="Attach" size={11} /> {ev.attachment}</div>}
      </div>
      {ev.blocked && ev.output && (
        <div className="out-block error clickable" onClick={() => setExpanded(e => !e)}>
          <div className={expanded ? '' : 'clamp-2'}>{ev.output.content}</div>
        </div>
      )}
    </div>
  )
}

function ReactionBody({ ev }) {
  const failed = ev.output?.isError
  return (
    <div className="row-body reaction">
      <span className="reaction-emoji">{reactionEmoji(ev.emoji)}</span>
      <span className="dim"> on msg …{String(ev.targetId ?? '').slice(-6)}</span>
      {failed && <span className="blocked-chip">failed</span>}
    </div>
  )
}

// ---------------------------------------------------------------------------
// msgs: incoming-message check. Kept compact -- the user can already read
// these messages in Discord; this card only shows that a check happened and
// roughly what came back.
// ---------------------------------------------------------------------------

function MsgsBody({ ev }) {
  const [expanded, setExpanded] = useState(false)
  if (!ev.output) {
    return <div className="row-body mono dim">{ev.awaiting ? 'checking…' : toolPreview('Bash', ev.input)}</div>
  }
  const entries = parseMsgsOutput(ev.output.content)
  if (entries == null) {
    // Unparseable (error, --raw, changed format): fall back to raw text.
    return <TextBody ev={{ kind: 'result' }} text={ev.output.content} />
  }
  if (entries.length === 0) {
    return <div className="row-body mono dim">no new messages</div>
  }
  const first = entries[0]
  return (
    <div className="row-body mono clickable" onClick={() => setExpanded(e => !e)}>
      {!expanded && (
        <div className="clamp-1 dim">
          {entries.length} new · <span className="msgs-author">{first.author}</span>: {first.text}
        </div>
      )}
      {expanded && (
        <div className="expanded-body">
          {entries.map((m, i) => (
            <div key={i} className="msgs-line">
              <span className="msgs-time">{m.time}</span>
              <span className="msgs-author">{m.author}{m.synthetic ? ' (system)' : ''}</span>
              <span className="msgs-text">{m.text}</span>
              {m.attachments.map((a, j) => <div key={j} className="bubble-meta">attachment: {a}</div>)}
            </div>
          ))}
        </div>
      )}
    </div>
  )
}

// ---------------------------------------------------------------------------
// Generic tool call + paired output
// ---------------------------------------------------------------------------

function ToolBody({ ev }) {
  const [expanded, setExpanded] = useState(false)
  const preview = toolPreview(ev.tool, ev.input)
  const Detail = TOOL_DETAIL[ev.tool]
  const out = ev.output
  return (
    <div className="row-body mono clickable" onClick={() => setExpanded(e => !e)}>
      {!expanded && <div className="clamp-1">{preview || '(no input)'}</div>}
      {expanded && (
        <div className="expanded-body">
          {Detail ? <Detail ev={ev} /> : <KvDump input={ev.input} />}
        </div>
      )}
      {out && (
        <div className={`out-block${out.isError ? ' error' : ''}`}>
          <div className={expanded ? 'expanded-out' : 'clamp-2'}>{out.content || '(no output)'}</div>
        </div>
      )}
    </div>
  )
}

function KvDump({ input }) {
  const entries = Object.entries(input ?? {})
  return entries.map(([k, v]) => (
    <div key={k} className="kv">
      <span className="k">{k}</span>
      <span className="v">{typeof v === 'object' ? JSON.stringify(v, null, 2) : String(v)}</span>
    </div>
  ))
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
  const markdown = MARKDOWN_KINDS.has(ev.kind)
  const long = text.length > 280 || text.split('\n').length > 4
  const cls = [
    'row-body',
    MONO_KINDS.has(ev.kind) ? 'mono' : '',
    long ? 'clickable' : '',
  ].filter(Boolean).join(' ')
  const content = markdown ? <Markdown text={text} /> : text
  return (
    <div className={cls} onClick={() => long && setExpanded(e => !e)}>
      <div className={expanded ? 'expanded-body' : long ? 'clamp-4' : ''}>{content}</div>
      {long && <span className="expand-hint">{expanded ? 'collapse' : 'expand'}</span>}
    </div>
  )
}
