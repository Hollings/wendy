// Dev-only harness behind /replay.html: replays a captured stream through the
// exact store pipeline (parseFrame -> appendEvents -> slice -> deriveFeed) and
// renders the real Feed. Lets feed changes be verified against production
// data without a live WebSocket or auth.

import { useEffect, useMemo, useState } from 'react'
import { createRoot } from 'react-dom/client'
import { parseFrame, frameKey, appendEvents, FILTER_GROUPS } from '../events'
import { deriveFeed } from '../derive'
import Feed from '../components/Feed'
import '../App.css'

const MAX_EVENTS = 600

// Stable channel names for the sample; unknown ids show their tail.
const CHANNELS = {}

function ReplayApp() {
  const [events, setEvents] = useState([])
  const [hiddenKinds, setHiddenKinds] = useState(new Set())
  const [error, setError] = useState(null)

  useEffect(() => {
    fetch('/stream-sample.jsonl')
      .then(r => { if (!r.ok) throw new Error(`${r.status}`); return r.text() })
      .then(text => {
        let evs = []
        for (const line of text.split('\n')) {
          if (!line.trim()) continue
          let raw
          try { raw = JSON.parse(line) } catch { continue }
          evs = appendEvents(evs, parseFrame(raw, frameKey(line)))
        }
        setEvents(evs.length > MAX_EVENTS ? evs.slice(-MAX_EVENTS) : evs)
      })
      .catch(e => setError(String(e)))
  }, [])

  const hiddenKindSet = useMemo(() => {
    const kinds = new Set()
    for (const g of FILTER_GROUPS) {
      if (hiddenKinds.has(g.id)) g.kinds.forEach(k => kinds.add(k))
    }
    return kinds
  }, [hiddenKinds])

  const derived = useMemo(() => deriveFeed(events, hiddenKindSet), [events, hiddenKindSet])

  if (error) return <div style={{ padding: 20 }}>failed to load /stream-sample.jsonl: {error}</div>

  return (
    <div className="app" style={{ height: '100vh' }}>
      <div className="main">
        <Feed
          rows={derived.rows}
          turns={derived.turns}
          lastTurnBySource={derived.lastTurnBySource}
          totalCount={events.length}
          channelsMap={CHANNELS}
          hiddenKinds={hiddenKinds}
          onToggleKind={(id) => setHiddenKinds(prev => {
            const next = new Set(prev)
            next.has(id) ? next.delete(id) : next.add(id)
            return next
          })}
          focusedBead={null}
          onClearBead={() => {}}
        />
      </div>
    </div>
  )
}

createRoot(document.getElementById('root')).render(<ReplayApp />)
