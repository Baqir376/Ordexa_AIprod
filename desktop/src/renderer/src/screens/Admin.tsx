import { useEffect, useState } from 'react'

import type { AppState } from '../../../core/types'
import { ordexa } from '../bridge'
import { ErrorBox, Field, Modal, Page, Table, useAction, useApi } from '../ui'
import { WhatsAppConnectionCard } from './WhatsAppConnection'

interface User { id: string; username: string; full_name: string; role: string; is_active: boolean }
interface Catalog { modules: Record<string, string[]>; presets: Record<string, string[]> }
const ADMIN_ONLY = new Set(['user.manage', 'permission.assign', 'settings.configure', 'integration.configure'])

export function Staff() {
  const users = useApi<User[]>('/api/v1/users')
  const catalog = useApi<Catalog>('/api/v1/permissions/catalog')
  const [editing, setEditing] = useState<User | 'new' | null>(null)
  const { error, run } = useAction()
  async function toggle(u: User) {
    await run(() => ordexa().request('PUT', `/api/v1/users/${u.id}/active`, { active: !u.is_active }))
    void users.reload()
  }
  return (
    <Page title="Staff & permissions" actions={<button className="btn primary" onClick={() => setEditing('new')}>+ Add staff</button>}>
      <ErrorBox error={users.error ?? error} />
      <Table
        rows={users.data}
        columns={[
          { key: 'username', label: 'Username' },
          { key: 'full_name', label: 'Name' },
          { key: 'role', label: 'Role', render: (u) => (u.role === 'administrator' ? 'Administrator (all permissions)' : 'Staff') },
          { key: 'is_active', label: 'Status', render: (u) => <span className={`tag ${u.is_active ? 'synced' : 'rejected'}`}>{u.is_active ? 'active' : 'disabled'}</span> },
          { key: 'a', label: '', render: (u) => (
            <span className="row-actions">
              {u.role === 'staff' && <button className="btn small" onClick={() => setEditing(u)}>Permissions</button>}
              <button className="btn small" onClick={() => void toggle(u)}>{u.is_active ? 'Disable' : 'Enable'}</button>
            </span>
          ) },
        ]}
      />
      {editing && catalog.data && (
        <StaffForm user={editing === 'new' ? null : editing} catalog={catalog.data} onClose={() => setEditing(null)} onSaved={() => { setEditing(null); void users.reload() }} />
      )}
    </Page>
  )
}

function StaffForm({ user, catalog, onClose, onSaved }: { user: User | null; catalog: Catalog; onClose: () => void; onSaved: () => void }) {
  const [f, setF] = useState({ username: '', full_name: '', password: '' })
  const [perms, setPerms] = useState<Set<string>>(new Set())
  const { busy, error, run } = useAction()
  const toggle = (p: string) => setPerms((s) => { const n = new Set(s); if (n.has(p)) n.delete(p); else n.add(p); return n })
  async function save() {
    const list = [...perms]
    const r = await run(() =>
      user
        ? ordexa().request('PUT', `/api/v1/users/${user.id}/permissions`, { permissions: list })
        : ordexa().request('POST', '/api/v1/users', { ...f, role: 'staff', permissions: list }),
    )
    if (r) onSaved()
  }
  return (
    <Modal title={user ? `Permissions: ${user.full_name}` : 'Add staff member'} onClose={onClose} wide>
      <div className="form">
        {!user && (
          <div className="row-3">
            <Field label="Username"><input value={f.username} onChange={(e) => setF({ ...f, username: e.target.value })} autoFocus /></Field>
            <Field label="Full name"><input value={f.full_name} onChange={(e) => setF({ ...f, full_name: e.target.value })} /></Field>
            <Field label="Password (min 8)"><input type="password" value={f.password} onChange={(e) => setF({ ...f, password: e.target.value })} /></Field>
          </div>
        )}
        {user && <p className="muted small">Choose the permissions again; this replaces the current set. The change applies on their next action.</p>}
        <div className="presets">
          <span className="muted small">Quick presets:</span>
          {Object.entries(catalog.presets).map(([name, ps]) => (
            <button key={name} className="btn small" onClick={() => setPerms(new Set(ps))}>{name.replace(/_/g, ' ')}</button>
          ))}
          <button className="btn small" onClick={() => setPerms(new Set())}>none</button>
        </div>
        <div className="perm-grid">
          {Object.entries(catalog.modules).map(([mod, ps]) => (
            <fieldset key={mod}>
              <legend>{mod}</legend>
              {ps.filter((p) => !ADMIN_ONLY.has(p)).map((p) => (
                <label key={p} className="check"><input type="checkbox" checked={perms.has(p)} onChange={() => toggle(p)} /> {p.split('.').slice(1).join(' ').replace(/_/g, ' ')} <span className="muted small">{p.split('.')[0].replace(/_/g, ' ')}</span></label>
              ))}
            </fieldset>
          ))}
        </div>
        <ErrorBox error={error} />
        <div className="modal-actions">
          <button className="btn" onClick={onClose}>Cancel</button>
          <button className="btn primary" disabled={busy || (!user && (!f.username || !f.full_name || f.password.length < 8))} onClick={() => void save()}>Save</button>
        </div>
      </div>
    </Modal>
  )
}

interface Tenant { code: string; name: string; branding: { display_name: string; license_text: string; logo_data_url: string | null; primary_color: string; address: string | null; phone: string | null; license_plan: string }; warehouses: { id: string; code: string; name: string }[] }
interface Device { id: string; name: string; terminal_code: string; is_active: boolean; last_sync_at: string | null }

export function Settings({ state }: { state: AppState }) {
  const tenant = useApi<Tenant>('/api/v1/tenant')
  const devices = useApi<Device[]>('/api/v1/devices')
  const [b, setB] = useState<Tenant['branding'] | null>(null)
  const [wh, setWh] = useState({ code: '', name: '' })
  const { busy, error, run } = useAction()
  const brand = b ?? tenant.data?.branding ?? null

  function pickLogo(file: File | undefined) {
    if (!file || !brand) return
    if (file.size > 200_000) return alert('Logo must be under 200 KB.')
    const r = new FileReader()
    r.onload = () => setB({ ...brand, logo_data_url: String(r.result) })
    r.readAsDataURL(file)
  }
  async function saveBranding() {
    if (!brand) return
    const ok = await run(() => ordexa().request('PUT', '/api/v1/tenant/branding', {
      display_name: brand.display_name, license_text: brand.license_text, primary_color: brand.primary_color,
      address: brand.address || null, phone: brand.phone || null, ...(brand.logo_data_url ? { logo_data_url: brand.logo_data_url } : {}),
    }))
    if (ok) { setB(null); void tenant.reload(); void ordexa().syncNow() }
  }
  async function deactivate(d: Device) {
    if (!window.confirm(`Deactivate ${d.name} (${d.terminal_code})? It will stop syncing and free a licence seat.`)) return
    await run(() => ordexa().request('POST', `/api/v1/devices/${d.id}/deactivate`))
    void devices.reload()
  }
  return (
    <Page title="Settings">
      <ErrorBox error={tenant.error ?? error} />
      <div className="settings-grid">
      {brand && (
        <div className="card pad form">
          <div>
            <h2>Business branding</h2>
          </div>
          <p className="muted small">Shown on every PC, the sign-in screen and printed bills. Plan: <b>{brand.license_plan}</b> · business code <b>{tenant.data?.code}</b></p>
          <div className="row-2">
            <Field label="Business name on screen and bills"><input value={brand.display_name} onChange={(e) => setB({ ...brand, display_name: e.target.value })} /></Field>
            <Field label="Licence line"><input value={brand.license_text} onChange={(e) => setB({ ...brand, license_text: e.target.value })} /></Field>
          </div>
          <div className="row-3">
            <Field label="Phone"><input value={brand.phone ?? ''} onChange={(e) => setB({ ...brand, phone: e.target.value })} /></Field>
            <Field label="Address"><input value={brand.address ?? ''} onChange={(e) => setB({ ...brand, address: e.target.value })} /></Field>
            <Field label="Brand colour"><input type="color" value={brand.primary_color} onChange={(e) => setB({ ...brand, primary_color: e.target.value.toUpperCase() })} /></Field>
          </div>
          <div className="logo-row">
            {brand.logo_data_url && <img src={brand.logo_data_url} alt="Current logo" className="logo-preview" />}
            <Field label="Logo (PNG/SVG, under 200 KB)"><input type="file" accept="image/png,image/svg+xml,image/jpeg" onChange={(e) => pickLogo(e.target.files?.[0])} /></Field>
          </div>
          <div className="modal-actions"><button className="btn primary" disabled={busy || !b} onClick={() => void saveBranding()}>Save branding</button></div>
        </div>
      )}
      <PrinterCard />
      <div className="span-2"><WhatsAppConnectionCard /></div>
      <BackupCard />
      <div className="card pad">
        <h2>Billing PCs</h2>
        <p className="section-desc">Every PC that bills for this business, with its own bill series.</p>
        <Table
          rows={devices.data}
          columns={[
            { key: 'terminal_code', label: 'Series' },
            { key: 'name', label: 'PC name', render: (d) => <>{d.name}{d.terminal_code === state.terminal_code && <span className="tag">this PC</span>}</> },
            { key: 'last_sync_at', label: 'Last sync', render: (d) => (d.last_sync_at ? new Date(d.last_sync_at).toLocaleString('en-PK') : 'never') },
            { key: 'is_active', label: '', render: (d) => (d.is_active ? <button className="btn small" onClick={() => void deactivate(d)}>Deactivate</button> : <span className="tag rejected">deactivated</span>) },
          ]}
        />
      </div>
      <div className="card pad form">
        <h2>Warehouses / stores</h2>
        <div className="chips">{tenant.data?.warehouses.map((w) => <span key={w.id} className="chip"><b>{w.code}</b> {w.name}</span>)}</div>
        <div className="inline-form">
          <Field label="Code"><input value={wh.code} onChange={(e) => setWh({ ...wh, code: e.target.value })} /></Field>
          <Field label="Name"><input value={wh.name} onChange={(e) => setWh({ ...wh, name: e.target.value })} /></Field>
          <button className="btn" disabled={busy || !wh.code || !wh.name} onClick={() => void run(() => ordexa().request('POST', '/api/v1/inventory/warehouses', wh)).then((r) => { if (r) { setWh({ code: '', name: '' }); void tenant.reload() } })}>Add warehouse</button>
        </div>
      </div>
      </div>
    </Page>
  )
}

function PrinterCard() {
  const [printers, setPrinters] = useState<string[]>([])
  const [name, setName] = useState<string>('')
  const [auto, setAuto] = useState(false)
  const [saved, setSaved] = useState(false)
  useEffect(() => {
    void ordexa().printers().then(setPrinters)
    void ordexa().getPrinter().then((p) => { setName(p.name ?? ''); setAuto(p.auto) })
  }, [])
  return (
    <div className="card pad form">
      <h2>Receipt printer for this PC</h2>
      <div className="inline-form">
        <label>
          Printer
          <select value={name} onChange={(e) => { setName(e.target.value); setSaved(false) }}>
            <option value="">Ask every time (print dialog)</option>
            {printers.map((p) => <option key={p} value={p}>{p}</option>)}
          </select>
        </label>
        <label className="check"><input type="checkbox" checked={auto} disabled={!name} onChange={(e) => { setAuto(e.target.checked); setSaved(false) }} /> Print automatically when a bill is saved</label>
        <button className="btn" onClick={() => void ordexa().setPrinter(name || null, auto && Boolean(name)).then(() => setSaved(true))}>Save</button>
        {saved && <span className="muted small">Saved for this PC.</span>}
      </div>
    </div>
  )
}

function BackupCard() {
  const [info, setInfo] = useState<{ last: string | null; file: string | null; dir: string } | null>(null)
  const { busy, error, run } = useAction()
  useEffect(() => { void ordexa().backupInfo().then(setInfo) }, [])
  return (
    <div className="card pad form">
      <h2>Backups</h2>
      <p className="muted small">A full copy of your business data is saved on this PC automatically once a day when an administrator signs in. Your data also stays safe on the Ordexa server.</p>
      <div>Last backup: <b>{info?.last ? new Date(info.last).toLocaleString('en-PK') : 'never'}</b></div>
      {info && <div className="muted small">Folder: {info.dir}</div>}
      <ErrorBox error={error} />
      <div className="inline-form">
        <button className="btn primary" disabled={busy} onClick={() => void run(() => ordexa().backupNow()).then(() => ordexa().backupInfo().then(setInfo))}>{busy ? 'Backing up…' : 'Back up now'}</button>
        <button className="btn" onClick={() => void ordexa().openBackups()}>Open backup folder</button>
      </div>
    </div>
  )
}
