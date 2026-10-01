import { bodyText, parseSentResult, rowSpec, toolPreview } from './events.js'
export const CATEGORIES = [['all', 'All'], ['messages', 'Messages'], ['thinking', 'Thinking'], ['tools', 'Tools'], ['errors', 'Errors'], ['system', 'System']]
export const sourceKey = ev => [ev.bead_id ? 'task' : 'channel', ev.bead_id || ev.channel_id || 'unknown', ev.attempt_id || '', ev.session_id || ''].join(':')
export const taskPhase = task => task.phase || ({ in_progress: 'running', open: 'queued', closed: 'succeeded', tombstone: 'cancelled' }[task.status]) || task.status || 'queued'
export const phaseLabel = phase => ({ quota_wait: 'Waiting for quota', needs_input: 'Needs input', timed_out: 'Timed out', succeeded: 'Completed' }[phase] || phase.replaceAll('_', ' '))
export const isRunning = task => ['starting', 'running', 'stopping', 'finishing'].includes(taskPhase(task))
export const needsAttention = task => ['needs_input', 'failed', 'interrupted', 'timed_out'].includes(taskPhase(task))
export const isError = ev => !!(ev.isError || ev.output?.isError || ev.blocked || ev.status === 'failed' || (ev.kind === 'rate_limit' && ev.status !== 'allowed'))

// Keep every distinct action. Pair results within the source and attempt.
export function buildTimeline(events) {
  const rows = [], pending = new Map(), turns = new Map()
  for (const event of events) {
    const key = sourceKey(event) + ':' + event.toolUseId
    if (event.kind === 'result' && event.toolUseId && pending.has(key)) {
      const index = pending.get(key), call = rows[index]
      const output = { content: event.content || '', isError: !!event.isError, ts: event.ts, sourceFrame: event.sourceFrame }
      rows[index] = { ...call, awaiting: false, output, durationMs: Math.max(0, event.ts - call.ts) }
      if (call.kind === 'discord') {
        const delivered = parseSentResult(output.content)
        if (delivered !== null) rows[index].text = delivered || call.text
        rows[index].blocked = output.isError
      }
      pending.delete(key)
      continue
    }
    if (event.kind === 'session_end') {
      for (const [id, index] of pending) {
        if (sourceKey(rows[index]) === sourceKey(event)) {
          rows[index] = { ...rows[index], awaiting: false, unfinished: true }
          pending.delete(id)
        }
      }
    }
    // Track turns before filtering so boundaries survive hidden system rows.
    const source = sourceKey(event)
    if (event.kind === 'session_start' || !turns.has(source)) turns.set(source, source + ':' + event.id)
    const row = { ...event, turnId: turns.get(source) }
    if (row.toolUseId && ['tool', 'discord'].includes(row.kind)) { row.awaiting = true; pending.set(key, rows.length) }
    rows.push(row)
    if (event.kind === 'session_end') turns.delete(source)
  }
  return rows
}
export function eventTitle(ev) {
  if (ev.kind === 'speech') return 'Assistant text'
  if (ev.kind === 'discord') return ev.sub === 'reaction' ? 'Discord reaction' : ev.blocked ? 'Message not sent' : ev.awaiting ? 'Sending to Discord' : ev.output ? 'Sent to Discord' : 'Discord message'
  if (ev.kind === 'thinking') return ev.mergeOpen ? 'Thinking' : 'Thought summary'
  if (ev.kind === 'session_start') return 'Turn started'
  if (ev.kind === 'session_end') return ev.isError ? 'Turn failed' : 'Turn completed'
  if (ev.kind === 'tool' && ev.flavor === 'msgs') return 'Check messages'
  return rowSpec(ev).label
}
export function eventText(ev) {
  if (ev.kind === 'tool') return toolPreview(ev.tool, ev.input)
  if (ev.kind === 'unknown') return JSON.stringify(ev.raw || {}, null, 2)
  if (ev.kind === 'thinking' && !ev.text) return ev.mergeOpen ? 'A thought summary will appear when available.' : 'No thought summary was returned.'
  return bodyText(ev) || (ev.kind === 'session_start' ? ev.model || 'Claude session' : '')
}
export function filterTimeline(rows, { source = 'all', category = 'all', query = '' } = {}) {
  const needle = query.trim().toLocaleLowerCase()
  return rows.filter(ev => {
    if (source.startsWith('channel:') && (ev.bead_id || String(ev.channel_id) !== source.slice(8))) return false
    if (source.startsWith('task:') && String(ev.bead_id) !== source.slice(5)) return false
    if (category === 'messages' && !['speech', 'discord', 'nudge'].includes(ev.kind)) return false
    if (category === 'thinking' && ev.kind !== 'thinking') return false
    if (category === 'tools' && !['tool', 'result'].includes(ev.kind)) return false
    if (category === 'errors' && !isError(ev)) return false
    if (category === 'system' && ['speech', 'discord', 'thinking', 'tool', 'result'].includes(ev.kind)) return false
    if (needle && ![eventTitle(ev), eventText(ev), ev.output?.content, JSON.stringify(ev.input || {}), ev.id, ev.channel_id, ev.bead_id, ev.session_id, ev.attempt_id, ev.toolUseId, ev.model].join(' ').toLocaleLowerCase().includes(needle)) return false
    return true
  })
}
export function mergeHistory(live, history) {
  const current = new Map(live.map(ev => [ev.id, ev]))
  const historicalIds = new Set(history.map(ev => ev.id))
  return [...history.map(ev => current.get(ev.id) || ev), ...live.filter(ev => !historicalIds.has(ev.id))]
}
export function sourceLabel(ev, channels, tasks = []) {
  if (ev.bead_id) return tasks.find(task => task.id === ev.bead_id)?.title || 'Task ' + ev.bead_id
  return ev.channel_id ? '#' + (channels[ev.channel_id] || ev.channel_id) : 'Unassigned'
}
