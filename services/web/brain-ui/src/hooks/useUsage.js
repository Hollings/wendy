import { useState, useEffect } from 'react'
import { authHeaders } from '../auth'

/** Polls /api/brain/usage every 5 minutes and returns the latest data object. */
export function useUsage() {
  const [usage, setUsage] = useState(null)

  async function load() {
    try {
      const res = await fetch('/api/brain/usage', { headers: authHeaders() })
      if (!res.ok) return
      const data = await res.json()
      if (data.available) setUsage(data)
    } catch {
      // ignore
    }
  }

  useEffect(() => {
    load()
    const id = setInterval(load, 5 * 60 * 1000)
    return () => clearInterval(id)
  }, [])

  return usage
}
