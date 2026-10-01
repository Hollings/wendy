import test from 'node:test'
import assert from 'node:assert/strict'
import { parseFrame, frameKey, frameUsage, bodyText, parseMsgsOutput } from '../src/events.js'
import { buildTimeline, filterTimeline } from '../src/timeline.js'

// Synthetic values, matching shapes observed on the live CLI in September 2026.
const envelope = event => ({ channel_id: 'channel', ts: 1000, event: { session_id: 'session', ...event } })
const parse = raw => parseFrame(raw, frameKey(JSON.stringify(raw)))
test('permission denials appear in Errors and retain their decision payload', () => {
  const raw = envelope({ type: 'system', subtype: 'permission_denied', tool_name: 'Monitor', tool_use_id: 'call-1', decision_reason_type: 'other', decision_reason: 'Cannot analyze shell syntax', message: 'Cannot analyze shell syntax' })
  const [row] = parse(raw)
  assert.equal(row.kind, 'permission')
  assert.equal(filterTimeline([row], { category: 'errors' }).length, 1)
  assert.equal(row.toolUseId, 'call-1')
  assert.equal(row.sourceFrame.event.decision_reason_type, 'other')
})
test('CLI task changes retain updates and empty background snapshots', () => {
  const [updated] = parse(envelope({ type: 'system', subtype: 'task_updated', task_id: 'internal-task', patch: { status: 'killed', end_time: 900 } }))
  assert.equal(updated.kind, 'task')
  assert.equal(updated.status, 'killed')
  const [snapshot] = parse(envelope({ type: 'system', subtype: 'background_tasks_changed', tasks: [] }))
  assert.equal(snapshot.kind, 'background_tasks')
  assert.equal(snapshot.text, 'No background tasks')
})
test('paired tools preserve both original payloads and request context', () => {
  const call = envelope({ type: 'assistant', uuid: 'call-frame', request_id: 'request', parent_tool_use_id: 'parent', message: { model: 'claude-sonnet-5', usage: { output_tokens: 12 }, content: [{ type: 'tool_use', id: 'call', name: 'Bash', input: { command: 'true' } }] } })
  const result = envelope({ type: 'user', uuid: 'result-frame', message: { content: [{ type: 'tool_result', tool_use_id: 'call', content: [{ type: 'text', text: 'ok' }] }] } })
  const [row] = buildTimeline([...parse(call), ...parse(result)])
  assert.equal(row.output.content, 'ok')
  assert.deepEqual(row.sourceFrame, call)
  assert.deepEqual(row.output.sourceFrame, result)
  assert.equal(row.model, 'claude-sonnet-5')
  assert.equal(row.parentToolUseId, 'parent')
})
test('context counts include tokens being written to cache', () => {
  assert.equal(frameUsage(envelope({ type: 'assistant', message: { usage: { input_tokens: 2, cache_creation_input_tokens: 4393, cache_read_input_tokens: 673710, output_tokens: 10 } } })), 678105)
})
test('msgs --raw displays messages without rounding Discord identifiers', () => {
  const rows = parseMsgsOutput('{"messages":[{"message_id":1546275876394500128,"author":"Example","timestamp":1788730000,"content":"Test message","attachments":["/tmp/example.png"]}],"more_pending":0}')
  assert.equal(rows[0].msgId, '1546275876394500128')
  assert.equal(rows[0].text, 'Test message')
  assert.deepEqual(rows[0].attachments, ['/tmp/example.png'])
  assert.equal(rows[0].synthetic, false)
  assert.deepEqual(parseMsgsOutput('{"messages":[]}'), [])
  assert.equal(parseMsgsOutput('{"unrelated":true}'), null)
})
test('compaction metrics and result diagnostics survive inspection', () => {
  const raw = envelope({ type: 'result', subtype: 'success', result: '', usage: { output_tokens: 100 }, modelUsage: { model: { costUSD: 0.1 } }, permission_denials: [{ tool_name: 'Monitor' }], ttft_ms: 500, terminal_reason: 'completed' })
  assert.deepEqual(parse(raw)[0].sourceFrame, raw)
  const [compact] = parse(envelope({ type: 'system', subtype: 'compact_boundary', compact_metadata: { pre_tokens: 968177, post_tokens: 18323, duration_ms: 222963, trigger: 'auto' } }))
  assert.match(bodyText(compact), /968k → 18k/)
})
