import { useState } from 'react'
import { TOOL_CONFIG } from '../eventUtils'

// ---- Per-kind config ----

const THINKING = { icon: '💭', color: '#60a5fa', label: 'Thinking' }
const RESULT   = { icon: '📨', color: '#10b981', label: 'Result' }
const SYSTEM   = { icon: '⚡', color: '#8b5cf6', label: 'System' }
const SESSION  = { icon: '✅', color: '#8b5cf6', label: 'Complete' }
const BEAD_COLOR = '#22d3ee'

// ---- Helpers ----

function fmt(ts) {
  return new Date(ts).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' })
}

function truncate(str, n = 200) {
  if (!str || str.length <= n) return { text: str || '', clipped: false }
  return { text: str.slice(0, n), clipped: true }
}

function basename(path) { return path.split('/').pop() || path }
function dirname(path) {
  const parts = path.split('/')
  parts.pop()
  return parts.length ? parts.join('/') + '/' : ''
}

// ---- Tool body renderers ----

function FilePath({ path }) {
  return (
    <>
      <span className="file-dir">{dirname(path)}</span>
      <span className="file-name">{basename(path)}</span>
    </>
  )
}

function ToolBody({ tool, input, expanded }) {
  switch (tool) {
    case 'Read': {
      const { file_path = '', offset, limit } = input
      return (
        <div className="tool-body">
          <FilePath path={file_path} />
          {(offset != null || limit != null) && (
            <span className="tool-meta"> lines {offset ?? 0}&ndash;{(offset ?? 0) + (limit ?? '?')}</span>
          )}
        </div>
      )
    }
    case 'Write': {
      const { file_path = '', content = '' } = input
      return (
        <div className="tool-body">
          <FilePath path={file_path} />
          <span className="tool-meta"> {content.length} bytes</span>
        </div>
      )
    }
    case 'Edit': {
      const { file_path = '', old_string = '', new_string = '' } = input
      const maxLen = expanded ? Infinity : 80
      return (
        <div className="tool-body">
          <FilePath path={file_path} />
          <div className="diff-block">
            <div className="diff-del">- {old_string.slice(0, maxLen)}{old_string.length > maxLen ? '…' : ''}</div>
            <div className="diff-add">+ {new_string.slice(0, maxLen)}{new_string.length > maxLen ? '…' : ''}</div>
          </div>
        </div>
      )
    }
    case 'Bash': {
      const { command = '', description = '' } = input
      const maxLen = expanded ? Infinity : 300
      const cmd = command.slice(0, maxLen) + (command.length > maxLen ? '…' : '')
      return (
        <div className="tool-body">
          {description && <div className="tool-desc">{description}</div>}
          <div className="code-block"><span className="code-prompt">$ </span>{cmd}</div>
        </div>
      )
    }
    case 'Grep': {
      const { pattern = '', path = '.' } = input
      return (
        <div className="tool-body">
          <span className="search-pattern">{pattern.slice(0, 60)}</span>
          <span className="search-path"> in {path}</span>
        </div>
      )
    }
    case 'Glob': {
      const { pattern = '', path = '.' } = input
      return (
        <div className="tool-body">
          <span className="search-pattern">{pattern}</span>
          <span className="search-path"> in {path}</span>
        </div>
      )
    }
    case 'Task': {
      const { subagent_type = 'agent', prompt = '' } = input
      const maxLen = expanded ? Infinity : 150
      return (
        <div className="tool-body">
          <span className="task-agent">{subagent_type}</span>
          <span className="task-prompt"> {prompt.slice(0, maxLen)}{prompt.length > maxLen ? '…' : ''}</span>
        </div>
      )
    }
    case 'TodoWrite': {
      const todos = input.todos ?? []
      if (todos.length === 0) return <div className="tool-body tool-meta">Cleared todos</div>
      const icons = { completed: '✓', in_progress: '◐', pending: '○' }
      const show = expanded ? todos : todos.slice(0, 4)
      return (
        <div className="tool-body">
          <div className="todo-list">
            {show.map((t, i) => (
              <div key={i} className={`todo-item todo-item--${t.status}`}>
                <span className="todo-icon">{icons[t.status] ?? '○'}</span>
                {t.content?.slice(0, 80)}
              </div>
            ))}
            {!expanded && todos.length > 4 && (
              <div className="tool-meta">+{todos.length - 4} more</div>
            )}
          </div>
        </div>
      )
    }
    default: {
      const json = JSON.stringify(input, null, 2)
      return (
        <div className="tool-body">
          <div className="code-block">{expanded ? json : json.slice(0, 200)}</div>
        </div>
      )
    }
  }
}

// ---- Main component ----

export default function EventCard({ event }) {
  const [expanded, setExpanded] = useState(false)

  let color, icon, label, body

  switch (event.kind) {
    case 'thinking': {
      ;({ color, icon, label } = THINKING)
      const { text, clipped } = truncate(event.text, expanded ? Infinity : 300)
      body = <div className="thinking-text">{text}{clipped && !expanded && '…'}</div>
      break
    }
    case 'tool': {
      const cfg = TOOL_CONFIG[event.tool] ?? { icon: '🔧', color: '#f59e0b', label: event.tool }
      ;({ color, icon, label } = cfg)
      body = <ToolBody tool={event.tool} input={event.input} expanded={expanded} />
      break
    }
    case 'result': {
      ;({ color, icon, label } = RESULT)
      const { text, clipped } = truncate(event.content, expanded ? Infinity : 200)
      body = <div className="result-text">{text}{clipped && !expanded && '…'}</div>
      break
    }
    case 'system':
      ;({ color, icon, label } = SYSTEM)
      body = (
        <div>
          <div className="tool-meta">{event.subtype === 'init' ? 'Session started' : event.subtype}</div>
          {event.nudgeText && <div className="nudge-text">{event.nudgeText}</div>}
        </div>
      )
      break
    case 'session_end':
      ;({ color, icon, label } = SESSION)
      body = <div className="tool-meta">Session complete &mdash; {event.turns ?? '?'} turns</div>
      break
    default:
      return null
  }

  // Bead events get a distinct accent
  const borderColor = event.bead_id ? BEAD_COLOR : color

  return (
    <div
      className={`event-card${expanded ? ' event-card--expanded' : ''}`}
      style={{ borderLeftColor: borderColor }}
      onClick={() => setExpanded(x => !x)}
    >
      <div className="event-card-header">
        <span className="event-icon">{icon}</span>
        <span className="event-label" style={{ color }}>{label}</span>
        {event.bead_id && <span className="bead-badge">bead</span>}
        <span className="event-time">{fmt(event.ts)}</span>
      </div>
      <div className="event-card-body">{body}</div>
    </div>
  )
}
