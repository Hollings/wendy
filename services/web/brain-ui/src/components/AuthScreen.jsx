import { useState } from 'react'
import { authenticate } from '../auth'
export default function AuthScreen({ onAuth }) {
  const [code, setCode] = useState(''), [error, setError] = useState(''), [loading, setLoading] = useState(false)
  async function submit(event) {
    event.preventDefault()
    if (!code.trim() || loading) return
    setLoading(true); setError('')
    try { await authenticate(code.trim()); onAuth() }
    catch (err) { setError(err.message) }
    finally { setLoading(false) }
  }
  return <div className="auth-screen">
    <form className="auth-form" onSubmit={submit}>
      <h1>wendy / brain</h1><p>Execution log &amp; model diagnostics</p>
      <label htmlFor="access-code">Access code</label>
      <input id="access-code" type="password" value={code} onChange={event => setCode(event.target.value)} autoComplete="current-password" autoFocus disabled={loading} aria-describedby={error ? 'auth-error' : undefined} aria-invalid={!!error} />
      <button className="auth-submit" disabled={loading || !code.trim()} type="submit">{loading ? 'Authenticating…' : 'Connect'}</button>
      {error && <div id="auth-error" className="auth-error" role="alert">{error}</div>}
    </form>
  </div>
}
