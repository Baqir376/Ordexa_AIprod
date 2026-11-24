// Small shared building blocks for the back-office screens.

import { useCallback, useEffect, useLayoutEffect, useRef, useState, type ReactNode } from 'react'

import { formatRs, toPaisa } from '../../core/money'
import { ordexa } from './bridge'

export const uid = (): string => crypto.randomUUID()
/** Local calendar date on this PC (Pakistan time), not the UTC date. */
export const today = (): string => {
  const d = new Date()
  return new Date(d.getTime() - d.getTimezoneOffset() * 60_000).toISOString().slice(0, 10)
}

/** Format a server decimal string ("1234.50") as "Rs 1,234.50". */
export function rs(v: string | number | null | undefined): string {
  if (v === null || v === undefined || v === '') return '—'
  try {
    return formatRs(toPaisa(String(v)))
  } catch {
    return String(v)
  }
}

export function qtyText(v: string | null | undefined): string {
  if (!v) return '—'
  const n = Number(v)
  return Number.isInteger(n) ? String(n) : String(n)
}

export function useApi<T>(path: string | null, deps: unknown[] = []) {
  const [data, setData] = useState<T | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(false)
  const load = useCallback(async () => {
    if (!path) return
    setLoading(true)
    setError(null)
    try {
      setData(await ordexa().request<T>('GET', path))
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setLoading(false)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [path, ...deps])
  useEffect(() => {
    void load()
  }, [load])
  return { data, error, loading, reload: load }
}

/** A "slider" that glides to the active child of a container (tabs, sidebar): put <span className="slider" /> as the
 *  container's first child and give the active child the class "active". Follows resizes and scroll content. */
export function useSlider<T extends HTMLElement>(dep: unknown) {
  const ref = useRef<T>(null)
  useLayoutEffect(() => {
    const el = ref.current
    if (!el) return
    const place = () => {
      const ind = el.querySelector<HTMLElement>(':scope > .slider')
      const a = el.querySelector<HTMLElement>('.active')
      if (!ind) return
      if (!a) {
        ind.style.opacity = '0'
        return
      }
      const box = el.getBoundingClientRect()
      const r = a.getBoundingClientRect()
      ind.style.opacity = '1'
      ind.style.width = `${r.width}px`
      ind.style.height = `${r.height}px`
      ind.style.transform = `translate(${r.left - box.left + el.scrollLeft}px, ${r.top - box.top + el.scrollTop}px)`
    }
    place()
    const ro = new ResizeObserver(place)
    ro.observe(el)
    const t = setTimeout(place, 60) // after fonts / badges settle
    return () => {
      ro.disconnect()
      clearTimeout(t)
    }
  }, [dep])
  return ref
}

export interface Column<T> {
  key: string
  label: string
  num?: boolean
  render?: (row: T) => ReactNode
}

export function Table<T>({ rows, columns, empty = 'Nothing here yet.', onRow }: {
  rows: T[] | null
  columns: Column<T>[]
  empty?: string
  onRow?: (row: T) => void
}) {
  return (
    <table className="lines">
      <thead>
        <tr>
          {columns.map((c) => (
            <th key={c.key} className={c.num ? 'num' : ''}>
              {c.label}
            </th>
          ))}
        </tr>
      </thead>
      <tbody>
        {rows === null && (
          <tr>
            <td colSpan={columns.length} className="empty-row">
              Loading…
            </td>
          </tr>
        )}
        {rows?.length === 0 && (
          <tr>
            <td colSpan={columns.length} className="empty-row">
              {empty}
            </td>
          </tr>
        )}
        {rows?.map((r, i) => (
          <tr key={i} className={onRow ? 'clickable' : ''} onClick={onRow ? () => onRow(r) : undefined}>
            {columns.map((c) => (
              <td key={c.key} className={c.num ? 'num' : ''}>
                {c.render ? c.render(r) : String((r as Record<string, unknown>)[c.key] ?? '')}
              </td>
            ))}
          </tr>
        ))}
      </tbody>
    </table>
  )
}

/** One line under each page title saying what the page is for (so nobody has to guess). */
const PAGE_HELP: [string, string][] = [
  ['Dashboard', "Today's sales, money owed to you and alerts at a glance."],
  ['Customers', 'Your shops: credit limits, what they owe, their ledger, and receiving payments.'],
  ['Products', 'Every medicine you sell: price, unit, stock by batch and reorder level.'],
  ['Purchases', 'Stock you received from suppliers. Receiving adds batches and stock.'],
  ['Suppliers', 'Companies you buy from and what you owe them.'],
  ['Sales returns', 'Goods a shop sent back. A credit note lowers what the shop owes.'],
  ['Cheques', 'Cheques received from shops: mark them cleared or bounced.'],
  ['Expenses', 'Day-to-day spending (rent, fuel, salaries) for the profit report.'],
  ['Reports', 'Sales, stock, ageing and profit reports for any period.'],
  ['Staff', 'Who can sign in and what each person is allowed to do.'],
  ['Settings', 'Your business details, WhatsApp connection, printer, backups and billing PCs.'],
  ['WhatsApp sign-ups', 'Shops asking to order on WhatsApp. Approve a number to link it to a customer.'],
]

export function Page({ title, subtitle, actions, children }: { title: string; subtitle?: string; actions?: ReactNode; children: ReactNode }) {
  const help = subtitle ?? PAGE_HELP.find(([k]) => title.startsWith(k))?.[1]
  return (
    <div className="page">
      <div className="page-head">
        <div>
          <h1>{title}</h1>
          {help && <div className="page-sub">{help}</div>}
        </div>
        <div className="page-actions">{actions}</div>
      </div>
      {children}
    </div>
  )
}

export function Modal({ title, onClose, children, wide = false }: {
  title: string
  onClose: () => void
  children: ReactNode
  wide?: boolean
}) {
  useEffect(() => {
    const k = (e: KeyboardEvent) => e.key === 'Escape' && onClose()
    window.addEventListener('keydown', k)
    return () => window.removeEventListener('keydown', k)
  }, [onClose])
  return (
    <div className="modal-backdrop" onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <div className={`modal card ${wide ? 'wide' : ''}`} role="dialog" aria-modal="true" aria-label={title}>
        <div className="modal-head">
          <h2>{title}</h2>
          <button className="x" onClick={onClose} aria-label="Close">
            ×
          </button>
        </div>
        {children}
      </div>
    </div>
  )
}

export function ErrorBox({ error }: { error: string | null }) {
  return error ? <div className="alert error">{error}</div> : null
}

/** Run an async action with a busy flag and error capture. */
export function useAction() {
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const run = useCallback(async <R,>(fn: () => Promise<R>): Promise<R | undefined> => {
    setBusy(true)
    setError(null)
    try {
      return await fn()
    } catch (e) {
      setError((e as Error).message)
      return undefined
    } finally {
      setBusy(false)
    }
  }, [])
  return { busy, error, setError, run }
}

export function Field({ label, children }: { label: string; children: ReactNode }) {
  return (
    <label>
      {label}
      {children}
    </label>
  )
}

export function Stat({ label, value, tone }: { label: string; value: ReactNode; tone?: 'ok' | 'warn' | 'bad' }) {
  return (
    <div className={`stat card tilt ${tone ?? ''}`}>
      <div className="stat-label">{label}</div>
      <div className="stat-value">{value}</div>
    </div>
  )
}

export function can(perms: string[] | undefined, p: string): boolean {
  return Boolean(perms?.includes(p))
}
