const read = key => { try { return localStorage.getItem(key) } catch { return null } }
const write = (key, value) => { try { localStorage.setItem(key, value) } catch { /* Storage may be unavailable. */ } }
const remove = key => { try { localStorage.removeItem(key) } catch { /* Already unavailable. */ } }
export const getToken = () => read('brain_token')
export const setToken = token => write('brain_token', token)
export const clearToken = () => remove('brain_token')
export const getPassphrase = () => read('brain_passphrase')
export const clearPassphrase = () => remove('brain_passphrase')
export const clearAuth = () => { clearToken(); clearPassphrase() }
export const authHeaders = () => getToken() ? { Authorization: 'Bearer ' + getToken() } : {}
export async function authenticate(code) {
  const response = await fetch('/api/brain/auth', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ code }) })
  const data = await response.json().catch(() => ({}))
  if (!response.ok) throw new Error(data.detail || 'Unable to sign in. Please try again.')
  if (!data.token) throw new Error('The server did not return a session.')
  setToken(data.token)
  clearPassphrase()
}
let reauth = null
export function tryReauth() {
  if (reauth) return reauth
  const code = getPassphrase()
  if (!code) return Promise.resolve(false)
  reauth = authenticate(code).then(() => true).catch(() => { clearAuth(); return false }).finally(() => { reauth = null })
  return reauth
}
export async function fetchBrain(url, { signal } = {}) {
  let response = await fetch(url, { headers: authHeaders(), signal })
  if (response.status === 401 && await tryReauth()) response = await fetch(url, { headers: authHeaders(), signal })
  if (!response.ok) {
    const error = new Error(response.status === 401 ? 'Your session has expired. Sign in again.' : 'Could not load this data. Retrying automatically.')
    error.status = response.status
    throw error
  }
  return response.json()
}
