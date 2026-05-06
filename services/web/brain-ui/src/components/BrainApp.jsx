import { useState, useCallback, useRef, useEffect, useMemo } from 'react'
import { useWebSocket } from '../hooks/useWebSocket'
import { parseStreamEvent, getEventSnippet } from '../eventUtils'
import { authHeaders } from '../auth'
import Feed from './Feed'
import Sidebar from './Sidebar'
import TopBar from './TopBar'
import TweaksPanel from './TweaksPanel'
import { channelHue as getChannelHue } from '../channelColors'

const MAX_EVENTS = 200
const CONTEXT_WINDOW = 200_000

const DEFAULT_TWEAKS = { density: 'comfortable', accentHue: 145, scanlines: true }

function loadTweaks() {
  try {
    const raw = localStorage.getItem('brain_tweaks')
    return raw ? { ...DEFAULT_TWEAKS, ...JSON.parse(raw) } : DEFAULT_TWEAKS
  } catch { return DEFAULT_TWEAKS }
}

export default function BrainApp({ onLogout }) {
  const [events, setEvents] = useState([])
  const [beads, setBeads] = useState(new Map())
  const [beadSnippets, setBeadSnippets] = useState(new Map())
  const [channelsMap, setChannelsMap] = useState({})
  const [channelTokens, setChannelTokens] = useState({})
  const [focusedBead, setFocusedBead] = useState(null)
  const [hiddenChannels, setHiddenChannels] = useState(new Set())
  const [wsStatus, setWsStatus] = useState('connecting')
  const [viewers, setViewers] = useState(null)

  const [mobileView, setMobileView] = useState('feed')
  const [tweaks, setTweaks] = useState(loadTweaks)
  useEffect(() => {
    document.documentElement.dataset.density = tweaks.density
    document.documentElement.style.setProperty('--accent-h', tweaks.accentHue)
    let s = document.getElementById('scanline-toggle-style')
    if (!s) { s = document.createElement('style'); s.id = 'scanline-toggle-style'; document.head.appendChild(s) }
    s.textContent = tweaks.scanlines ? '' : 'body::before { display: none !important; }'
    try { localStorage.setItem('brain_tweaks', JSON.stringify(tweaks)) } catch {}
  }, [tweaks])

  const seenIds = useRef(new Set())

  const onEvent = useCallback((raw) => {
    const parsed = parseStreamEvent(raw)
    if (!parsed) return
    if (seenIds.current.has(parsed.id)) return
    seenIds.current.add(parsed.id)
    if (seenIds.current.size > 2000) {
      const arr = [...seenIds.current]
      seenIds.current = new Set(arr.slice(-1000))
    }

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
    if (!channel_id || !usage) return
    const tokens = (usage.cache_read_input_tokens ?? 0) + (usage.input_tokens ?? 0)
    setChannelTokens(prev => ({ ...prev, [channel_id]: tokens }))
  }, [])

  const onStatus = useCallback(async (status) => {
    setWsStatus(status)
    if (status === 'auth_error') {
      onLogout?.()
      return
    }
    if (status === 'connected') {
      try {
        const res = await fetch('/api/brain/beads', { headers: authHeaders() })
        if (res.ok) {
          const data = await res.json()
          setBeads(new Map((data.beads ?? []).map(b => [b.id, b])))
        }
      } catch {}
      try {
        const res = await fetch('/api/brain/stats', { headers: authHeaders() })
        if (res.ok) {
          const data = await res.json()
          if (data.viewers != null) setViewers(data.viewers)
        }
      } catch {}
    }
  }, [onLogout])

  useWebSocket({ onEvent, onBeadsList, onChannelsMap, onContextUpdate, onStatus })

  const activeChannelIds = useMemo(
    () => [...new Set(events.map(e => e.channel_id).filter(Boolean))],
    [events]
  )

  const channelActivity = useMemo(() => {
    const act = {}
    for (const ev of events) {
      if (!ev.channel_id) continue
      const tsMs = new Date(ev.ts).getTime()
      const ref = act[ev.channel_id] || { count: 0, last: 0 }
      ref.count += 1
      if (tsMs > ref.last) ref.last = tsMs
      act[ev.channel_id] = ref
    }
    const max = Math.max(1, ...Object.values(act).map(c => c.count))
    for (const k of Object.keys(act)) act[k].pct = (act[k].count / max) * 100
    return act
  }, [events])

  const channels = useMemo(
    () => activeChannelIds.map(id => ({ id, name: channelsMap[id] || `#${String(id).slice(-4)}` })),
    [activeChannelIds, channelsMap]
  )

  const sessions = useMemo(() => {
    return Object.entries(channelTokens)
      .filter(([id]) => activeChannelIds.includes(id) || channelsMap[id])
      .map(([id, tokens]) => ({
        id,
        name: channelsMap[id] || `#${String(id).slice(-4)}`,
        tokens,
      }))
      .sort((a, b) => b.tokens - a.tokens)
  }, [channelTokens, channelsMap, activeChannelIds])

  const totalTokens = useMemo(() => sessions.reduce((s, c) => s + c.tokens, 0), [sessions])
  const turns = useMemo(() => events.filter(e => e.kind === 'session_end').length + 1, [events])
  const activeTasks = useMemo(
    () => beads ? [...beads.values()].filter(b => b.status === 'in_progress').length : 0,
    [beads]
  )

  const lastActivity = useMemo(() => {
    const last = events[events.length - 1]
    return last?.ts ?? null
  }, [events])

  const mood = useMemo(() => {
    const overCap = sessions.some(s => s.tokens / CONTEXT_WINDOW > 0.82)
    if (overCap) return 'warn'
    const last = events[events.length - 1]
    if (!last) return 'idle'
    if (lastActivity && Date.now() - new Date(lastActivity).getTime() > 60_000) return 'idle'
    if (last.kind === 'tool') return 'tooling'
    return 'thinking'
  }, [events, sessions, lastActivity])

  const recentChannelHue = useMemo(() => {
    const last = events[events.length - 1]
    if (!last || !last.channel_id) return tweaks.accentHue
    return getChannelHue(last.channel_id)
  }, [events, tweaks.accentHue])

  const refreshChannelsMap = useCallback(async () => {
    try {
      const res = await fetch('/api/brain/channels', { headers: authHeaders() })
      if (res.ok) {
        const data = await res.json()
        if (data?.channels) setChannelsMap(data.channels)
      }
    } catch {}
  }, [])

  useEffect(() => {
    const id = setInterval(refreshChannelsMap, 30_000)
    return () => clearInterval(id)
  }, [refreshChannelsMap])

  const seenChannels = useRef(new Set())
  useEffect(() => {
    const hasNew = activeChannelIds.some(id => !seenChannels.current.has(id))
    activeChannelIds.forEach(id => seenChannels.current.add(id))
    if (hasNew) refreshChannelsMap()
  }, [activeChannelIds, refreshChannelsMap])

  const visibleEvents = useMemo(() => events.filter(ev => {
    if (focusedBead && ev.bead_id !== focusedBead.id) return false
    if (hiddenChannels.size > 0 && ev.channel_id && hiddenChannels.has(ev.channel_id)) return false
    return true
  }), [events, focusedBead, hiddenChannels])

  const toggleChannel = useCallback((id) => {
    setHiddenChannels(prev => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id); else next.add(id)
      return next
    })
  }, [])

  const onBeadClick = useCallback((bead) => {
    setFocusedBead(prev => prev?.id === bead.id ? null : { id: bead.id, title: bead.title })
  }, [])

  return (
    <div className="app" data-mobile-view={mobileView}>
      <TopBar
        viewers={viewers}
        lastActivity={lastActivity}
        wsStatus={wsStatus}
        onLogout={onLogout}
      />
      <div className="main">
        <Feed
          events={events}
          visibleEvents={visibleEvents}
          channelsMap={channelsMap}
          focusedBead={focusedBead}
          onClearFocus={() => setFocusedBead(null)}
        />
        <Sidebar
          mood={mood}
          channelHue={recentChannelHue}
          totalTokens={totalTokens}
          turns={turns}
          activeTasks={activeTasks}
          sessions={sessions}
          channels={channels}
          channelActivity={channelActivity}
          hiddenChannels={hiddenChannels}
          onToggleChannel={toggleChannel}
          beads={beads}
          beadSnippets={beadSnippets}
          focusedBeadId={focusedBead?.id ?? null}
          onBeadClick={onBeadClick}
        />
      </div>
      <button
        className="view-toggle"
        onClick={() => setMobileView(v => v === 'feed' ? 'sidebar' : 'feed')}
        title={mobileView === 'feed' ? 'Show sidebar' : 'Show feed'}
      >
        <span className="glyph" />
        {mobileView === 'feed' ? 'sidebar' : 'feed'}
      </button>
      <TweaksPanel tweaks={tweaks} onChange={setTweaks} />
    </div>
  )
}
