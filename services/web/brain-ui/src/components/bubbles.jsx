import { useState } from 'react'
import Icon from './Icon'

function shortTime(ts) {
  const d = new Date(ts)
  return d.toLocaleTimeString('en-GB', { hour: '2-digit', minute: '2-digit', second: '2-digit' })
}

function ChannelDot({ channel }) {
  if (!channel) return null
  return (
    <>
      <span className="channel-dot" style={{ color: channel.color }} aria-hidden />
      <span>#{channel.name}</span>
    </>
  )
}

export function ThinkingBubble({ event, channel }) {
  const chStyle = channel ? { '--ch-color': channel.color } : {}
  return (
    <div className="event" style={chStyle}>
      <span className={'node thinking' + (channel ? ' has-channel' : '')}>
        <Icon name="Thinking" size={10} />
      </span>
      <div className="bubble thinking">
        <div className="bubble-head">
          <ChannelDot channel={channel} />
          <span className="kind">thought</span>
          <span className="ts mono">{shortTime(event.ts)}</span>
        </div>
        <div className="body">{event.text}</div>
      </div>
    </div>
  )
}

function describeToolInput(tool, input = {}) {
  switch (tool) {
    case 'Read':
    case 'Write':
    case 'Edit':
      return ['file_path', input.file_path ?? '']
    case 'Bash':
      return ['cmd', input.command ?? input.description ?? '']
    case 'Grep':
    case 'Glob':
      return ['pattern', input.pattern ?? '']
    case 'Task':
      return ['task', (input.prompt || input.description || '').slice(0, 120)]
    case 'WebFetch':
    case 'WebSearch':
      return ['url', input.url ?? input.query ?? '']
    default: {
      const entries = Object.entries(input)
      if (!entries.length) return null
      const [k, v] = entries[0]
      return [k, typeof v === 'object' ? JSON.stringify(v) : String(v)]
    }
  }
}

export function ToolBubble({ event, channel }) {
  const [expanded, setExpanded] = useState(false)
  const input = event.input || {}
  const entries = Object.entries(input)
  const first = describeToolInput(event.tool, input)
  const extra = Math.max(0, entries.length - 1)
  const chStyle = channel ? { '--ch-color': channel.color } : {}
  return (
    <div className="event" style={chStyle}>
      <span className={'node tool' + (channel ? ' has-channel' : '')}>
        <Icon name="Tool" size={10} />
      </span>
      <div className="bubble tool" onClick={() => setExpanded(e => !e)}>
        <div className="bubble-head">
          <span className="kind" style={{ color: 'var(--accent-ink)', background: 'var(--accent-bg)' }}>tool call</span>
          <ChannelDot channel={channel} />
          <span className="ts mono">{shortTime(event.ts)}</span>
        </div>
        <div className="tool-row">
          <span className="tool-icon"><Icon name={event.tool} size={13} /></span>
          <span className="tool-name">{event.tool}</span>
          <span className="expand-hint">{expanded ? '▾ hide' : '▸ expand'}</span>
        </div>
        {first && !expanded && (
          <div className="tool-args mono collapsed">
            <span className="k">{first[0]}</span>
            <span className="v truncate">
              {String(first[1]).length > 64 ? '…' + String(first[1]).slice(-62) : String(first[1])}
            </span>
            {extra > 0 && <span className="k">+{extra}</span>}
          </div>
        )}
        {expanded && entries.length > 0 && (
          <div className="tool-args mono expanded">
            {entries.map(([k, v], i) => (
              <div key={i}>
                <span className="k">{k}: </span>
                <span className="v">{typeof v === 'object' ? JSON.stringify(v, null, 2) : String(v)}</span>
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  )
}

export function ResultBubble({ event, channel }) {
  const [expanded, setExpanded] = useState(false)
  const body = event.content ?? ''
  const needsExpand = body.length > 80 || body.split('\n').length > 3
  const chStyle = channel ? { '--ch-color': channel.color } : {}
  return (
    <div className="event" style={chStyle}>
      <span className={'node result' + (channel ? ' has-channel' : '')}>
        <Icon name="Result" size={10} />
      </span>
      <div className="bubble result" onClick={() => needsExpand && setExpanded(e => !e)}>
        <div className="bubble-head">
          <span className="result-tag">
            <Icon name="Result" size={10} /> result
          </span>
          <ChannelDot channel={channel} />
          <span className="ts mono">{shortTime(event.ts)}</span>
          {needsExpand && <span className="expand-hint">{expanded ? '▾ hide' : '▸ expand'}</span>}
        </div>
        <div className={'body ' + (expanded || !needsExpand ? '' : 'collapsed')}>{body}</div>
      </div>
    </div>
  )
}

export function SystemLine({ event }) {
  const isEnd = event.kind === 'session_end'
  const text = isEnd
    ? `turn ended · ${event.turns ?? '?'} turns`
    : event.subtype === 'init' ? 'session init' : (event.subtype || 'system')
  return (
    <div className={'event system ' + (isEnd ? 'end' : '')}>
      <div className="sys-line">
        <span className="rule" />
        <Icon name={isEnd ? 'End' : 'System'} size={12} />
        <span>{text}</span>
        <span className="ts">{shortTime(event.ts)}</span>
        <span className="rule" />
      </div>
    </div>
  )
}
