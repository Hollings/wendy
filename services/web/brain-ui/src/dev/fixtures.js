import { appendEvents, frameKey, parseFrame } from '../events.js'
const now = Date.now()
export const channels = { '101': 'coding', '102': 'general', '103': 'creative-lab' }
export const tasks = [
  { id: 'wd-41', title: 'Polish the field notes page', status: 'in_progress', phase: 'running', model: 'claude-fable-5-1', _channel: 'coding' },
  { id: 'wd-42', title: 'Choose a direction for the poster', status: 'open', phase: 'needs_input', model: 'claude-opus-4-6', _channel: 'creative-lab', close_reason: 'Two directions are ready. Waiting for Wendy to choose which one to develop.' },
  { id: 'wd-43', title: 'Review the search implementation', status: 'open', phase: 'quota_wait', model: 'claude-fable-5-1', _channel: 'coding', close_reason: 'Daily Fable allowance reached. The task will remain queued until the next reset.' },
  { id: 'wd-38', title: 'Fix the image loading state', status: 'closed', phase: 'succeeded', model: 'claude-opus-4-6', _channel: 'coding', close_reason: 'Added an image placeholder and verified the loading and error states at mobile sizes.' },
]
const frame = (n, event, source = '101') => ({ ts: now - 120000 + n * 6000, channel_id: source.startsWith('wd-') ? null : source, bead_id: source.startsWith('wd-') ? source : null, event: { ...event, uuid: 'preview-' + n, session_id: 'preview-' + source } })
const text = (value, model = 'claude-opus-4-6') => ({ type: 'assistant', message: { model, usage: { input_tokens: 2400, cache_read_input_tokens: 68400 }, content: [{ type: 'text', text: value }] } })
const call = (id, name, input) => ({ type: 'assistant', message: { content: [{ type: 'tool_use', id, name, input }] } })
const result = (id, content, is_error = false) => ({ type: 'user', message: { content: [{ type: 'tool_result', tool_use_id: id, content, is_error }] } })
export const frames = [
  frame(0, { type: 'system', subtype: 'init', model: 'claude-opus-4-6' }),
  frame(1, text('I’m taking a look at the field notes page. The content is there; it just needs a little more room to breathe.')),
  frame(2, { type: 'assistant', message: { content: [{ type: 'thinking', thinking: 'I’ll keep the existing notes intact, then check the spacing and image layout at a few screen sizes.' }] } }),
  frame(3, call('read-1', 'Read', { file_path: '/workspace/field-notes/styles.css' }), 'wd-41'),
  frame(4, result('read-1', '.notes-grid {\n  display: grid;\n  grid-template-columns: repeat(3, 1fr);\n  gap: 12px;\n}\n\n.note { padding: 12px; }'), 'wd-41'),
  frame(5, call('edit-1', 'Edit', { file_path: '/workspace/field-notes/styles.css', old_string: 'gap: 12px;\nmax-width: 1440px;', new_string: 'gap: 24px;\nmax-width: 1120px;' }), 'wd-41'),
  frame(6, result('edit-1', 'The file was updated successfully.'), 'wd-41'),
  frame(7, call('chat-1', 'Bash', { command: 'msg "A tiny field report: the image loading fix is in. Slow connections get a proper placeholder now, instead of a blank patch. 🌱"' }), '102'),
  frame(8, result('chat-1', 'Sent (id=123456): A tiny field report: the image loading fix is in. Slow connections get a proper placeholder now, instead of a blank patch. 🌱'), '102'),
  frame(9, call('test-1', 'Bash', { command: 'npm run test -- --run', description: 'Check the field notes layout tests' }), 'wd-41'),
  frame(10, result('test-1', 'PASS  layout.test.js\n✓ keeps the note text visible on mobile\n✓ preserves image aspect ratios\n✓ shows the loading placeholder\n\nTest files  1 passed\nTests       3 passed\nDuration    842ms'), 'wd-41'),
  frame(11, text('The poster drafts are ready. One is quiet and typographic; the other leans into a bright, illustrated border. I’ve saved both for a closer look.', 'claude-sonnet-5'), '103'),
  frame(12, call('read-2', 'Read', { file_path: '/workspace/field-notes/mobile.css' }), 'wd-41'),
  frame(13, result('read-2', 'File does not exist. The mobile styles are in styles.css.', true), 'wd-41'),
  frame(14, { type: 'assistant', message: { content: [{ type: 'thinking', thinking: 'The layout checks pass. I’m doing one final visual pass on the narrower screen before handing this back to Wendy.' }] } }, 'wd-41'),
  frame(15, call('checkpoint', 'Bash', { command: 'wtask checkpoint "Spacing updated; all 3 layout tests pass. Final mobile visual check remains."', description: 'Save a progress checkpoint' }), 'wd-41'),
  frame(16, result('checkpoint', '{"saved": true}'), 'wd-41'),
  frame(17, text('A little more space makes a big difference. The notes are easier to scan, the images keep their shape, and the mobile view is almost ready.\n\nI’ll share the finished page once the last visual check is done.')),
]
export function fixtureStore() {
  let events = []
  for (const raw of frames) events = appendEvents(events, parseFrame(raw, frameKey(JSON.stringify(raw))))
  return { events, channelsMap: channels, channelStats: {
    '101': { tokens: 70800, model: 'claude-opus-4-6', lastTs: now, count: 24 },
    '102': { tokens: 24200, model: 'claude-opus-4-6', lastTs: now - 60000, count: 12 },
    '103': { tokens: 118200, model: 'claude-sonnet-5', lastTs: now - 30000, count: 18 },
  }, beads: tasks, wsStatus: 'demo', viewers: 2, syncError: '', received: events.length, reconnect: () => {} }
}
