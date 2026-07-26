// Event parsing and display registries.
//
// Everything the feed renders is driven by data in this file:
//   - parseFrame()   turns a raw WS frame into zero or more display events
//   - appendEvents() folds new events into the list, collapsing repeats
//   - KINDS          maps an event kind to its label/icon/tone
//   - rowSpec()      resolves a kind's registry entry for a given event
//   - toolPreview()  summarizes tool inputs for collapsed rows
//
// Adding support for a new tool or event kind means adding an entry to a
// table here -- no component changes.
//
// parseFrame is deliberately exhaustive: anything it does not recognize
// becomes an `unknown` row rather than being dropped. The one exception is
// `stream_event` (partial-message deltas), which is a duplicate of the
// assistant frame that follows it.

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

// ---------------------------------------------------------------------------
// Dedup key: content-based hash of the raw frame.
// The same event can arrive twice (once in the replay on connect, once live).
// The frame embeds a ms timestamp, so identical strings are the same event
// for all practical purposes.
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
// tone maps to CSS classes (.row.tone-* / .divider.tone-*); divider kinds
// render as horizontal rules instead of rows. label/icon/tone may each be a
// plain value or a function of the event -- use rowSpec() to resolve them.
// ---------------------------------------------------------------------------

export const KINDS = {
  // A thought. On models that expose reasoning text this carries `text`; on
  // models that return encrypted thinking (Sonnet 5+) only the running token
  // count from system/thinking_tokens is available, so the row stays a
  // counter and is marked `redacted` once the block closes.
  thinking: {
    label: ev => (ev.mergeOpen ? 'thinking…' : ev.redacted ? 'thought · encrypted' : 'thought'),
    icon: 'Thinking',
    tone: 'thinking',
  },
  speech:       { label: 'says',        icon: 'Speech',  tone: 'speech' },
  tool:         { label: ev => ev.tool, icon: ev => ev.tool, tone: 'tool' },
  result:       { label: ev => (ev.isError ? 'tool error' : 'result'),
                  icon: 'Result', tone: ev => (ev.isError ? 'error' : 'result') },
  nudge:        { label: 'nudge',       icon: 'System',  tone: 'nudge' },
  task:         { label: ev => `task ${ev.status}`, icon: 'Task',
                  tone: ev => (ev.status === 'failed' ? 'error' : 'task') },
  notification: { label: ev => ev.notifKey || 'notification', icon: 'Bell', tone: 'nudge' },
  rate_limit:   { label: ev => `rate limit · ${ev.limitType || 'window'}`, icon: 'Limit',
                  tone: ev => (ev.status && ev.status !== 'allowed' ? 'error' : 'limit') },
  unknown:      { label: ev => ev.eventType || 'unknown event', icon: 'Unknown', tone: 'unknown' },

  // Dividers -- turn/session boundaries, rendered as a labelled rule.
  // system/init fires once per `claude -p` invocation, i.e. once per turn --
  // not once per Claude session, which outlives many turns via --resume.
  session_start: { label: ev => `turn start${ev.model ? ` · ${ev.model}` : ''}`,
                   icon: 'System', tone: 'system', divider: true },
  session_end:   { label: sessionEndLabel, icon: 'End',
                   tone: ev => (ev.isError ? 'error' : 'system'), divider: true },
  status:        { label: ev => ev.status || 'status', icon: 'Status', tone: 'system', divider: true },
  compact:       { label: compactLabel, icon: 'Compact', tone: 'system', divider: true },
}

function sessionEndLabel(ev) {
  const bits = [ev.isError ? `turn failed${ev.subtype ? ` · ${ev.subtype}` : ''}` : 'turn ended']
  if (ev.turns != null) bits.push(`${ev.turns} turns`)
  if (ev.durationMs) bits.push(formatDuration(ev.durationMs))
  if (ev.cost) bits.push(`$${ev.cost < 0.01 ? ev.cost.toFixed(4) : ev.cost.toFixed(2)}`)
  return bits.join(' · ')
}

function compactLabel(ev) {
  const bits = [`compacted${ev.trigger ? ` · ${ev.trigger}` : ''}`]
  if (ev.preTokens && ev.postTokens) {
    bits.push(`${formatTokens(ev.preTokens)} → ${formatTokens(ev.postTokens)}`)
  }
  if (ev.durationMs) bits.push(formatDuration(ev.durationMs))
  return bits.join(' · ')
}

/** Resolve a kind's registry entry against an event. Never returns null. */
export function rowSpec(ev) {
  const cfg = KINDS[ev.kind] ?? KINDS.unknown
  return {
    label: String(resolve(cfg.label, ev) ?? ev.kind),
    icon: resolve(cfg.icon, ev) ?? 'Unknown',
    tone: resolve(cfg.tone, ev) ?? 'system',
    divider: !!cfg.divider,
  }
}

const resolve = (v, ev) => (typeof v === 'function' ? v(ev) : v)

// Feed filter groups: which kinds each filter chip controls.
export const FILTER_GROUPS = [
  { id: 'thinking', label: 'thoughts', kinds: ['thinking'] },
  { id: 'speech',   label: 'speech',   kinds: ['speech'] },
  { id: 'tool',     label: 'tools',    kinds: ['tool'] },
  { id: 'result',   label: 'results',  kinds: ['result'] },
  { id: 'task',     label: 'tasks',    kinds: ['task'] },
  { id: 'system',   label: 'system',
    kinds: ['session_start', 'session_end', 'status', 'compact',
            'nudge', 'notification', 'rate_limit', 'unknown'] },
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
  if (!event || typeof event !== 'object') return []
  if (event.type === 'ping') return []

  // Partial-message deltas duplicate the assistant frame that follows them.
  if (event.type === 'stream_event') return []

  const base = (i) => ({ id: `${key}-${i}`, ts: toMs(ts), channel_id, bead_id })

  switch (event.type) {
    case 'assistant': return parseAssistant(event, base)
    case 'user':      return parseUser(event, base)
    case 'result':    return [parseResult(event, base(0))]
    case 'system':    return [parseSystem(event, base(0))]
    case 'rate_limit_event': {
      const info = event.rate_limit_info ?? {}
      return [{
        ...base(0),
        kind: 'rate_limit',
        status: info.status ?? 'unknown',
        limitType: info.rateLimitType ?? null,
        resetsAt: info.resetsAt ?? null,
        usingOverage: !!info.isUsingOverage,
        // Fires once per request; collapse the identical repeats.
        mergeKey: 'rate_limit',
        mergeOpen: true,
      }]
    }
    default:
      return [unknownRow(base(0), event.type, event)]
  }
}

function parseAssistant(event, base) {
  const content = event.message?.content
  if (typeof content === 'string') {
    const text = cleanText(content)
    return text ? [{ ...base(0), kind: 'speech', text }] : []
  }
  const out = []
  for (const [i, block] of (Array.isArray(content) ? content : []).entries()) {
    if (!block || typeof block !== 'object') continue
    switch (block.type) {
      case 'thinking':
      case 'redacted_thinking': {
        const text = (block.thinking ?? '').trim()
        // Closes whatever thinking row the token counters opened. Sonnet 5+
        // returns encrypted thinking (signature only, empty text), so the
        // counter is the whole story -- say so rather than dropping the block.
        out.push({
          ...base(i),
          kind: 'thinking',
          text,
          redacted: !text,
          mergeKey: 'thinking',
          mergeOpen: false,
        })
        break
      }
      case 'text': {
        const text = cleanText(block.text)
        if (text) out.push({ ...base(i), kind: 'speech', text })
        break
      }
      case 'tool_use':
      case 'server_tool_use':
        out.push({ ...base(i), kind: 'tool', tool: block.name ?? 'tool', input: block.input ?? {} })
        break
      default:
        out.push(unknownRow(base(i), `assistant/${block.type}`, block))
    }
  }
  return out
}

function parseUser(event, base) {
  const content = event.message?.content
  if (typeof content === 'string') {
    const text = content.trim()
    return text ? [{ ...base(0), kind: 'nudge', text }] : []
  }
  const out = []
  for (const [i, block] of (Array.isArray(content) ? content : []).entries()) {
    if (!block || typeof block !== 'object') continue
    if (block.type === 'tool_result') {
      out.push({
        ...base(i),
        kind: 'result',
        content: normalizeContent(block.content),
        isError: !!block.is_error,
      })
    } else if (block.type === 'text') {
      const text = (block.text ?? '').trim()
      if (text) out.push({ ...base(i), kind: 'nudge', text })
    } else {
      out.push(unknownRow(base(i), `user/${block.type}`, block))
    }
  }
  return out
}

function parseResult(event, base) {
  return {
    ...base,
    kind: 'session_end',
    subtype: event.subtype ?? null,
    turns: event.num_turns ?? null,
    durationMs: event.duration_ms ?? null,
    cost: event.total_cost_usd ?? null,
    stopReason: event.stop_reason ?? null,
    isError: !!event.is_error || (event.subtype != null && event.subtype !== 'success'),
    text: typeof event.result === 'string' ? event.result : '',
  }
}

// system-event subtypes get a dedicated shape; anything else falls through to
// a generic `system` divider carrying the raw payload.
const SYSTEM_PARSERS = {
  init: (e, base) => ({ ...base, kind: 'session_start', model: e.model ?? null }),
  // Emitted continuously while the model reasons. On encrypted-thinking models
  // this is the only visible trace of a thought, so it opens a live row that
  // later counters merge into and the assistant frame closes.
  thinking_tokens: (e, base) => ({
    ...base, kind: 'thinking',
    tokens: e.estimated_tokens ?? 0, text: '',
    mergeKey: 'thinking', mergeOpen: true,
  }),
  task_started: (e, base) => ({
    ...base, kind: 'task', status: 'started',
    taskId: e.task_id ?? null, taskType: e.task_type ?? null, text: e.description ?? '',
  }),
  task_notification: (e, base) => ({
    ...base, kind: 'task', status: e.status ?? 'update',
    taskId: e.task_id ?? null, taskType: null, text: e.summary ?? '',
  }),
  notification: (e, base) => ({
    ...base, kind: 'notification',
    text: e.text ?? '', notifKey: e.key ?? null, priority: e.priority ?? null,
  }),
  status: (e, base) => ({ ...base, kind: 'status', status: e.status ?? 'unknown' }),
  compact_boundary: (e, base) => {
    const meta = e.compact_metadata ?? {}
    return {
      ...base, kind: 'compact',
      trigger: meta.trigger ?? null,
      preTokens: meta.pre_tokens ?? null,
      postTokens: meta.post_tokens ?? null,
      durationMs: meta.duration_ms ?? null,
    }
  },
}

function parseSystem(event, base) {
  const parser = SYSTEM_PARSERS[event.subtype]
  if (parser) return parser(event, base)
  // A subtype the CLI added since this file was written. Route it through the
  // unknown row so the raw payload is on screen the day it first appears --
  // dividers have no body and would hide it.
  return unknownRow(base, `system/${event.subtype ?? 'system'}`, event)
}

function unknownRow(base, eventType, raw) {
  return { ...base, kind: 'unknown', eventType: String(eventType ?? 'unknown'), raw }
}

// ---------------------------------------------------------------------------
// Appending: collapse high-frequency repeats instead of flooding the feed.
//
// An event carrying `mergeKey` folds into the most recent event from the same
// source (channel or bead) when that event shares the key and is still open.
// Thinking-token counters open a row that later counters update and the
// assistant frame closes; rate-limit pings collapse into a single row.
// ---------------------------------------------------------------------------

export function appendEvents(prev, incoming) {
  let out = prev
  for (const ev of incoming) out = appendOne(out, ev)
  return out
}

function appendOne(list, ev) {
  const idx = lastIndexFromSource(list, ev)
  const target = idx === -1 ? null : list[idx]
  const open = target?.mergeOpen ? target : null

  if (ev.mergeKey && open?.mergeKey === ev.mergeKey) {
    const next = list.slice()
    next[idx] = mergePatch(open, ev)
    return next
  }
  // Anything else from the same source ends the open row -- a row only stays
  // open while it is the newest thing that source has said.
  if (open) {
    const next = list.slice()
    next[idx] = { ...open, mergeOpen: false }
    return next.concat(ev)
  }
  return list.concat(ev)
}

/** Index of the last event sharing this event's channel/bead, or -1. */
function lastIndexFromSource(list, ev) {
  for (let i = list.length - 1; i >= 0; i--) {
    if (list[i].channel_id === ev.channel_id && list[i].bead_id === ev.bead_id) return i
  }
  return -1
}

// Keeps the original row id (stable React key) and never lets an absent field
// on the incoming event erase what the row already knows -- a closing thinking
// block has no token count, but the row it closes does.
function mergePatch(target, patch) {
  const next = { ...target, repeats: (target.repeats ?? 1) + 1 }
  for (const [k, v] of Object.entries(patch)) {
    if (k === 'id' || k === 'repeats') continue
    if (v === undefined || v === null || v === '') continue
    next[k] = v
  }
  next.mergeOpen = patch.mergeOpen
  if (patch.redacted !== undefined) next.redacted = patch.redacted
  return next
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

// ---------------------------------------------------------------------------
// Row bodies
// ---------------------------------------------------------------------------

/**
 * The plain-text body for a row, or '' for kinds that render a custom body
 * (tool inputs, unknown payloads). Used by the feed and by bead snippets.
 */
export function bodyText(ev) {
  switch (ev.kind) {
    case 'thinking': {
      if (ev.text) return ev.text
      const tokens = `${formatTokens(ev.tokens)} tokens`
      if (ev.mergeOpen) return `${tokens}…`
      return ev.redacted ? `${tokens} · contents not exposed by this model` : tokens
    }
    case 'result':
      return ev.content ?? ''
    case 'task':
      return [ev.taskId && `[${ev.taskId}]`, ev.text].filter(Boolean).join(' ')
    case 'rate_limit': {
      const bits = [ev.status]
      if (ev.resetsAt) bits.push(`resets ${clockTime(ev.resetsAt * 1000)}`)
      if (ev.usingOverage) bits.push('on overage')
      if (ev.repeats > 1) bits.push(`×${ev.repeats}`)
      return bits.filter(Boolean).join(' · ')
    }
    default:
      return ev.text ?? ''
  }
}

/** Short text snippet for bead cards. */
export function eventSnippet(ev) {
  if (ev.kind === 'tool') return `${ev.tool}: ${toolPreview(ev.tool, ev.input)}`.slice(0, 100)
  const label = rowSpec(ev).label
  const body = bodyText(ev)
  return (body ? `${label}: ${body}` : label).slice(0, 100)
}

// ts arrives as epoch-ms int (bot writes) or ISO string (other sources)
function toMs(ts) {
  if (typeof ts === 'number') return ts
  const ms = new Date(ts).getTime()
  return Number.isFinite(ms) ? ms : Date.now()
}

function cleanText(text) {
  return (text ?? '').replace(/<br\s*\/?>/gi, '').trim()
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

export function formatTokens(n) {
  if (!n) return '0'
  if (n < 1000) return String(n)
  return (n / 1000).toFixed(n < 10_000 ? 1 : 0) + 'k'
}

export function formatDuration(ms) {
  if (!ms) return '0s'
  if (ms < 1000) return `${ms}ms`
  const s = ms / 1000
  if (s < 60) return `${s.toFixed(1)}s`
  const m = Math.floor(s / 60)
  return `${m}m ${Math.round(s % 60)}s`
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
