import { useEffect, useRef } from 'react'
import { getToken, tryReauth } from '../auth'

/**
 * Manages the brain WebSocket connection with automatic reconnect and re-auth.
 *
 * Callbacks:
 *   onEvent(rawMsg)         - raw WS message (stream event or bead event)
 *   onBeadsList(beads[])    - beads_list envelope
 *   onChannelsMap(channels) - channels_map envelope
 *   onContextUpdate(usage)  - assistant usage object
 *   onStatus(string)        - 'connecting' | 'connected' | 'disconnected' | 'full' | 'auth_error'
 */
export function useWebSocket({ onEvent, onBeadsList, onChannelsMap, onContextUpdate, onStatus }) {
  const wsRef = useRef(null)
  const reconnectRef = useRef(null)
  const cbRef = useRef({})
  cbRef.current = { onEvent, onBeadsList, onChannelsMap, onContextUpdate, onStatus }

  useEffect(() => {
    connect()
    return () => {
      clearTimeout(reconnectRef.current)
      wsRef.current?.close(1000)
    }
  }, [])

  function connect() {
    const token = getToken()
    if (!token) { cbRef.current.onStatus('disconnected'); return }

    cbRef.current.onStatus('connecting')
    const proto = location.protocol === 'https:' ? 'wss:' : 'ws:'
    const ws = new WebSocket(`${proto}//${location.host}/ws/brain?token=${encodeURIComponent(token)}`)
    wsRef.current = ws

    ws.onopen = () => cbRef.current.onStatus('connected')

    ws.onmessage = ({ data }) => {
      try {
        const msg = JSON.parse(data)
        const { onEvent, onBeadsList, onChannelsMap, onContextUpdate } = cbRef.current

        if (msg.type === 'ping') { ws.send('pong'); return }
        if (msg.type === 'beads_list') { onBeadsList(msg.beads ?? []); return }
        if (msg.type === 'channels_map') { onChannelsMap(msg.channels ?? {}); return }

        // Regular or bead stream event
        onEvent(msg)

        // Side-channel: extract usage for context tracking
        const usage = msg.event?.message?.usage
        if (usage) onContextUpdate({ usage, channel_id: msg.channel_id ?? null })
      } catch {
        // ignore parse errors
      }
    }

    ws.onclose = async ({ code }) => {
      wsRef.current = null
      const authErrors = [4001, 4003, 1008, 3000]
      if (authErrors.includes(code)) {
        cbRef.current.onStatus('connecting')
        const ok = await tryReauth()
        if (ok) { connect(); return }
        cbRef.current.onStatus('auth_error')
        return
      }
      if (code === 4002) { cbRef.current.onStatus('full'); return }
      cbRef.current.onStatus('disconnected')
      reconnectRef.current = setTimeout(connect, 3000)
    }

    ws.onerror = () => {}
  }
}
