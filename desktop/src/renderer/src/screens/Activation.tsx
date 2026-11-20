import { useState, type FormEvent } from 'react'

import type { AppState } from '../../../core/types'
import { ordexa } from '../bridge'

export function Activation({ onDone }: { onDone: (s: AppState) => void }) {
  const [form, setForm] = useState({
    serverUrl: 'http://127.0.0.1:8000',
    tenantCode: '',
    username: 'admin',
    password: '',
    deviceName: 'Counter PC',
  })
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const set = (k: keyof typeof form) => (e: { target: { value: string } }) => setForm({ ...form, [k]: e.target.value })

  async function submit(e: FormEvent) {
    e.preventDefault()
    setBusy(true)
    setError(null)
    try {
      onDone(await ordexa().activate(form))
    } catch (err) {
      setError((err as Error).message)
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="center-page">
      <form className="card auth-card" onSubmit={submit}>
        <div className="brand-mark">Ordexa ERP</div>
        <h1>Activate this PC</h1>
        <p className="muted">An administrator signs in once. This PC then gets its own bill series and works offline.</p>
        <label>
          Server address
          <input value={form.serverUrl} onChange={set('serverUrl')} required />
        </label>
        <label>
          Business code
          <input value={form.tenantCode} onChange={set('tenantCode')} placeholder="e.g. ali-chemist" autoFocus required />
        </label>
        <div className="row-2">
          <label>
            Administrator
            <input value={form.username} onChange={set('username')} required />
          </label>
          <label>
            Password
            <input type="password" value={form.password} onChange={set('password')} required />
          </label>
        </div>
        <label>
          Name for this PC
          <input value={form.deviceName} onChange={set('deviceName')} required />
        </label>
        {error && <div className="alert error">{error}</div>}
        <button className="btn primary block" disabled={busy}>
          {busy ? 'Activating…' : 'Activate'}
        </button>
      </form>
    </div>
  )
}
