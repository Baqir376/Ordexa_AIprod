// Counter billing. Keyboard flow: type product -> Enter -> qty -> Enter (line added, back to search)
// ... F9 save -> Enter confirms warnings -> receipt (Ctrl+P prints) -> Esc for the next bill.

import { useEffect, useRef, useState, type KeyboardEvent } from 'react'
import { flushSync } from 'react-dom'

import { formatRs, lineTotal, toMilli, toPaisa } from '../../../core/money'
import type { AppState, BatchInfo, CustomerHit, InvoiceView, ProductHit, Warning } from '../../../core/types'
import { ordexa } from '../bridge'
import { Receipt } from './Receipt'

interface Line {
  key: number
  product: ProductHit
  batches: BatchInfo[]
  batchId: string | null // null = automatic (earliest expiry first)
  qty: string
  bonus: string
}

function safe<T>(fn: () => T, fallback: T): T {
  try {
    return fn()
  } catch {
    return fallback
  }
}

function linePrice(l: Line): number {
  const b = l.batchId ? l.batches.find((x) => x.id === l.batchId) : l.batches[0]
  return b?.sale_price ?? l.product.sale_price
}

export function Billing({ state }: { state: AppState }) {
  const [customer, setCustomer] = useState<CustomerHit | null>(null)
  const [custQuery, setCustQuery] = useState('')
  const [custHits, setCustHits] = useState<CustomerHit[]>([])
  const [query, setQuery] = useState('')
  const [hits, setHits] = useState<ProductHit[]>([])
  const [hitsFor, setHitsFor] = useState<string | null>(null)
  const [hi, setHi] = useState(0)
  const [picked, setPicked] = useState<{ product: ProductHit; batches: BatchInfo[] } | null>(null)
  const [qty, setQty] = useState('1')
  const [bonus, setBonus] = useState('')
  const [lines, setLines] = useState<Line[]>([])
  const [discount, setDiscount] = useState('')
  const [paid, setPaid] = useState('')
  const [warnings, setWarnings] = useState<Warning[] | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [saved, setSaved] = useState<InvoiceView | null>(null)
  const [busy, setBusy] = useState(false)
  const searchRef = useRef<HTMLInputElement>(null)
  const qtyRef = useRef<HTMLInputElement>(null)
  const custRef = useRef<HTMLInputElement>(null)
  const keySeq = useRef(1)
  const saving = useRef(false) // synchronous guard: Enter can fire the button and the global handler together

  const canBill = state.user?.permissions.includes('invoice.create')

  useEffect(() => {
    const t = setTimeout(() => void ordexa().searchProducts(query).then((h) => (setHits(h), setHitsFor(query), setHi(0))), 80)
    return () => clearTimeout(t)
  }, [query])

  useEffect(() => {
    if (!custQuery) return setCustHits([])
    const t = setTimeout(() => void ordexa().searchCustomers(custQuery).then(setCustHits), 80)
    return () => clearTimeout(t)
  }, [custQuery])

  const subtotal = lines.reduce((s, l) => s + safe(() => lineTotal(toMilli(l.qty), linePrice(l)), 0), 0)
  const disc = safe(() => toPaisa(discount || '0'), 0)
  const total = subtotal - disc
  const paidP = safe(() => toPaisa(paid || '0'), 0)

  function reset() {
    setLines([])
    setCustomer(null)
    setCustQuery('')
    setDiscount('')
    setPaid('')
    setWarnings(null)
    setError(null)
    setSaved(null)
    setPicked(null)
    setQuery('')
    setTimeout(() => searchRef.current?.focus(), 0)
  }

  // Focus moves synchronously (flushSync) so fast typing / barcode scanners never lose keystrokes;
  // batch details load in the background and are patched in.
  function pick(p: ProductHit) {
    flushSync(() => {
      setPicked({ product: p, batches: [] })
      setQty('1')
      setBonus('')
    })
    qtyRef.current?.focus()
    qtyRef.current?.select()
    if (p.track_batches) {
      void ordexa()
        .batches(p.id)
        .then((batches) => {
          setPicked((cur) => (cur?.product.id === p.id ? { ...cur, batches } : cur))
          setLines((ls) => ls.map((l) => (l.product.id === p.id && !l.batches.length ? { ...l, batches } : l)))
        })
    }
  }

  async function pickFromSearch() {
    // Results may lag behind what was typed (debounce); search now if they are stale.
    const list = hitsFor === query ? hits : await ordexa().searchProducts(query)
    if (list[hi] ?? list[0]) pick(list[hitsFor === query ? hi : 0])
  }

  function addLine() {
    if (!picked || safe(() => toMilli(qty), 0) <= 0) return
    flushSync(() => {
      setLines((ls) => [...ls, { key: keySeq.current++, product: picked.product, batches: picked.batches, batchId: null, qty, bonus }])
      setPicked(null)
      setQuery('')
      setQty('1')
      setBonus('')
    })
    searchRef.current?.focus()
  }

  async function save(confirmed: boolean) {
    if (!lines.length || saving.current) return
    saving.current = true
    setBusy(true)
    setError(null)
    try {
      const res = await ordexa().createInvoice(
        {
          customerId: customer?.id ?? null,
          discount: discount || '0',
          paid: paid || '0',
          lines: lines.map((l) => ({ productId: l.product.id, batchId: l.batchId, qty: l.qty, bonus: l.bonus || '0' })),
        },
        confirmed,
      )
      if (res.ok) {
        setWarnings(null)
        setSaved(res.invoice)
      } else if (res.needsConfirmation) {
        setWarnings(res.warnings)
      } else {
        setError(res.error)
      }
    } finally {
      saving.current = false
      setBusy(false)
    }
  }

  useEffect(() => {
    const onKey = (e: globalThis.KeyboardEvent) => {
      if (saved) {
        if (e.key === 'Escape' || e.key === 'Enter') (e.preventDefault(), reset())
        return
      }
      if (warnings) {
        if (e.key === 'Enter') (e.preventDefault(), void save(true))
        if (e.key === 'Escape') (e.preventDefault(), setWarnings(null))
        return
      }
      if (e.key === 'F9') (e.preventDefault(), void save(false))
      else if (e.key === 'F4') (e.preventDefault(), custRef.current?.focus())
      else if (e.key === 'Escape') {
        e.preventDefault()
        if (picked) (setPicked(null), searchRef.current?.focus())
        else if (query) setQuery('')
        else if (lines.length && window.confirm('Clear this bill?')) reset()
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  })

  function onSearchKey(e: KeyboardEvent<HTMLInputElement>) {
    if (e.key === 'ArrowDown') (e.preventDefault(), setHi((i) => Math.min(i + 1, hits.length - 1)))
    else if (e.key === 'ArrowUp') (e.preventDefault(), setHi((i) => Math.max(i - 1, 0)))
    else if (e.key === 'Enter' && query.trim()) (e.preventDefault(), void pickFromSearch())
  }

  if (!canBill) return <div className="empty">Your account does not have permission to create bills. Ask the administrator.</div>
  if (saved) return <Receipt invoice={saved} state={state} onNext={reset} />

  return (
    <div className="billing">
      <section className="bill-main">
        <div className="bill-head">
          <div>
            <h1>New bill</h1>
            <div className="page-sub">
              {state.device_name ?? 'This PC'} · series {state.terminal_code ?? '—'}- · {new Date().toLocaleDateString('en-PK', { weekday: 'long', day: 'numeric', month: 'long' })}
            </div>
          </div>
          <div className="bill-keys">
            <span><kbd>F4</kbd> customer</span>
            <span><kbd>Enter</kbd> add item</span>
            <span><kbd>F9</kbd> save</span>
            <span><kbd>Esc</kbd> clear</span>
          </div>
        </div>
        <div className="card entry-card">
        <div className="customer-row">
          <label className="inline">
            Customer <kbd>F4</kbd>
          </label>
          {customer ? (
            <div className="chip">
              <b>{customer.code}</b> {customer.name}
              {customer.credit_limit !== null && (
                <span className="muted small">
                  {' '}
                  · owes {formatRs(customer.outstanding)} of {formatRs(customer.credit_limit)}
                </span>
              )}
              <button className="x" onClick={() => setCustomer(null)} aria-label="Remove customer">
                ×
              </button>
            </div>
          ) : (
            <div className="combo">
              <input
                ref={custRef}
                value={custQuery}
                onChange={(e) => setCustQuery(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === 'Enter' && custHits[0]) {
                    e.preventDefault()
                    setCustomer(custHits[0])
                    setCustQuery('')
                    searchRef.current?.focus()
                  }
                }}
                placeholder="Walk-in · type shop code (e.g. 101) or name"
              />
              {custHits.length > 0 && (
                <ul className="dropdown">
                  {custHits.map((c) => (
                    <li key={c.id} onMouseDown={() => (setCustomer(c), setCustQuery(''))}>
                      <b>{c.code}</b> {c.name}
                    </li>
                  ))}
                </ul>
              )}
            </div>
          )}
        </div>

        <div className="entry-row">
          <div className="combo grow">
            <input
              ref={searchRef}
              autoFocus
              className="search"
              value={picked ? picked.product.name : query}
              readOnly={Boolean(picked)}
              onChange={(e) => setQuery(e.target.value)}
              onKeyDown={onSearchKey}
              placeholder="Search medicine by name, generic, code or scan barcode"
            />
            {!picked && query && (
              <ul className="dropdown results">
                {hits.length === 0 && <li className="muted">No product found</li>}
                {hits.map((h, i) => (
                  <li key={h.id} className={i === hi ? 'hi' : ''} onMouseEnter={() => setHi(i)} onMouseDown={() => pick(h)}>
                    <span className="name">{h.name}</span>
                    <span className="muted small">
                      {h.generic_name ?? ''} {h.pack_size ? `· ${h.pack_size}` : ''}
                    </span>
                    <span className="price">{formatRs(h.sale_price)}</span>
                    <span className={`stock ${h.available <= 0 ? 'out' : ''}`}>{h.available / 1000} in stock</span>
                  </li>
                ))}
              </ul>
            )}
          </div>
          <input
            ref={qtyRef}
            className="qty"
            value={qty}
            disabled={!picked}
            onChange={(e) => setQty(e.target.value)}
            onKeyDown={(e) => e.key === 'Enter' && (e.preventDefault(), addLine())}
            aria-label="Quantity"
          />
          <input
            className="qty"
            value={bonus}
            disabled={!picked}
            onChange={(e) => setBonus(e.target.value)}
            onKeyDown={(e) => e.key === 'Enter' && (e.preventDefault(), addLine())}
            placeholder="Bonus"
            aria-label="Bonus quantity"
          />
          <button className="btn" disabled={!picked} onClick={addLine}>
            Add
          </button>
        </div>
        </div>
        {picked && picked.batches.length > 0 && (
          <div className="hint">
            Batch: earliest expiry first — <b>{picked.batches[0].batch_no}</b> (exp {picked.batches[0].expiry_date},{' '}
            {picked.batches[0].available / 1000} left)
            {picked.batches[0].near_expiry && <span className="tag warn">near expiry</span>}
          </div>
        )}

        <table className="lines">
          <thead>
            <tr>
              <th>#</th>
              <th>Item</th>
              <th>Batch</th>
              <th className="num">Qty</th>
              <th className="num">Bonus</th>
              <th className="num">Rate</th>
              <th className="num">Amount</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {lines.length === 0 && (
              <tr>
                <td colSpan={8} className="empty-row">
                  <div className="empty-bill">
                    <div className="big-icon">💊</div>
                    <b>No items yet</b>
                    <div className="small">Type a medicine name, code or scan a barcode, then press Enter.</div>
                  </div>
                </td>
              </tr>
            )}
            {lines.map((l, i) => (
              <tr key={l.key}>
                <td>{i + 1}</td>
                <td>
                  {l.product.name}
                  <div className="muted small">{l.product.pack_size}</div>
                </td>
                <td>
                  {l.batches.length > 0 ? (
                    <select
                      value={l.batchId ?? ''}
                      onChange={(e) => setLines((ls) => ls.map((x) => (x.key === l.key ? { ...x, batchId: e.target.value || null } : x)))}
                    >
                      <option value="">Auto (earliest expiry)</option>
                      {l.batches.map((b) => (
                        <option key={b.id} value={b.id}>
                          {b.batch_no} · exp {b.expiry_date} · {b.available / 1000}
                        </option>
                      ))}
                    </select>
                  ) : (
                    <span className="muted">—</span>
                  )}
                </td>
                <td className="num">{l.qty}</td>
                <td className="num">{l.bonus || ''}</td>
                <td className="num">{formatRs(linePrice(l))}</td>
                <td className="num">{formatRs(safe(() => lineTotal(toMilli(l.qty), linePrice(l)), 0))}</td>
                <td>
                  <button className="x" onClick={() => setLines((ls) => ls.filter((x) => x.key !== l.key))} aria-label="Remove line">
                    ×
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </section>

      <aside className="bill-side card">
        <div className="total-hero">
          <span className="th-label">{customer ? customer.name : 'Walk-in customer'}</span>
          <span className="th-total">{formatRs(total)}</span>
          <span className="th-sub">{lines.length} item{lines.length === 1 ? '' : 's'}</span>
        </div>
        <div className="sum-row">
          <span>Subtotal</span>
          <span>{formatRs(subtotal)}</span>
        </div>
        <label className="sum-row">
          <span>Discount</span>
          <input className="money" value={discount} onChange={(e) => setDiscount(e.target.value)} placeholder="0" />
        </label>
        <div className="sum-row total">
          <span>Total</span>
          <span>{formatRs(total)}</span>
        </div>
        <label className="sum-row">
          <span>Cash received</span>
          <input className="money" value={paid} onChange={(e) => setPaid(e.target.value)} placeholder="0" />
        </label>
        <div className="sum-row">
          <span>{paidP >= total ? 'Change' : customer ? 'On account' : 'Due'}</span>
          <span>{formatRs(Math.abs(paidP - total))}</span>
        </div>
        {error && <div className="alert error">{error}</div>}
        <button className="btn primary block big" disabled={!lines.length || busy} onClick={() => void save(false)}>
          Save bill <kbd>F9</kbd>
        </button>
        <p className="muted small center">Saved on this PC instantly; sent to the server automatically.</p>
      </aside>

      {warnings && (
        <div className="modal-backdrop">
          <div className="modal card" role="dialog" aria-modal="true">
            <h2>Please confirm</h2>
            <ul className="warnings">
              {warnings.map((w, i) => (
                <li key={i} className={w.code}>
                  {w.message}
                </li>
              ))}
            </ul>
            <div className="modal-actions">
              <button className="btn" onClick={() => setWarnings(null)}>
                Go back <kbd>Esc</kbd>
              </button>
              <button className="btn primary" autoFocus onClick={() => void save(true)}>
                Save anyway <kbd>Enter</kbd>
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
