// Event parsing and display registries.
//
// Everything the feed renders is driven by data in this file:
//   - parseFrame() turns a raw WS frame into zero or more display events
//   - KINDS maps an event kind to its label/icon/tone
//   - toolPreview() summarizes tool inputs for collapsed rows
//
// Adding support for a new tool or event kind means adding an entry to a
// table here -- no component changes.

const DEFAULT_CONTEXT_WINDOW = 200_000

// Per-model context window sizes, matched by model-ID prefix. Sonnet 5 runs a
// 1M window; everything else falls back to 200k.
const CONTEXT_WINDOWS = {
  'claude-sonnet-5': 1_000_000,
}

/** Context window (tokens) for a model ID, defaulting to 200k. */
export function contextWindowFor(model) {
  if (model) {
    for (const [prefix, window] of Object.entries(CONTEXT_WINDOWS)) {
      if (model.startsWith(prefix)) return window
    }
  }
  return DEFAULT_CONTEXT_WINDOW
}

// system-event subtypes the CLI emits mid-turn (incremental token counters)
// that carry no feed value. Dropped before they become rows.
const NOISE_SYSTEM_SUBTYPES = new Set(['thinking_tokens'])

// ---------------------------------------------------------------------------
// Dedup key: content-based hash of the raw frame.
// The same event can arrive twice (once in the 50-event replay on connect,
// once live). The frame embeds a ms timestamp, so identical strings are the
// same event for all practical purposes.
// ---------------------------------------------------------------------------

export function frameKey(rawString) {
  let h = 5381
  for (let i = 0; i < rawString.length; i++) {
    h = ((h * 33) ^ rawString.charCodeAt(i)) >>> 0
  }
  return `${rawString.length}-${h.toString(36)}`
}

// ---------------------------------------------------------------------------
// Kind registry: how each parsed event kind renders.
// tone maps to CSS classes (.row.tone-*), divider kinds render as rules.
// ---------------------------------------------------------------------------

export const KINDS = {
  thinking:    { label: () => 'thought',      icon: () => 'Thinking', tone: 'thinking' },
  tool:        { label: ev => ev.tool,        icon: ev => ev.tool,    tone: 'tool' },
  result:      { label: () => 'result',       icon: () => 'Result',   tone: 'result' },
  nudge:       { label: () => 'nudge',        icon: () => 'System',   tone: 'nudge' },
  system:      { label: ev => ev.subtype === 'init' ? 'session start' : (ev.subtype || 'system'),
                 icon: () => 'System', tone: 'system', divider: true },
  session_end: { label: ev => `turn ended${ev.turns != null ? ` · ${ev.turns} turns` : ''}`,
                 icon: () => 'End', tone: 'system', divider: true },
}

// Feed filter groups: which kinds each filter chip controls.
export const FILTER_GROUPS = [
  { id: 'thinking', label: 'thoughts', kinds: ['thinking'] },
  { id: 'tool',     label: 'tools',    kinds: ['tool'] },
  { id: 'result',   label: 'results',  kinds: ['result'] },
  { id: 'system',   label: 'system',   kinds: ['system', 'session_end', 'nudge'] },
]

// ---------------------------------------------------------------------------
// Tool input previews (collapsed row summary), keyed by tool name.
// Fallback shows the first input entry.
// ---------------------------------------------------------------------------

const TOOL_PREVIEW = {
  Bash:      i => i.description || i.command,
  Read:      i => i.file_path,
  Write:     i => i.file_path,
  Edit:      i => i.file_path,
  Grep:      i => i.pattern,
  Glob:      i => i.pattern,
  Task:      i => i.description || i.prompt,
  TodoWrite: i => (i.todos ?? []).map(t => t.subject ?? t.content).filter(Boolean).join(' · '),
  WebSearch: i => i.query,
  WebFetch:  i => i.url,
}

export function toolPreview(tool, input = {}) {
  const fn = TOOL_PREVIEW[tool]
  if (fn) {
    const v = fn(input)
    if (v) return String(v)
  }
  const first = Object.entries(input)[0]
  if (!first) return ''
  const [k, v] = first
  return `${k}: ${typeof v === 'object' ? JSON.stringify(v) : String(v)}`
}

// ---------------------------------------------------------------------------
// Frame parsing
// ---------------------------------------------------------------------------

/**
 * Parse a raw WS stream frame {ts, event, channel_id?, bead_id?} into a list
 * of display events. An assistant message can contain several blocks
 * (thinking + tool_use), so one frame may produce multiple rows.
 */
export function parseFrame(raw, key) {
  const { ts, event, channel_id = null, bead_id = null } = raw
  if (!event || event.type === 'ping') return []

  const base = (i) => ({ id: `${key}-${i}`, ts: toMs(ts), channel_id, bead_id })
  const out = []

  if (event.type === 'assistant') {
    const blocks = event.message?.content ?? []
    blocks.forEach((block, i) => {
      if (block.type === 'thinking' && block.thinking) {
        out.push({ ...base(i), kind: 'thinking', text: block.thinking })
      } else if (block.type === 'text' && block.text) {
        const cleaned = block.text.replace(/<br\s*\/?>/gi, '').trim()
        if (cleaned) out.push({ ...base(i), kind: 'thinking', text: cleaned })
      } else if (block.type === 'tool_use') {
        out.push({ ...base(i), kind: 'tool', tool: block.name, input: block.input ?? {} })
      }
    })
    return out
  }

  if (event.type === 'user') {
    const blocks = Array.isArray(event.message?.content) ? event.message.content : []
    blocks.forEach((block, i) => {
      if (block.type === 'tool_result') {
        out.push({ ...base(i), kind: 'result', content: normalizeContent(block.content) })
      } else if (block.type === 'text' && block.text) {
        out.push({ ...base(i), kind: 'nudge', text: block.text })
      }
    })
    return out
  }

  if (event.type === 'result') {
    return [{ ...base(0), kind: 'session_end', turns: event.num_turns }]
  }

  if (event.type === 'system') {
    // Mid-turn token counters are noise, not feed-worthy session events.
    if (NOISE_SYSTEM_SUBTYPES.has(event.subtype)) return []
    return [{ ...base(0), kind: 'system', subtype: event.subtype }]
  }

  return []
}

/** Context usage from an assistant frame, or null. */
export function frameUsage(raw) {
  if (raw.event?.type !== 'assistant') return null
  const usage = raw.event.message?.usage
  if (!usage) return null
  return (usage.cache_read_input_tokens ?? 0) + (usage.input_tokens ?? 0)
}

/** Model ID from an assistant frame, or null. Used to size the context window. */
export function frameModel(raw) {
  if (raw.event?.type !== 'assistant') return null
  return raw.event.message?.model ?? null
}

/** Short text snippet for bead cards. */
export function eventSnippet(ev) {
  if (ev.kind === 'thinking') return ev.text?.slice(0, 100) ?? ''
  if (ev.kind === 'tool') return `${ev.tool}: ${toolPreview(ev.tool, ev.input)}`.slice(0, 100)
  if (ev.kind === 'result') return ev.content?.slice(0, 80) ?? ''
  return ''
}

// ts arrives as epoch-ms int (bot writes) or ISO string (other sources)
function toMs(ts) {
  if (typeof ts === 'number') return ts
  const ms = new Date(ts).getTime()
  return Number.isFinite(ms) ? ms : Date.now()
}

function normalizeContent(content) {
  if (content == null) return ''
  if (typeof content === 'string') return content
  if (Array.isArray(content)) {
    return content.map(c => c?.text ?? (typeof c === 'string' ? c : JSON.stringify(c))).join('\n')
  }
  return JSON.stringify(content, null, 2)
}

// ---------------------------------------------------------------------------
// Small shared formatting helpers
// ---------------------------------------------------------------------------

const numFmt = new Intl.NumberFormat('en-US')

export function formatTokens(n) {
  if (!n) return '0'
  if (n < 1000) return String(n)
  return (n / 1000).toFixed(n < 10_000 ? 1 : 0) + 'k'
}

export function formatNumber(n) {
  return numFmt.format(n ?? 0)
}

export function agoShort(tsMs) {
  if (!tsMs) return '—'
  const s = Math.max(0, Math.floor((Date.now() - tsMs) / 1000))
  if (s < 60) return s + 's'
  const m = Math.floor(s / 60)
  if (m < 60) return m + 'm'
  const h = Math.floor(m / 60)
  if (h < 24) return h + 'h'
  return Math.floor(h / 24) + 'd'
}

export function clockTime(tsMs) {
  return new Date(tsMs).toLocaleTimeString('en-GB', {
    hour: '2-digit', minute: '2-digit', second: '2-digit',
  })
}

// Stable per-channel hue from a small curated palette.
const HUES = [145, 220, 78, 305, 18, 175, 260, 40, 330, 200]

export function channelHue(channelId) {
  if (!channelId) return 145
  let h = 0
  const s = String(channelId)
  for (let i = 0; i < s.length; i++) h = ((h << 5) - h + s.charCodeAt(i)) | 0
  return HUES[Math.abs(h) % HUES.length]
}

export function channelColor(channelId) {
  return `oklch(0.78 0.15 ${channelHue(channelId)})`
}
