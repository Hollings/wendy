import { useEffect, useRef, useState } from 'react'
import { eventText, eventTitle, sourceLabel } from '../timeline'
import { parseMsgsOutput } from '../events'
import { TOOL_DETAIL } from './ToolDetails'
import Icon from './Icon'
import Markdown from './Markdown'
export default function Inspector({ event, channelsMap, beads, onClose }) {
  const [tab, setTab] = useState('content'), [copied, setCopied] = useState('')
  const panel = useRef(null)
  useEffect(() => {
    setTab('content'); setCopied('')
    if (event) panel.current?.focus({ preventScroll: true })
  }, [event?.id])
  useEffect(() => {
    if (!event) return
    const handler = e => { if (e.key === 'Escape') onClose() }
    document.addEventListener('keydown', handler)
    return () => document.removeEventListener('keydown', handler)
  }, [event, onClose])
  const raw = event ? JSON.stringify(event, null, 2) : ''
  async function copy() {
    try { await navigator.clipboard.writeText(tab === 'raw' ? raw : [eventTitle(event), eventText(event), event.input ? JSON.stringify(event.input, null, 2) : '', event.output?.content].filter(Boolean).join('\n\n')); setCopied('Copied') }
    catch { setCopied('Select the text to copy it.') }
  }
  if (!event) return null
  const Detail = TOOL_DETAIL[event.tool]
  const messages = event.flavor === 'msgs' && event.output ? parseMsgsOutput(event.output.content) : null
  return <aside className="inspector" data-event-kind={event.kind} data-tool={event.tool} data-flavor={event.flavor} ref={panel} tabIndex={-1} aria-label="Event details">
    <div className="inspector-heading"><span className="eyebrow">DETAIL INSPECTOR</span><button className="icon-button" aria-label="Close details" onClick={onClose}><Icon name="Close" size={18} /></button></div>
    <div className="inspector-title"><span className="inspector-source">{sourceLabel(event, channelsMap, beads)}</span><h2>{eventTitle(event)}</h2><time>{event.timestampEstimated ? 'Saved log · individual event time not recorded' : new Date(event.ts).toLocaleString()}</time></div>
    <div className="inspector-tabs"><div role="tablist" aria-label="Event detail format"><button role="tab" aria-selected={tab === 'content'} onClick={() => setTab('content')}>Content</button><button role="tab" aria-selected={tab === 'raw'} onClick={() => setTab('raw')}>Event data</button></div><button className="icon-button" aria-label="Copy event details" onClick={copy}><Icon name="Copy" size={16} /></button></div>
    {copied && <div className="copy-status" role="status">{copied}</div>}
    <div className="inspector-body" role="tabpanel">
      {tab === 'raw' ? <pre className="raw-data">{raw}</pre> : <>
        {event.input && <section className="detail-section"><h3>Tool input</h3>{Detail ? <Detail ev={event} /> : <pre>{JSON.stringify(event.input, null, 2)}</pre>}</section>}
        {!event.input && <section className="detail-section"><h3>{event.kind === 'thinking' ? 'Thought summary' : 'Full content'}</h3>{event.kind === 'unknown' ? <pre>{eventText(event)}</pre> : <Markdown text={eventText(event) || 'No additional content recorded.'} />}</section>}
        {event.kind === 'discord' && event.text && <section className="detail-section"><h3>Message</h3><Markdown text={event.text} />{event.attachment && <p>Attachment: <code>{event.attachment}</code></p>}</section>}
        {event.output && <section className={'detail-section' + (event.output.isError ? ' error-section' : '')}><h3>{event.output.isError ? 'Tool error' : 'Tool result'}</h3>{messages ? messages.length ? messages.map((message, i) => <div className="received-message" key={i}><div><strong>{message.author}</strong><time>{message.time}</time></div><Markdown text={message.text} />{message.attachments.map((path, j) => <p key={j} className="attachment-path">{path}</p>)}</div>) : <p className="muted">No new messages.</p> : <pre>{event.output.content || 'Completed with no output.'}</pre>}</section>}
        {event.awaiting && <p className="pending-label">Waiting for the tool result…</p>}
        <section className="detail-section detail-provenance"><h3>Event metadata</h3><dl>{[['Event', event.id], ['Session', event.session_id], ['Attempt', event.attempt_id], ['Tool call', event.toolUseId], ['Task', event.bead_id], ['Channel', event.channel_id], ['Model', event.model], ['Duration', event.durationMs != null && !event.timestampEstimated ? event.durationMs + ' ms' : null], ['Cost', event.cost != null ? '$' + event.cost.toFixed(4) : null]].filter(([, value]) => value != null && value !== '').map(([label, value]) => <div key={label}><dt>{label}</dt><dd><code>{value}</code></dd></div>)}</dl></section>
      </>}
    </div>
    <div className="inspector-footer"><kbd>Esc</kbd> close<span>Full event content</span></div>
  </aside>
}
