import { useState } from 'react'
import { authenticate } from '../auth'

export default function AuthScreen({ onAuth }) {
  const [code, setCode] = useState('')
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(false)

  async function handleSubmit(e) {
    e.preventDefault()
    if (!code.trim()) return
    setError('')
    setLoading(true)
    try {
      await authenticate(code.trim())
      onAuth()
    } catch (err) {
      setError(err.message)
    } finally {
      setLoading(false)
    }
  }

  return (
    <div className="auth-screen">
      <h1>Wendy's Brain</h1>
      <p>Enter the code to watch Wendy think</p>
      <form onSubmit={handleSubmit} className="auth-form">
        <input
          type="text"
          value={code}
          onChange={e => setCode(e.target.value)}
          placeholder="Code word"
          autoFocus
          autoComplete="off"
          disabled={loading}
        />
        <button type="submit" disabled={loading || !code.trim()}>
          {loading ? '...' : 'Enter'}
        </button>
      </form>
      {error && <div className="auth-error">{error}</div>}
    </div>
  )
}
