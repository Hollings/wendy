import { useEffect, useRef, useState } from 'react'
import { clearAuth, fetchBrain } from './auth'
import { appendEvents, frameKey, parseFrame } from './events'
const EMPTY = { taskId: null, events: [], error: '', loading: false, truncated: false }
export function useTaskLog(taskId, onAuthError) {
  const [state, setState] = useState(EMPTY)
  const authError = useRef(onAuthError)
  authError.current = onAuthError
  useEffect(() => {
    if (!taskId) { setState(EMPTY); return }
    let stopped = false, timer = null, offset = 0, logId = '', events = [], tail = '', truncated = false
    const seen = new Set(), abort = new AbortController()
    setState({ ...EMPTY, taskId, loading: true })
    async function poll() {
      try {
        const data = await fetchBrain('/api/brain/beads/' + encodeURIComponent(taskId) + '/log?offset=' + offset + '&log_id=' + encodeURIComponent(logId), { signal: abort.signal })
        if (stopped) return
        if (!data.log_id && logId) {
          timer = setTimeout(poll, 4000)
          return // Keep the last good history through a transient missing file.
        }
        if (data.log_id !== logId || data.offset < offset) { events = []; tail = ''; truncated = false; seen.clear() }
        truncated = truncated || !!data.truncated
        logId = data.log_id || ''
        offset = data.offset || 0
        const lines = (tail + (data.log || '')).split('\n')
        tail = lines.pop()
        for (const line of lines) {
          let raw
          try { raw = JSON.parse(line) } catch { continue }
          if (!raw || typeof raw !== 'object') continue
          const recordedAt = raw.ts || raw.timestamp || raw.event?.timestamp
          const envelope = { event: raw.event || raw, ts: recordedAt || data.modified_at || Date.now(), bead_id: taskId, channel_id: null, attempt_id: data.attempt_id || null, timestamp_estimated: !recordedAt }
          const key = frameKey(JSON.stringify(envelope))
          if (seen.has(key)) continue
          seen.add(key)
          events = appendEvents(events, parseFrame(envelope, key)).slice(-1200)
        }
        if (seen.size > 8000) for (const key of [...seen].slice(0, 4000)) seen.delete(key)
        setState({ taskId, events, error: '', loading: false, truncated })
      } catch (error) {
        if (stopped) return
        if (error.status === 401) { clearAuth(); authError.current?.(); return }
        setState(previous => ({ ...previous, error: error.message, loading: false }))
      }
      if (!stopped) timer = setTimeout(poll, 4000)
    }
    poll()
    return () => { stopped = true; clearTimeout(timer); abort.abort() }
  }, [taskId])
  return state.taskId === taskId ? state : { ...EMPTY, taskId, loading: !!taskId }
}
