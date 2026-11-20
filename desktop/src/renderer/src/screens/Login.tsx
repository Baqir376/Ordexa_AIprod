import { useState, type FormEvent } from 'react'

import type { AppState } from '../../../core/types'
import { ordexa } from '../bridge'
import { BrandHeader } from './Shell'

export function Login({ state, onDone }: { state: AppState; onDone: () => void }) {
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  async function submit(e: FormEvent) {
    e.preventDefault()
    setBusy(true)
    setError(null)
    try {
      await ordexa().login(username, password)
      onDone()
    } catch (err) {
      setError((err as Error).message)
      setBusy(false)
    }
  }

  return (
    <div className="center-page">
      <form className="card auth-card" onSubmit={submit}>
        <BrandHeader state={state} large />
        <label>
          Username
          <input value={username} onChange={(e) => setUsername(e.target.value)} autoFocus required />
        </label>
        <label>
          Password
          <input type="password" value={password} onChange={(e) => setPassword(e.target.value)} required />
        </label>
        {error && <div className="alert error">{error}</div>}
        <button className="btn primary block" disabled={busy}>
          {busy ? 'Signing in…' : 'Sign in'}
        </button>
        <p className="muted small center">
          {state.tenant_code} · {state.device_name} ({state.terminal_code}) · {state.branding?.powered_by ?? 'Powered by Ordexa ERP'}
        </p>
      </form>
    </div>
  )
}
