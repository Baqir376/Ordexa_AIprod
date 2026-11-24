// Settings -> WhatsApp connection: the business connects its own Meta WhatsApp number (test or real) from the app.

import { useEffect, useState } from 'react'

import { ordexa } from '../bridge'
import { ErrorBox, Field, useAction } from '../ui'

interface Connection {
  connected: boolean
  phone_number_id?: string
  display_phone?: string
  has_access_token?: boolean
  has_app_secret?: boolean
  verify_token?: string | null
  public_url?: string | null
  webhook_url?: string | null
  webhook_path: string
  last_inbound_at?: string | null
  last_error?: string | null
}

function Copy({ value }: { value: string }) {
  const [done, setDone] = useState(false)
  return (
    <button className="btn small" onClick={() => void navigator.clipboard.writeText(value).then(() => { setDone(true); setTimeout(() => setDone(false), 1500) })}>
      {done ? 'Copied' : 'Copy'}
    </button>
  )
}

export function WhatsAppConnectionCard() {
  const [conn, setConn] = useState<Connection | null>(null)
  const [f, setF] = useState({ phone_number_id: '', access_token: '', app_secret: '', public_url: '' })
  const [testTo, setTestTo] = useState('')
  const [result, setResult] = useState<{ ok: boolean; message: string } | null>(null)
  const [showSteps, setShowSteps] = useState(false)
  const { busy, error, run } = useAction()

  async function load() {
    const c = await ordexa().request<Connection>('GET', '/api/v1/channels/whatsapp/connection')
    setConn(c)
    setF((cur) => ({ ...cur, phone_number_id: c.phone_number_id ?? '', public_url: c.public_url ?? '', access_token: '', app_secret: '' }))
    if (!c.connected) setShowSteps(true)
  }
  useEffect(() => {
    void load()
  }, [])

  async function save() {
    setResult(null)
    const body: Record<string, string> = { phone_number_id: f.phone_number_id.trim(), public_url: f.public_url.trim() }
    if (f.access_token.trim()) body.access_token = f.access_token.trim()
    if (f.app_secret.trim()) body.app_secret = f.app_secret.trim()
    const c = await run(() => ordexa().request<Connection>('PUT', '/api/v1/channels/whatsapp/connection', body))
    if (c) {
      setConn(c)
      setF({ ...f, access_token: '', app_secret: '' })
      setResult({ ok: true, message: 'Saved. Now press "Test connection".' })
    }
  }
  async function act(path: string, body?: unknown) {
    setResult(null)
    const r = await run(() => ordexa().request<{ ok: boolean; message: string }>('POST', `/api/v1/channels/whatsapp/connection/${path}`, body ?? {}))
    if (r) setResult(r)
    void load()
  }
  async function disconnect() {
    if (!window.confirm('Disconnect WhatsApp? The saved token and app secret are deleted; replies go back to simulation.')) return
    const c = await run(() => ordexa().request<Connection>('DELETE', '/api/v1/channels/whatsapp/connection'))
    if (c) setConn(c)
  }

  const webhookUrl = f.public_url.trim() ? `${f.public_url.trim().replace(/\/+$/, '')}${conn?.webhook_path ?? '/webhooks/whatsapp'}` : null
  return (
    <div className="card pad form">
      <div className="page-head">
        <h2>WhatsApp connection</h2>
        {conn && (
          <span className={`tag ${conn.connected ? 'synced' : 'pending'}`}>
            {conn.connected ? `Connected ${conn.display_phone ?? ''}` : 'Not connected (simulator only)'}
          </span>
        )}
      </div>
      {conn?.connected && (
        <div className="muted small">
          Last message received: {conn.last_inbound_at ? new Date(conn.last_inbound_at).toLocaleString('en-PK') : 'none yet'}
        </div>
      )}
      {conn?.last_error && <div className="alert error">Last error from Meta: {conn.last_error}</div>}

      <button className="btn ghost small" onClick={() => setShowSteps(!showSteps)}>{showSteps ? 'Hide' : 'Show'} setup steps</button>
      {showSteps && (
        <ol className="steps small">
          <li>Open <b>developers.facebook.com</b> → <b>My Apps → Create App</b> (type <i>Business</i>) → add the <b>WhatsApp</b> product.</li>
          <li>In <b>WhatsApp → API Setup</b>: copy the <b>Phone number ID</b> and the <b>access token</b>. Under <b>To</b>, add the phones you will test from (up to 5).</li>
          <li>In <b>App settings → Basic</b>: copy the <b>App secret</b>.</li>
          <li>Make this server reachable from the internet: run <code>scripts\start-tunnel.ps1</code> on this PC and paste the <code>https://…trycloudflare.com</code> address below (or your Render address after deploying).</li>
          <li>Fill the fields below and press <b>Save</b>, then <b>Test connection</b>.</li>
          <li>In Meta <b>WhatsApp → Configuration → Webhook</b>: paste the <b>Callback URL</b> and <b>Verify token</b> shown below, press <i>Verify and save</i>, then subscribe to the <b>messages</b> field.</li>
          <li>Send a WhatsApp message (e.g. "hi") from your phone to the test number. It appears in <b>WhatsApp sign-ups</b>; orders appear in <b>WhatsApp orders</b>.</li>
        </ol>
      )}

      <div className="row-2">
        <Field label="Phone number ID (from Meta, digits only)">
          <input value={f.phone_number_id} onChange={(e) => setF({ ...f, phone_number_id: e.target.value })} placeholder="e.g. 123456789012345" />
        </Field>
        <Field label="Public server address (https)">
          <input value={f.public_url} onChange={(e) => setF({ ...f, public_url: e.target.value })} placeholder="https://xxxx.trycloudflare.com" />
        </Field>
      </div>
      <div className="row-2">
        <Field label={`Access token ${conn?.has_access_token ? '(saved; leave empty to keep)' : ''}`}>
          <input type="password" autoComplete="off" value={f.access_token} onChange={(e) => setF({ ...f, access_token: e.target.value })} placeholder={conn?.has_access_token ? '••••••••' : 'EAAG…'} />
        </Field>
        <Field label={`App secret ${conn?.has_app_secret ? '(saved; leave empty to keep)' : ''}`}>
          <input type="password" autoComplete="off" value={f.app_secret} onChange={(e) => setF({ ...f, app_secret: e.target.value })} placeholder={conn?.has_app_secret ? '••••••••' : 'from App settings → Basic'} />
        </Field>
      </div>
      <div className="inline-form">
        <button className="btn primary" disabled={busy || !f.phone_number_id.trim()} onClick={() => void save()}>Save</button>
        <button className="btn" disabled={busy || !conn?.connected} onClick={() => void act('test')}>Test connection</button>
        {conn?.phone_number_id && <button className="btn danger" disabled={busy} onClick={() => void disconnect()}>Disconnect</button>}
      </div>
      {conn?.verify_token && (
        <div className="copy-box">
          <div className="muted small">Paste these into Meta → WhatsApp → Configuration → Webhook:</div>
          <div className="copy-row"><span>Callback URL</span><code>{webhookUrl ?? 'enter the public server address first'}</code>{webhookUrl && <Copy value={webhookUrl} />}</div>
          <div className="copy-row"><span>Verify token</span><code>{conn.verify_token}</code><Copy value={conn.verify_token} /></div>
        </div>
      )}
      {conn?.connected && (
        <div className="inline-form">
          <Field label="Send Meta's 'hello world' test message to (an allowed test phone)">
            <input value={testTo} onChange={(e) => setTestTo(e.target.value)} placeholder="+923001234567" />
          </Field>
          <button className="btn" disabled={busy || testTo.length < 6} onClick={() => void act('send-test', { to: testTo })}>Send test message</button>
        </div>
      )}
      {result && <div className={`alert ${result.ok ? 'ok' : 'error'}`}>{result.message}</div>}
      <ErrorBox error={error} />
      <p className="muted small">The access token and app secret are stored encrypted on the server and are never shown again.</p>
    </div>
  )
}
