import { useState, useEffect } from 'react'
import AuthScreen from './components/AuthScreen'
import BrainApp from './components/BrainApp'
import { getToken, getPassphrase, tryReauth } from './auth'

export default function App() {
  const [authed, setAuthed] = useState(false)
  const [checking, setChecking] = useState(true)

  useEffect(() => {
    async function check() {
      if (getToken()) {
        setAuthed(true)
      } else if (getPassphrase()) {
        const ok = await tryReauth()
        if (ok) setAuthed(true)
      }
      setChecking(false)
    }
    check()
  }, [])

  if (checking) return null

  return authed
    ? <BrainApp onLogout={() => setAuthed(false)} />
    : <AuthScreen onAuth={() => setAuthed(true)} />
}
