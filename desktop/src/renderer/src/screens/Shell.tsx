import { useEffect, useState, type ReactNode } from 'react'

import type { AppState } from '../../../core/types'
import { ordexa } from '../bridge'
import { can } from '../ui'
import { Staff, Settings } from './Admin'
import { AiOrders } from './AiOrders'
import { Billing } from './Billing'
import { Customers } from './Customers'
import { Dashboard } from './Dashboard'
import { Cheques, Expenses, Returns } from './Finance'
import { Invoices } from './Invoices'
import { Products } from './Products'
import { Purchases, Suppliers } from './Purchases'
import { Reports } from './Reports'
import { Registrations } from './Registrations'

export function BrandHeader({ state, large = false }: { state: AppState; large?: boolean }) {
  const b = state.branding
  return (
    <div className={`brand ${large ? 'large' : ''}`}>
      {b?.logo_data_url ? <img src={b.logo_data_url} alt="" className="logo" /> : <div className="logo placeholder">{(b?.display_name ?? 'O')[0]}</div>}
      <div>
        <div className="brand-name">{b?.display_name ?? 'Ordexa ERP'}</div>
        <div className="brand-sub">{b?.license_text ?? ''}</div>
      </div>
    </div>
  )
}

function SyncPill({ state }: { state: AppState }) {
  const s = state.sync
  const label = { syncing: 'Syncing…', offline: 'Offline', auth_required: 'Sign in to sync', error: 'Sync error', idle: 'Online' }[s.state]
  const tone = s.state === 'idle' || s.state === 'syncing' ? 'ok' : s.state === 'offline' ? 'warn' : 'bad'
  return (
    <button className={`pill ${tone}`} title={s.message ?? 'F5 to sync now'} onClick={() => void ordexa().syncNow()}>
      <span className="dot" />
      {label}
      {s.pending > 0 && <span className="count">{s.pending} waiting</span>}
      {s.attention > 0 && <span className="count bad">{s.attention} need attention</span>}
    </button>
  )
}

interface NavItem { key: string; label: string; perm?: string; key_hint?: string; online?: boolean; group: string }

const NAV: NavItem[] = [
  { key: 'billing', label: 'New bill', perm: 'invoice.create', key_hint: 'F2', group: 'Counter' },
  { key: 'bills', label: 'Bills & receipts', key_hint: 'F3', group: 'Counter' },
  { key: 'ai', label: 'WhatsApp orders', perm: 'sales_order.view', key_hint: 'F6', online: true, group: 'Counter' },
  { key: 'dashboard', label: 'Dashboard', perm: 'reports.view', key_hint: 'F7', online: true, group: 'Business' },
  { key: 'customers', label: 'Customers', perm: 'customer.view', online: true, group: 'Business' },
  { key: 'registrations', label: 'WhatsApp sign-ups', perm: 'customer.create', online: true, group: 'Business' },
  { key: 'products', label: 'Products & stock', perm: 'product.view', online: true, group: 'Business' },
  { key: 'purchases', label: 'Purchases', perm: 'purchase_order.view', online: true, group: 'Business' },
  { key: 'suppliers', label: 'Suppliers', perm: 'purchase_order.view', online: true, group: 'Business' },
  { key: 'returns', label: 'Sales returns', perm: 'invoice.view', online: true, group: 'Money' },
  { key: 'cheques', label: 'Cheques', perm: 'finance.view', online: true, group: 'Money' },
  { key: 'expenses', label: 'Expenses', perm: 'finance.view', online: true, group: 'Money' },
  { key: 'reports', label: 'Reports', perm: 'reports.view', key_hint: 'F8', online: true, group: 'Money' },
  { key: 'staff', label: 'Staff', perm: 'user.manage', online: true, group: 'Admin' },
  { key: 'settings', label: 'Settings', perm: 'settings.configure', online: true, group: 'Admin' },
]

export function Shell({ state, refresh }: { state: AppState; refresh: () => void }) {
  const perms = state.user?.permissions
  const visible = NAV.filter((n) => !n.perm || can(perms, n.perm))
  const [tab, setTab] = useState<string>(visible[0]?.key ?? 'bills')
  const [sub, setSub] = useState<string | undefined>()

  const go = (t: string) => {
    const [k, s] = t.split(':')
    setTab(k)
    setSub(s)
  }

  useEffect(() => {
    const keys: Record<string, string> = { F2: 'billing', F3: 'bills', F6: 'ai', F7: 'dashboard', F8: 'reports' }
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'F5') {
        e.preventDefault()
        void ordexa().syncNow()
      } else if (keys[e.key] && visible.some((n) => n.key === keys[e.key])) {
        e.preventDefault()
        go(keys[e.key])
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [visible])

  const user = state.user!
  const offlineScreen = !user.online && visible.find((n) => n.key === tab)?.online
  let body: ReactNode
  if (offlineScreen) body = <div className="empty">This screen needs the server. You signed in offline: billing and bills work; sign in again when the internet is back.</div>
  else if (tab === 'billing') body = <Billing state={state} />
  else if (tab === 'bills') body = <Invoices state={state} />
  else if (tab === 'ai') body = <AiOrders state={state} />
  else if (tab === 'dashboard') body = <Dashboard go={go} />
  else if (tab === 'customers') body = <Customers state={state} />
  else if (tab === 'registrations') body = <Registrations />
  else if (tab === 'products') body = <Products state={state} />
  else if (tab === 'purchases') body = <Purchases state={state} />
  else if (tab === 'suppliers') body = <Suppliers state={state} />
  else if (tab === 'returns') body = <Returns state={state} />
  else if (tab === 'cheques') body = <Cheques state={state} />
  else if (tab === 'expenses') body = <Expenses state={state} />
  else if (tab === 'reports') body = <Reports key={sub ?? 'reports'} initial={sub} />
  else if (tab === 'staff') body = <Staff />
  else if (tab === 'settings') body = <Settings state={state} />

  const groups = [...new Set(visible.map((n) => n.group))]
  return (
    <div className="shell">
      <header className="topbar">
        <BrandHeader state={state} />
        <div className="topbar-right">
          <SyncPill state={state} />
          <div className="user">
            {user.full_name}
            {!user.online && <span className="muted small"> (offline sign-in)</span>}
          </div>
          <button className="btn ghost" onClick={() => void ordexa().logout().then(refresh)}>
            Sign out
          </button>
        </div>
      </header>
      {state.sync.message && state.sync.state !== 'idle' && <div className={`banner ${state.sync.state}`}>{state.sync.message}</div>}
      <div className="body">
        <nav className="sidebar" aria-label="Modules">
          {groups.map((g) => (
            <div key={g} className="nav-group">
              <div className="nav-title">{g}</div>
              {visible.filter((n) => n.group === g).map((n) => (
                <button key={n.key} className={tab === n.key ? 'active' : ''} onClick={() => go(n.key)}>
                  <span>{n.label}</span>
                  {n.key_hint && <kbd>{n.key_hint}</kbd>}
                </button>
              ))}
            </div>
          ))}
        </nav>
        <main className="content">{body}</main>
      </div>
      <footer className="statusbar">
        <span>
          {state.device_name} · series {state.terminal_code}-
        </span>
        <span>
          <kbd>F2</kbd> new bill <kbd>F3</kbd> bills <kbd>F4</kbd> customer <kbd>F9</kbd> save <kbd>Esc</kbd> clear <kbd>F5</kbd> sync
        </span>
        <span>{state.branding?.powered_by ?? 'Powered by Ordexa ERP'}</span>
      </footer>
    </div>
  )
}
