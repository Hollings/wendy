import { useState, useCallback, useRef, useEffect } from 'react'
import { useWebSocket } from '../hooks/useWebSocket'
import { parseStreamEvent, getEventSnippet, CONTEXT_WINDOW } from '../eventUtils'
import { authHeaders, clearToken, clearPassphrase } from '../auth'
import Feed from './Feed'
import Sidebar from './Sidebar'

const MAX_EVENTS = 200

export default function BrainApp() {
  const [events, setEvents] = useState([])
  // beads: Map<id, {id, title, status, created, updated}>
  const [beads, setBeads] = useState(new Map())
  // beadSnippets: Map<id, {text, ts}> - latest event snippet per bead
  const [beadSnippets, setBeadSnippets] = useState(new Map())
  const [channelsMap, setChannelsMap] = useState({})
  const [channelContextPct, setChannelContextPct] = useState({})
  // focusedBead: {id, title} or null
  const [focusedBead, setFocusedBead] = useState(null)
  // hiddenChannels: Set of channel IDs to hide (empty = show all)
  const [hiddenChannels, setHiddenChannels] = useState(new Set())
  const [wsStatus, setWsStatus] = useState('connecting')

  // Dedup tracker
  const seenIds = useRef(new Set())

  const onEvent = useCallback((raw) => {
    const parsed = parseStreamEvent(raw)
    if (!parsed) return

    // Simple dedup by id (ts + counter)
    if (seenIds.current.has(parsed.id)) return
    seenIds.current.add(parsed.id)
    if (seenIds.current.size > 2000) {
      const arr = [...seenIds.current]
      seenIds.current = new Set(arr.slice(-1000))
    }

    // Update bead snippet with latest event text
    if (parsed.bead_id) {
      const snippet = getEventSnippet(parsed)
      if (snippet) {
        setBeadSnippets(prev => {
          const next = new Map(prev)
          next.set(parsed.bead_id, { text: snippet, ts: parsed.ts })
          return next
        })
      }
    }

    // Fold nudge text into the preceding system (init) card
    if (parsed.kind === 'nudge') {
      setEvents(prev => {
        const idx = [...prev].map(e => e.kind).lastIndexOf('system')
        if (idx === -1) return prev
        const updated = [...prev]
        updated[idx] = { ...updated[idx], nudgeText: parsed.text }
        return updated
      })
      return
    }

    setEvents(prev => {
      const next = [...prev, parsed]
      return next.length > MAX_EVENTS ? next.slice(-MAX_EVENTS) : next
    })
  }, [])

  const onBeadsList = useCallback((list) => {
    setBeads(new Map(list.map(b => [b.id, b])))
  }, [])

  const onChannelsMap = useCallback((channels) => {
    setChannelsMap(channels)
  }, [])

  const onContextUpdate = useCallback(({ usage, channel_id }) => {
    if (!channel_id) return
    const tokens = (usage.cache_read_input_tokens ?? 0) + (usage.input_tokens ?? 0)
    const pct = Math.min(100, Math.round((tokens / CONTEXT_WINDOW) * 100 * 10) / 10)
    setChannelContextPct(prev => ({ ...prev, [channel_id]: pct }))
  }, [])

  const onStatus = useCallback(async (status) => {
    setWsStatus(status)
    if (status === 'auth_error') {
      clearToken()
      clearPassphrase()
      window.location.reload()
    }
    // On connect, load initial beads list
    if (status === 'connected') {
      try {
        const res = await fetch('/api/brain/beads', { headers: authHeaders() })
        if (res.ok) {
          const data = await res.json()
          setBeads(new Map((data.beads ?? []).map(b => [b.id, b])))
        }
      } catch { /* ignore */ }
    }
  }, [])

  useWebSocket({ onEvent, onBeadsList, onChannelsMap, onContextUpdate, onStatus })

  // Channels that have appeared in the event stream
  const activeChannels = [...new Set(events.map(e => e.channel_id).filter(Boolean))]

  // Last activity timestamp per channel (derived from events)
  const lastActivity = {}
  for (const ev of events) {
    if (ev.channel_id && ev.ts > (lastActivity[ev.channel_id] ?? 0)) {
      lastActivity[ev.channel_id] = ev.ts
    }
  }

  const refreshChannelsMap = useCallback(async () => {
    try {
      const res = await fetch('/api/brain/channels', { headers: authHeaders() })
      if (res.ok) {
        const data = await res.json()
        if (data?.channels) setChannelsMap(data.channels)
      }
    } catch { /* ignore */ }
  }, [])

  // Poll every 30s so thread names stay fresh
  useEffect(() => {
    const id = setInterval(refreshChannelsMap, 30_000)
    return () => clearInterval(id)
  }, [refreshChannelsMap])

  // Refresh immediately when a new channel appears (e.g. new thread mid-session)
  const seenChannels = useRef(new Set())
  useEffect(() => {
    const hasNew = activeChannels.some(id => !seenChannels.current.has(id))
    activeChannels.forEach(id => seenChannels.current.add(id))
    if (hasNew) refreshChannelsMap()
  }, [activeChannels, refreshChannelsMap])

  // Events to show in feed - filtered by bead focus or channel filter
  const visibleEvents = events.filter(ev => {
    if (focusedBead) return ev.bead_id === focusedBead.id
    if (hiddenChannels.size > 0 && ev.channel_id) return !hiddenChannels.has(ev.channel_id)
    return true
  })

  function toggleChannel(id) {
    setHiddenChannels(prev => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })
  }

  return (
    <div className="brain-app">
      <Feed
        events={visibleEvents}
        wsStatus={wsStatus}
        focusedBead={focusedBead}
        onBack={() => setFocusedBead(null)}
      />
      <Sidebar
        activeChannels={activeChannels}
        channelsMap={channelsMap}
        channelContextPct={channelContextPct}
        lastActivity={lastActivity}
        hiddenChannels={hiddenChannels}
        onToggleChannel={toggleChannel}
        beads={beads}
        beadSnippets={beadSnippets}
        focusedBeadId={focusedBead?.id ?? null}
        onBeadClick={(bead) => setFocusedBead({ id: bead.id, title: bead.title })}
      />
    </div>
  )
}
