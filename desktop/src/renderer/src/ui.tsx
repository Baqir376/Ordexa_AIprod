// Small shared building blocks for the back-office screens.

import { useCallback, useEffect, useState, type ReactNode } from 'react'

import { formatRs, toPaisa } from '../../core/money'
import { ordexa } from './bridge'

export const uid = (): string => crypto.randomUUID()
export const today = (): string => new Date().toISOString().slice(0, 10)

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

export function Page({ title, actions, children }: { title: string; actions?: ReactNode; children: ReactNode }) {
  return (
    <div className="page">
      <div className="page-head">
        <h1>{title}</h1>
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
    <div className={`stat card ${tone ?? ''}`}>
      <div className="stat-label">{label}</div>
      <div className="stat-value">{value}</div>
    </div>
  )
}

export function can(perms: string[] | undefined, p: string): boolean {
  return Boolean(perms?.includes(p))
}
