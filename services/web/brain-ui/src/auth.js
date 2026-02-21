export const getToken = () => localStorage.getItem('brain_token')
export const setToken = (t) => localStorage.setItem('brain_token', t)
export const clearToken = () => localStorage.removeItem('brain_token')
export const getPassphrase = () => localStorage.getItem('brain_passphrase')
export const setPassphrase = (p) => localStorage.setItem('brain_passphrase', p)
export const clearPassphrase = () => localStorage.removeItem('brain_passphrase')

export function authHeaders() {
  const t = getToken()
  return t ? { Authorization: `Bearer ${t}` } : {}
}

export async function authenticate(code) {
  const res = await fetch('/api/brain/auth', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ code }),
  })
  if (!res.ok) {
    const err = await res.json().catch(() => ({}))
    throw new Error(err.detail || 'Invalid code')
  }
  const { token } = await res.json()
  setToken(token)
  setPassphrase(code)
}

export async function tryReauth() {
  const p = getPassphrase()
  if (!p) return false
  try {
    await authenticate(p)
    return true
  } catch {
    clearPassphrase()
    return false
  }
}
