import { useEffect, useState, type ReactNode } from 'react'

import type { AppState } from '../../../core/types'
import { ordexa } from '../bridge'
import { Icon } from '../icons'
import { can } from '../ui'
import { Staff, Settings } from './Admin'
import { AiOrders, deskSeenAt } from './AiOrders'
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
  const notice = useNotice(state)
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
  const todo = notice?.new?.total ?? 0
  return (
    <div className="shell">
      <header className="topbar">
        <BrandHeader state={state} />
        <div className="topbar-right">
          {notice && <Bell counts={notice} onOpen={() => go('ai')} />}
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
                  <Icon name={n.key} />
                  <span className="nav-label">{n.label}</span>
                  {n.key === 'ai' && todo > 0 && <span className="nav-badge" title="Orders and messages waiting for you">{todo}</span>}
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


interface Notice {
  ready: number
  fix: number
  messages: number
  waiting_shop: number
  new?: { ready: number; fix: number; messages: number; total: number }
}

/** What is waiting for staff on the WhatsApp desk (polled while signed in online). */
function useNotice(state: AppState): Notice | null {
  const [n, setN] = useState<Notice | null>(null)
  const allowed = state.user?.online && can(state.user?.permissions, 'sales_order.view')
  useEffect(() => {
    if (!allowed) return
    let alive = true
    const load = () => {
      const since = deskSeenAt() ?? new Date(Date.now() - 24 * 3600_000).toISOString()
      void ordexa()
        .request<Notice>('GET', `/api/v1/ai/orders/counts?since=${encodeURIComponent(since)}`)
        .then((r) => alive && setN(r), () => undefined)
    }
    load()
    const t = setInterval(load, 15_000)
    window.addEventListener('ordexa:desk-seen', load) // opening the desk clears the badge at once
    return () => { alive = false; clearInterval(t); window.removeEventListener('ordexa:desk-seen', load) }
  }, [allowed])
  return allowed ? n : null
}

function Bell({ counts, onOpen }: { counts: Notice; onOpen: () => void }) {
  const [open, setOpen] = useState(false)
  const total = counts.new?.total ?? 0
  const rows: [string, number, number, string][] = [
    ['Ready to approve', counts.ready, counts.new?.ready ?? 0, 'ok'],
    ['Need your fix', counts.fix, counts.new?.fix ?? 0, 'warn'],
    ['Messages from shops', counts.messages, counts.new?.messages ?? 0, 'bad'],
    ['Waiting for the shop to reply', counts.waiting_shop, 0, 'info'],
  ]
  return (
    <div className="bell-wrap">
      <button className={`bell ${total ? 'has' : ''}`} onClick={() => setOpen(!open)} aria-label={`${total} items waiting`} title="WhatsApp desk">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.8} strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
          <path d="M18 8a6 6 0 0 0-12 0c0 7-3 9-3 9h18s-3-2-3-9" />
          <path d="M13.7 21a2 2 0 0 1-3.4 0" />
        </svg>
        {total > 0 && <span className="bell-count">{total > 99 ? '99+' : total}</span>}
      </button>
      {open && (
        <div className="bell-pop card" onMouseLeave={() => setOpen(false)}>
          <div className="bell-title">{total ? `${total} new since you last looked` : 'Nothing new'}</div>
          {rows.map(([label, n, fresh, tone]) => (
            <button key={label} className="bell-row" onClick={() => { setOpen(false); onOpen() }}>
              <span className={`dot-${tone}`} />
              <span>{label}</span>
              {fresh > 0 && <em className="bell-new">{fresh} new</em>}
              <b>{n}</b>
            </button>
          ))}
          <div className="bell-foot">Opening WhatsApp orders marks everything as seen.</div>
        </div>
      )}
    </div>
  )
}
