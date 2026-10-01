import test from 'node:test'
import assert from 'node:assert/strict'
import { buildTimeline, filterTimeline, mergeHistory, taskPhase, needsAttention } from '../src/timeline.js'
import { appendEvents, frameKey, parseFrame } from '../src/events.js'
const call = (id, source, input = 'echo hello') => ({ id, channel_id: source, kind: 'tool', tool: 'Bash', input: { command: input }, toolUseId: 'same-id', ts: 100 })
const output = (id, source, content, isError = false) => ({ id, channel_id: source, kind: 'result', toolUseId: 'same-id', content, isError, ts: 200 })
test('interleaved sources keep their own tool results', () => {
  const rows = buildTimeline([call('a', 'one'), call('b', 'two'), output('c', 'two', 'second'), output('d', 'one', 'first')])
  assert.equal(rows.length, 2)
  assert.equal(rows[0].output.content, 'first')
  assert.equal(rows[1].output.content, 'second')
})
test('attempts keep separate results even with reused tool identifiers', () => {
  const rows = buildTimeline([{ ...call('a', 'one'), attempt_id: 'a' }, { ...call('b', 'one'), attempt_id: 'b' }, { ...output('c', 'one', 'b'), attempt_id: 'b' }])
  assert.equal(rows[0].awaiting, true)
  assert.equal(rows[1].output.content, 'b')
})
test('repeated calls and old turns never disappear', () => {
  const rows = buildTimeline([call('a', 'one'), output('b', 'one', 'first'), call('c', 'one'), output('d', 'one', 'second')])
  assert.equal(rows.length, 2)
  assert.equal(rows[0].output.content, 'first')
  assert.equal(rows[1].output.content, 'second')
})

test('turn boundaries survive filtering and interleaved channels in a resumed session', () => {
  const event = (id, channel, kind) => ({ id, channel_id: channel, session_id: 'resumed', kind, text: id, ts: 100 })
  const rows = buildTimeline([
    event('start-a', 'one', 'session_start'), event('a', 'one', 'speech'),
    event('start-b', 'two', 'session_start'), event('b', 'two', 'speech'),
    event('a-more', 'one', 'speech'), event('end-a', 'one', 'session_end'),
    event('start-c', 'one', 'session_start'), event('c', 'one', 'speech'),
    event('b-more', 'two', 'speech'), event('end-c', 'one', 'session_end'),
    event('missing-init', 'one', 'speech'),
  ])
  const messages = filterTimeline(rows, { category: 'messages' })
  assert.equal(messages[0].turnId, messages[2].turnId)
  assert.equal(messages[1].turnId, messages[4].turnId)
  assert.notEqual(messages[0].turnId, messages[1].turnId)
  assert.notEqual(messages[0].turnId, messages[3].turnId)
  assert.notEqual(messages[3].turnId, messages[5].turnId)
})
test('error and text filtering apply after pairing, including output contents', () => {
  const rows = buildTimeline([call('a', 'one'), output('b', 'one', 'missing styles.css', true)])
  assert.equal(filterTimeline(rows, { category: 'errors' }).length, 1)
  assert.equal(filterTimeline(rows, { query: 'styles.css' }).length, 1)
  assert.equal(filterTimeline(rows, { category: 'messages' }).length, 0)
})
test('channel selection does not accidentally include task events', () => {
  const rows = [{ ...call('a', 'one'), bead_id: 'task' }, call('b', 'one')]
  assert.deepEqual(filterTimeline(rows, { source: 'channel:one' }).map(row => row.id), ['b'])
  assert.deepEqual(filterTimeline(rows, { source: 'task:task' }).map(row => row.id), ['a'])
})
test('execution identifiers can trace an event without matching unrelated rows', () => {
  const rows = [{ ...call('event-a', 'one'), session_id: 'session-abc', attempt_id: 'attempt-xyz', toolUseId: 'tool-123' }, call('unrelated', 'two')]
  for (const query of ['event-a', 'session-abc', 'attempt-xyz', 'tool-123']) {
    assert.deepEqual(filterTimeline(rows, { query }).map(row => row.id), ['event-a'])
  }
})
test('late history is deduplicated without replacing the live record', () => {
  assert.deepEqual(mergeHistory([{ id: 'b', value: 'live' }], [{ id: 'a' }, { id: 'b', value: 'history' }]), [{ id: 'a' }, { id: 'b', value: 'live' }])
  assert.deepEqual(mergeHistory([{ id: 'b', value: 'live' }], [{ id: 'a' }, { id: 'b' }, { id: 'c' }]).map(event => event.id), ['a', 'b', 'c'])
})
test('UUID identity survives different timestamps on history replay', () => {
  const raw = { event: { uuid: 'abc', type: 'assistant' }, channel_id: 'one' }
  assert.equal(frameKey(JSON.stringify({ ...raw, ts: 100 })), frameKey(JSON.stringify({ ...raw, ts: 200 })))
  assert.equal(frameKey(JSON.stringify(raw)), frameKey(JSON.stringify({ ...raw, attempt_id: 'newly-enriched-history' })))
})
test('thinking updates keep the selected row identity and finalized text', () => {
  const first = parseFrame({ ts: 100, channel_id: 'one', event: { type: 'system', subtype: 'thinking_tokens', estimated_tokens: 120 } }, 'first')
  const second = parseFrame({ ts: 200, channel_id: 'one', event: { type: 'assistant', message: { content: [{ type: 'thinking', thinking: 'A visible summary.' }] } } }, 'second')
  const rows = appendEvents(first, second)
  assert.equal(rows.length, 1)
  assert.equal(rows[0].id, 'first-0')
  assert.equal(rows[0].text, 'A visible summary.')
  assert.equal(rows[0].mergeOpen, false)
})
test('missing results stop looking active when their turn ends', () => {
  const rows = buildTimeline([call('a', 'one'), { id: 'end', channel_id: 'one', kind: 'session_end', ts: 300 }])
  assert.equal(rows[0].awaiting, false)
  assert.equal(rows[0].unfinished, true)
})
test('task phases preserve quota waits and explicit intervention', () => {
  assert.equal(taskPhase({ status: 'open', phase: 'quota_wait' }), 'quota_wait')
  assert.equal(taskPhase({ status: 'closed' }), 'succeeded')
  assert.equal(needsAttention({ phase: 'interrupted' }), true)
})
