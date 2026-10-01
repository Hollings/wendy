import { useEffect, useRef, useState } from 'react'
import { clearAuth, fetchBrain, getToken, tryReauth } from './auth'
import { appendEvents, frameKey, frameModel, frameUsage, parseFrame } from './events'
const MAX_EVENTS = 1200
const INITIAL = { events: [], channelsMap: {}, channelStats: {}, beads: [], wsStatus: 'connecting', viewers: null, syncError: '', received: 0 }
export function useBrainStore({ onAuthError }) {
  const [state, setState] = useState(INITIAL)
  const [retry, setRetry] = useState(0)
  const authError = useRef(onAuthError)
  const seenRef = useRef(new Set())
  authError.current = onAuthError
  useEffect(() => {
    let disposed = false, socket = null, reconnectTimer = null, attempts = 0, pollTimer = null
    let batch = [], flushTimer = null
    const seen = seenRef.current
    const abort = new AbortController()
    const failAuth = () => { if (!disposed) { clearAuth(); authError.current?.() } }
    function flush() {
      flushTimer = null
      const frames = batch
      batch = []
      if (disposed || !frames.length) return
      setState(previous => {
        let events = previous.events, received = previous.received
        const stats = { ...previous.channelStats }
        for (const [raw, key] of frames) {
          const parsed = parseFrame(raw, key)
          events = appendEvents(events, parsed)
          received += parsed.length
          if (raw.channel_id && !raw.bead_id) {
            const id = String(raw.channel_id)
            const current = stats[id] || { count: 0, tokens: null, model: null, lastTs: 0 }
            const ts = parsed[0]?.ts || current.lastTs
            stats[id] = { ...current, count: current.count + parsed.length, lastTs: Math.max(current.lastTs, ts),
              ...(ts >= current.lastTs ? { tokens: frameUsage(raw) ?? current.tokens, model: frameModel(raw) ?? current.model } : {}) }
          }
        }
        return { ...previous, events: events.slice(-MAX_EVENTS), channelStats: stats, received }
      })
    }
    async function poll() {
      const results = await Promise.allSettled(['/api/brain/channels', '/api/brain/beads', '/api/brain/stats'].map(path => fetchBrain(path, { signal: abort.signal })))
      if (disposed) return
      if (results.some(result => result.status === 'rejected' && result.reason.status === 401)) { failAuth(); return }
      setState(previous => {
        const next = { ...previous, syncError: results.some(result => result.status === 'rejected') ? 'Some status data is unavailable. Last known values are shown.' : '' }
        if (results[0].status === 'fulfilled') next.channelsMap = results[0].value.channels || {}
        if (results[1].status === 'fulfilled') next.beads = results[1].value.beads || []
        if (results[2].status === 'fulfilled') next.viewers = results[2].value.viewers ?? null
        return next
      })
      pollTimer = setTimeout(poll, 30_000)
    }
    function schedule(status) {
      if (disposed) return
      setState(previous => ({ ...previous, wsStatus: status }))
      reconnectTimer = setTimeout(connect, Math.min(30_000, 1500 * 2 ** Math.min(attempts++, 5)))
    }
    async function connect() {
      if (disposed) return
      if (!getToken()) { failAuth(); return }
      setState(previous => ({ ...previous, wsStatus: 'connecting' }))
      const protocol = location.protocol === 'https:' ? 'wss:' : 'ws:'
      const current = new WebSocket(protocol + '//' + location.host + '/ws/brain?token=' + encodeURIComponent(getToken()))
      socket = current
      current.onopen = () => {
        if (disposed) { current.close(); return }
        setState(previous => ({ ...previous, wsStatus: 'connected' }))
      }
      current.onmessage = ({ data }) => {
        if (disposed) return
        let raw
        try { raw = JSON.parse(data) } catch { return }
        if (!raw || typeof raw !== 'object') return
        attempts = 0 // A real frame, not merely an accepted then capacity-closed socket.
        if (raw.type === 'ping') { if (current.readyState === WebSocket.OPEN) current.send('pong'); return }
        if (raw.type === 'channels_map') { setState(previous => ({ ...previous, channelsMap: raw.channels || {} })); return }
        if (raw.type === 'beads_list') { setState(previous => ({ ...previous, beads: raw.beads || [] })); return }
        const key = frameKey(data)
        if (seen.has(key)) return
        seen.add(key)
        if (seen.size > 8000) for (const value of [...seen].slice(0, 4000)) seen.delete(value)
        batch.push([raw, key])
        if (!flushTimer) flushTimer = setTimeout(flush, 75)
      }
      current.onclose = async ({ code }) => {
        if (disposed) return
        flush()
        if ([4001, 4003, 1008, 3000].includes(code)) {
          if (await tryReauth()) { if (!disposed) connect() } else failAuth()
          return
        }
        schedule(code === 4002 ? 'full' : 'disconnected')
      }
      current.onerror = () => {}
    }
    poll()
    connect()
    return () => {
      disposed = true
      abort.abort()
      clearTimeout(pollTimer)
      clearTimeout(reconnectTimer)
      clearTimeout(flushTimer)
      socket?.close(1000)
    }
  }, [retry])
  return { ...state, reconnect: () => setRetry(value => value + 1) }
}
