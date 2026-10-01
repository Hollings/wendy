import { clockTime, formatDuration, parseMsgsOutput } from '../events'
import { eventText, eventTitle, isError, sourceLabel } from '../timeline'
import Icon from './Icon'
export default function EventRow({ ev, channelsMap, beads, selected, onSelect, showDate, showTurn }) {
  const error = isError(ev)
  const text = ev.tool === 'Bash' && ev.input?.command ? ev.input.command : eventText(ev)
  const messages = ev.flavor === 'msgs' && ev.output ? parseMsgsOutput(ev.output.content) : null
  const result = ev.output && (ev.kind !== 'discord' || error) ? messages ? messages.length ? messages.length + ' messages · ' + messages[0].author + ': ' + messages[0].text : 'No new messages' : ev.output.content || '(empty output)' : ''
  const state = error ? 'ERROR' : ev.awaiting ? 'RUNNING' : ev.mergeOpen ? 'STREAMING' : ev.unfinished ? 'NO RESULT' : ev.output || ev.kind === 'session_end' ? 'DONE' : ''
  const origin = ev.bead_id || (ev.channel_id ? '#' + (channelsMap[ev.channel_id] || ev.channel_id) : 'unassigned')
  function selectRow(event) {
    if (event.defaultPrevented || event.target.closest('button, a, input, textarea, select, [contenteditable="true"]')) return
    const selection = window.getSelection()
    if (selection && !selection.isCollapsed && selection.rangeCount && selection.getRangeAt(0).intersectsNode(event.currentTarget)) return
    event.currentTarget.querySelector('.detail-button')?.focus({ preventScroll: true })
    onSelect()
  }
  return <>
    {showDate && <div className="date-label">{new Date(ev.ts).toLocaleDateString(undefined, { year: 'numeric', month: '2-digit', day: '2-digit' })}<span /></div>}
    {showTurn && !showDate && <div className="turn-separator" role="separator" aria-label={'New turn in ' + sourceLabel(ev, channelsMap, beads)} />}
    <article onClick={selectRow} data-event-kind={ev.kind} data-tool={ev.tool} data-flavor={ev.flavor} className={'event-card kind-' + ev.kind + (error ? ' has-error' : '') + (selected ? ' selected' : '')}>
      <div className="event-time"><time dateTime={new Date(ev.ts).toISOString()} title={ev.timestampEstimated ? 'Saved log; individual event time was not recorded.' : new Date(ev.ts).toLocaleString()}>{ev.timestampEstimated ? 'saved log' : clockTime(ev.ts)}</time><span>{ev.durationMs != null && !ev.timestampEstimated ? formatDuration(ev.durationMs) : ev.cost != null ? '$' + ev.cost.toFixed(4) : ''}</span></div>
      <div className="event-origin"><span className="event-source" title={sourceLabel(ev, channelsMap, beads)}>{origin}</span><span className={'event-label' + (ev.kind === 'thinking' ? ' thinking-label' : '')}>{eventTitle(ev)}</span></div>
      <div className="event-main"><p className="event-preview" title={text}>{text.slice(0, 1200) || 'No content recorded'}</p>{result && <div className={'output-preview' + (error ? ' error-text' : '')}><span aria-hidden="true">↳</span><p>{result.slice(0, 500)}</p></div>}</div>
      <div className="event-actions"><span className={'event-state' + (ev.awaiting || ev.mergeOpen ? ' pending-label' : '')}>{state}</span><button className="detail-button" onClick={onSelect} aria-label={'Inspect ' + eventTitle(ev)} aria-pressed={selected} title="Inspect full input, output, and event data"><Icon name="Panel" size={14} /><span>Inspect</span></button></div>
    </article>
  </>
}
