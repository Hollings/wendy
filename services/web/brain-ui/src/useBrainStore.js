import { useState, useEffect, useRef, useCallback } from 'react'
import { getToken, tryReauth, authHeaders } from './auth'
import { parseFrame, frameKey, frameUsage, frameModel, eventSnippet, appendEvents } from './events'

const MAX_EVENTS = 300
const MAX_SEEN = 4000
const CHANNELS_POLL_MS = 30_000
const STATS_POLL_MS = 60_000
const RECONNECT_MS = 3000

/**
 * Single source of truth for the dashboard.
 *
 * Owns the /ws/brain connection plus the REST polls, and exposes one plain
 * state object. Components render from this and nothing else.
 *
 *   events       parsed display events, capped at MAX_EVENTS
 *   channelsMap  {channel_id: display_name}
 *   channelStats {channel_id: {tokens, lastTs, count}} derived from the stream
 *   beads        bead list (REST on connect, WS beads_list pushes after)
 *   beadSnippets {bead_id: {text, ts}} most recent activity per bead
 *   wsStatus     connecting | connected | disconnected | full | auth_error
 *   viewers      connected dashboard count (from /api/brain/stats)
 */
export function useBrainStore({ onAuthError }) {
  const [events, setEvents] = useState([])
  const [channelsMap, setChannelsMap] = useState({})
  const [channelStats, setChannelStats] = useState({})
  const [beads, setBeads] = useState([])
  const [beadSnippets, setBeadSnippets] = useState({})
  const [wsStatus, setWsStatus] = useState('connecting')
  const [viewers, setViewers] = useState(null)

  const seenRef = useRef(new Set())
  const knownChannelsRef = useRef(new Set())
  const onAuthErrorRef = useRef(onAuthError)
  onAuthErrorRef.current = onAuthError

  // ---- REST fetchers ------------------------------------------------------

  const fetchJson = useCallback(async (url) => {
    try {
      const res = await fetch(url, { headers: authHeaders() })
      return res.ok ? await res.json() : null
    } catch {
      return null
    }
  }, [])

  const refreshChannels = useCallback(async () => {
    const data = await fetchJson('/api/brain/channels')
    if (data?.channels) setChannelsMap(data.channels)
  }, [fetchJson])

  const refreshStats = useCallback(async () => {
    const data = await fetchJson('/api/brain/stats')
    if (data?.viewers != null) setViewers(data.viewers)
  }, [fetchJson])

  const refreshBeads = useCallback(async () => {
    const data = await fetchJson('/api/brain/beads')
    if (data?.beads) setBeads(data.beads)
  }, [fetchJson])

  // ---- Stream frame handling ----------------------------------------------

  const handleFrame = useCallback((rawString, raw) => {
    const key = frameKey(rawString)
    if (seenRef.current.has(key)) return
    seenRef.current.add(key)
    if (seenRef.current.size > MAX_SEEN) {
      seenRef.current = new Set([...seenRef.current].slice(-MAX_SEEN / 2))
    }

    const parsed = parseFrame(raw, key)
    if (parsed.length === 0 && frameUsage(raw) == null) return

    // Per-channel stats (context tokens, last activity, event count)
    if (raw.channel_id) {
      const id = raw.channel_id
      const tokens = frameUsage(raw)
      const model = frameModel(raw)
      const lastTs = parsed[0]?.ts ?? Date.now()
      setChannelStats(prev => {
        const cur = prev[id] ?? { tokens: 0, lastTs: 0, count: 0, model: null }
        return {
          ...prev,
          [id]: {
            tokens: tokens ?? cur.tokens,
            model: model ?? cur.model,
            lastTs: Math.max(cur.lastTs, lastTs),
            count: cur.count + parsed.length,
          },
        }
      })
      if (!knownChannelsRef.current.has(id)) {
        knownChannelsRef.current.add(id)
        refreshChannels()
      }
    }

    if (parsed.length === 0) return

    // Bead activity snippet
    if (raw.bead_id) {
      const last = parsed[parsed.length - 1]
      const text = eventSnippet(last)
      if (text) {
        setBeadSnippets(prev => ({ ...prev, [raw.bead_id]: { text, ts: last.ts } }))
      }
    }

    setEvents(prev => {
      const next = appendEvents(prev, parsed)
      return next.length > MAX_EVENTS ? next.slice(-MAX_EVENTS) : next
    })
  }, [refreshChannels])

  const handleFrameRef = useRef(handleFrame)
  handleFrameRef.current = handleFrame

  // ---- WebSocket lifecycle -------------------------------------------------

  useEffect(() => {
    let closed = false
    let ws = null
    let timer = null

    async function connect() {
      if (closed) return
      const token = getToken()
      if (!token) {
        setWsStatus('auth_error')
        onAuthErrorRef.current?.()
        return
      }

      setWsStatus('connecting')
      const proto = location.protocol === 'https:' ? 'wss:' : 'ws:'
      ws = new WebSocket(`${proto}//${location.host}/ws/brain?token=${encodeURIComponent(token)}`)

      ws.onopen = () => {
        setWsStatus('connected')
        refreshBeads()
        refreshStats()
      }

      ws.onmessage = ({ data }) => {
        let msg
        try { msg = JSON.parse(data) } catch { return }
        if (msg.type === 'ping') { ws.send('pong'); return }
        if (msg.type === 'beads_list') { setBeads(msg.beads ?? []); return }
        if (msg.type === 'channels_map') { setChannelsMap(msg.channels ?? {}); return }
        handleFrameRef.current(data, msg)
      }

      ws.onclose = async ({ code }) => {
        ws = null
        if (closed) return
        if ([4001, 4003, 1008, 3000].includes(code)) {
          setWsStatus('connecting')
          if (await tryReauth()) { connect(); return }
          setWsStatus('auth_error')
          onAuthErrorRef.current?.()
          return
        }
        if (code === 4002) { setWsStatus('full'); return }
        setWsStatus('disconnected')
        timer = setTimeout(connect, RECONNECT_MS)
      }

      ws.onerror = () => {}
    }

    connect()
    return () => {
      closed = true
      clearTimeout(timer)
      ws?.close(1000)
    }
  }, [refreshBeads, refreshStats])

  // ---- Background polls ----------------------------------------------------

  useEffect(() => {
    refreshChannels()
    const a = setInterval(refreshChannels, CHANNELS_POLL_MS)
    const b = setInterval(refreshStats, STATS_POLL_MS)
    return () => { clearInterval(a); clearInterval(b) }
  }, [refreshChannels, refreshStats])

  return { events, channelsMap, channelStats, beads, beadSnippets, wsStatus, viewers }
}
