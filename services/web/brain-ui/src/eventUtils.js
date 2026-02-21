// Stream event parsing utilities.
// Each raw WS message -> a parsed event object for the feed.

export const CONTEXT_WINDOW = 200_000

export const TOOL_CONFIG = {
  Read:       { icon: '📄', color: '#a78bfa', label: 'Read' },
  Write:      { icon: '✏️',  color: '#f472b6', label: 'Write' },
  Edit:       { icon: '🔧', color: '#fb923c', label: 'Edit' },
  Bash:       { icon: '⌨️',  color: '#4ade80', label: 'Bash' },
  Grep:       { icon: '🔍', color: '#fbbf24', label: 'Grep' },
  Glob:       { icon: '📁', color: '#fbbf24', label: 'Glob' },
  Task:       { icon: '🤖', color: '#22d3ee', label: 'Task' },
  TodoWrite:  { icon: '📋', color: '#a3e635', label: 'Todos' },
  WebSearch:  { icon: '🌐', color: '#f59e0b', label: 'Web Search' },
  WebFetch:   { icon: '🌐', color: '#f59e0b', label: 'Web Fetch' },
}
const DEFAULT_TOOL = { icon: '🔧', color: '#f59e0b', label: 'Tool' }

let _idCounter = 0
function nextId(ts) { return `${ts}-${++_idCounter}` }

/**
 * Parse a raw WS message into a display event, or null if not renderable.
 * The raw object may be a stream event {ts, event, channel_id, bead_id}
 * or a special envelope {type, ...}.
 */
export function parseStreamEvent(raw) {
  const { ts, event, channel_id = null, bead_id = null } = raw
  if (!event) return null

  const base = { id: nextId(ts), ts, channel_id, bead_id }

  if (event.type === 'ping') return null

  if (event.type === 'assistant') {
    const blocks = event.message?.content ?? []
    for (const block of blocks) {
      if (block.type === 'thinking' && block.thinking) {
        return { ...base, kind: 'thinking', text: block.thinking }
      }
      if (block.type === 'text' && block.text) {
        const cleaned = block.text.replace(/<br\s*\/?>/gi, '').trim()
        if (!cleaned) return null
        return { ...base, kind: 'thinking', text: cleaned }
      }
      if (block.type === 'tool_use') {
        const cfg = TOOL_CONFIG[block.name] ?? DEFAULT_TOOL
        return { ...base, kind: 'tool', tool: block.name, input: block.input ?? {}, ...cfg }
      }
    }
    return null // usage-only assistant event, handled separately
  }

  if (event.type === 'user') {
    const blocks = event.message?.content ?? []
    for (const block of blocks) {
      if (block.type === 'tool_result') {
        return { ...base, kind: 'result', content: normalizeContent(block.content) }
      }
      if (block.type === 'text' && block.text) {
        return { ...base, kind: 'nudge', text: block.text }
      }
    }
    return null
  }

  if (event.type === 'result') {
    return { ...base, kind: 'session_end', turns: event.num_turns }
  }

  if (event.type === 'system') {
    return { ...base, kind: 'system', subtype: event.subtype }
  }

  return null
}

/** Extract usage object from an assistant event, for context tracking. */
export function extractUsage(raw) {
  if (raw.event?.type !== 'assistant') return null
  return raw.event.message?.usage ?? null
}

/** Get a short text snippet from a parsed event (for bead card preview). */
export function getEventSnippet(ev) {
  if (ev.kind === 'thinking') return ev.text?.slice(0, 100) ?? ''
  if (ev.kind === 'tool') return `${ev.tool}: ${toolSummary(ev)}`
  if (ev.kind === 'result') return ev.content?.slice(0, 80) ?? ''
  return ''
}

function toolSummary(ev) {
  const i = ev.input
  switch (ev.tool) {
    case 'Bash':  return i.description || i.command?.slice(0, 60) || ''
    case 'Read':
    case 'Write':
    case 'Edit':  return basename(i.file_path || '')
    case 'Grep':  return i.pattern?.slice(0, 40) || ''
    case 'Glob':  return i.pattern?.slice(0, 40) || ''
    case 'Task':  return i.prompt?.slice(0, 60) || ''
    default:      return ''
  }
}

function basename(path) {
  return path.split('/').pop() || path
}

function normalizeContent(content) {
  if (content == null) return ''
  if (typeof content === 'string') return content
  if (Array.isArray(content)) {
    return content.map(c => c?.text ?? (typeof c === 'string' ? c : JSON.stringify(c))).join('\n')
  }
  return JSON.stringify(content, null, 2)
}
