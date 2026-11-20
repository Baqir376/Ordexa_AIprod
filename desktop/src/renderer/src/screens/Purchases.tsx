import { useState } from 'react'

import { formatRs, lineTotal, toMilli, toPaisa } from '../../../core/money'
import type { AppState, ProductHit } from '../../../core/types'
import { ordexa } from '../bridge'
import { ErrorBox, Field, Modal, Page, Table, can, rs, today, uid, useAction, useApi } from '../ui'
import { LedgerModal } from './Customers'

interface Supplier { id: string; code: string; name: string; phone: string | null; balance: string | null }
interface Purchase { id: string; number: string; supplier_name: string; supplier_bill_no: string | null; bill_date: string; total: string; paid_amount: string; lines: unknown[] }
interface PLine { key: string; product: ProductHit; batch_no: string; expiry_date: string; quantity: string; bonus_quantity: string; unit_cost: string; sale_price: string }

const safe = (f: () => number) => { try { return f() } catch { return 0 } }

export function Purchases({ state }: { state: AppState }) {
  const perms = state.user?.permissions
  const list = useApi<Purchase[]>('/api/v1/purchases')
  const [creating, setCreating] = useState(false)
  return (
    <Page title="Purchases (goods received)" actions={can(perms, 'goods_receipt.create') && <button className="btn primary" onClick={() => setCreating(true)}>+ New purchase</button>}>
      <ErrorBox error={list.error} />
      <Table
        rows={list.data}
        empty="No purchases yet."
        columns={[
          { key: 'number', label: 'No.' },
          { key: 'bill_date', label: 'Date' },
          { key: 'supplier_name', label: 'Supplier' },
          { key: 'supplier_bill_no', label: 'Supplier bill', render: (p) => p.supplier_bill_no ?? '' },
          { key: 'lines', label: 'Items', num: true, render: (p) => p.lines.length },
          { key: 'total', label: 'Total', num: true, render: (p) => rs(p.total) },
          { key: 'paid_amount', label: 'Paid', num: true, render: (p) => rs(p.paid_amount) },
        ]}
      />
      {creating && <PurchaseForm onClose={() => setCreating(false)} onSaved={() => { setCreating(false); void list.reload() }} />}
    </Page>
  )
}

function PurchaseForm({ onClose, onSaved }: { onClose: () => void; onSaved: () => void }) {
  const suppliers = useApi<Supplier[]>('/api/v1/suppliers')
  const [head, setHead] = useState({ supplier_id: '', supplier_bill_no: '', bill_date: today(), discount: '', paid_amount: '', payment_method: 'cash' })
  const [lines, setLines] = useState<PLine[]>([])
  const [q, setQ] = useState('')
  const [hits, setHits] = useState<ProductHit[]>([])
  const { busy, error, run } = useAction()

  async function search(v: string) {
    setQ(v)
    setHits(v ? await ordexa().searchProducts(v) : [])
  }
  function add(p: ProductHit) {
    setLines((ls) => [...ls, { key: uid(), product: p, batch_no: '', expiry_date: '', quantity: '', bonus_quantity: '', unit_cost: '', sale_price: '' }])
    setQ('')
    setHits([])
  }
  const upd = (key: string, k: keyof PLine) => (e: { target: { value: string } }) => setLines((ls) => ls.map((l) => (l.key === key ? { ...l, [k]: e.target.value } : l)))
  const subtotal = lines.reduce((s, l) => s + safe(() => lineTotal(toMilli(l.quantity), toPaisa(l.unit_cost))), 0)
  const total = subtotal - safe(() => toPaisa(head.discount || '0'))

  async function save() {
    const r = await run(() => ordexa().request('POST', '/api/v1/purchases', {
      id: uid(), supplier_id: head.supplier_id, supplier_bill_no: head.supplier_bill_no || null, bill_date: head.bill_date,
      discount: head.discount || '0', paid_amount: head.paid_amount || '0', payment_method: head.payment_method,
      lines: lines.map((l) => ({
        product_id: l.product.id, quantity: l.quantity, bonus_quantity: l.bonus_quantity || '0', unit_cost: l.unit_cost,
        batch_no: l.product.track_batches ? l.batch_no : null, expiry_date: l.product.track_batches ? l.expiry_date : null,
        sale_price: l.sale_price || null,
      })),
    }))
    if (r) onSaved()
  }

  return (
    <Modal title="New purchase" onClose={onClose} wide>
      <div className="form">
        <div className="row-3">
          <Field label="Supplier">
            <select value={head.supplier_id} onChange={(e) => setHead({ ...head, supplier_id: e.target.value })} autoFocus>
              <option value="">Choose supplier…</option>
              {(suppliers.data ?? []).map((s) => <option key={s.id} value={s.id}>{s.name}</option>)}
            </select>
          </Field>
          <Field label="Supplier bill no."><input value={head.supplier_bill_no} onChange={(e) => setHead({ ...head, supplier_bill_no: e.target.value })} /></Field>
          <Field label="Bill date"><input type="date" value={head.bill_date} onChange={(e) => setHead({ ...head, bill_date: e.target.value })} /></Field>
        </div>
        <div className="combo">
          <input value={q} onChange={(e) => void search(e.target.value)} placeholder="Add product: type name or code" />
          {hits.length > 0 && (
            <ul className="dropdown">
              {hits.map((h) => <li key={h.id} onMouseDown={() => add(h)}><b>{h.code}</b> {h.name}</li>)}
            </ul>
          )}
        </div>
        <table className="lines compact">
          <thead><tr><th>Product</th><th>Batch</th><th>Expiry</th><th className="num">Qty</th><th className="num">Bonus</th><th className="num">Cost</th><th className="num">New sale price</th><th className="num">Amount</th><th /></tr></thead>
          <tbody>
            {lines.length === 0 && <tr><td colSpan={9} className="empty-row">Add the products on the supplier's bill.</td></tr>}
            {lines.map((l) => (
              <tr key={l.key}>
                <td>{l.product.name}</td>
                <td>{l.product.track_batches ? <input value={l.batch_no} onChange={upd(l.key, 'batch_no')} /> : '—'}</td>
                <td>{l.product.track_batches ? <input type="date" value={l.expiry_date} onChange={upd(l.key, 'expiry_date')} /> : '—'}</td>
                <td><input className="num-in" value={l.quantity} onChange={upd(l.key, 'quantity')} /></td>
                <td><input className="num-in" value={l.bonus_quantity} onChange={upd(l.key, 'bonus_quantity')} /></td>
                <td><input className="num-in" value={l.unit_cost} onChange={upd(l.key, 'unit_cost')} /></td>
                <td><input className="num-in" value={l.sale_price} onChange={upd(l.key, 'sale_price')} placeholder={l.product.sale_price / 100 + ''} /></td>
                <td className="num">{formatRs(safe(() => lineTotal(toMilli(l.quantity), toPaisa(l.unit_cost))))}</td>
                <td><button className="x" onClick={() => setLines((ls) => ls.filter((x) => x.key !== l.key))}>×</button></td>
              </tr>
            ))}
          </tbody>
        </table>
        <div className="row-3">
          <Field label="Discount (Rs)"><input value={head.discount} onChange={(e) => setHead({ ...head, discount: e.target.value })} /></Field>
          <Field label="Paid now (Rs)"><input value={head.paid_amount} onChange={(e) => setHead({ ...head, paid_amount: e.target.value })} /></Field>
          <Field label="Paid by">
            <select value={head.payment_method} onChange={(e) => setHead({ ...head, payment_method: e.target.value })}>
              <option value="cash">Cash</option><option value="bank">Bank</option><option value="cheque">Cheque</option>
            </select>
          </Field>
        </div>
        <div className="sum-row total"><span>Total</span><span>{formatRs(total)}</span></div>
        <ErrorBox error={error} />
        <div className="modal-actions">
          <button className="btn" onClick={onClose}>Cancel</button>
          <button className="btn primary" disabled={busy || !head.supplier_id || !lines.length} onClick={() => void save()}>Post purchase (adds stock)</button>
        </div>
      </div>
    </Modal>
  )
}

export function Suppliers({ state }: { state: AppState }) {
  const perms = state.user?.permissions
  const list = useApi<Supplier[]>('/api/v1/suppliers')
  const [adding, setAdding] = useState(false)
  const [ledger, setLedger] = useState<Supplier | null>(null)
  const [paying, setPaying] = useState<Supplier | null>(null)
  return (
    <Page title="Suppliers" actions={can(perms, 'supplier.manage') && <button className="btn primary" onClick={() => setAdding(true)}>+ New supplier</button>}>
      <ErrorBox error={list.error} />
      <Table
        rows={list.data}
        columns={[
          { key: 'code', label: 'Code' },
          { key: 'name', label: 'Name' },
          { key: 'phone', label: 'Phone', render: (s) => s.phone ?? '' },
          { key: 'balance', label: 'We owe', num: true, render: (s) => rs(s.balance) },
          { key: 'a', label: '', render: (s) => (
            <span className="row-actions">
              {can(perms, 'finance.view') && <button className="btn small" onClick={() => setLedger(s)}>Ledger</button>}
              {can(perms, 'payment.record') && <button className="btn small" onClick={() => setPaying(s)}>Pay</button>}
            </span>
          ) },
        ]}
      />
      {adding && <SupplierForm onClose={() => setAdding(false)} onSaved={() => { setAdding(false); void list.reload() }} />}
      {ledger && <LedgerModal party="supplier" id={ledger.id} name={ledger.name} onClose={() => setLedger(null)} />}
      {paying && <PaySupplier supplier={paying} onClose={() => setPaying(null)} onDone={() => { setPaying(null); void list.reload() }} />}
    </Page>
  )
}

function SupplierForm({ onClose, onSaved }: { onClose: () => void; onSaved: () => void }) {
  const [f, setF] = useState({ code: '', name: '', phone: '', address: '', ntn: '' })
  const { busy, error, run } = useAction()
  const set = (k: keyof typeof f) => (e: { target: { value: string } }) => setF({ ...f, [k]: e.target.value })
  return (
    <Modal title="New supplier" onClose={onClose}>
      <div className="form">
        <div className="row-2">
          <Field label="Code"><input value={f.code} onChange={set('code')} autoFocus /></Field>
          <Field label="NTN"><input value={f.ntn} onChange={set('ntn')} /></Field>
        </div>
        <Field label="Name"><input value={f.name} onChange={set('name')} /></Field>
        <div className="row-2">
          <Field label="Phone"><input value={f.phone} onChange={set('phone')} /></Field>
          <Field label="Address"><input value={f.address} onChange={set('address')} /></Field>
        </div>
        <ErrorBox error={error} />
        <div className="modal-actions">
          <button className="btn" onClick={onClose}>Cancel</button>
          <button className="btn primary" disabled={busy || !f.code || !f.name} onClick={() => void run(() => ordexa().request('POST', '/api/v1/suppliers', { ...f, phone: f.phone || null, address: f.address || null, ntn: f.ntn || null })).then((r) => r && onSaved())}>Save</button>
        </div>
      </div>
    </Modal>
  )
}

function PaySupplier({ supplier, onClose, onDone }: { supplier: Supplier; onClose: () => void; onDone: () => void }) {
  const [f, setF] = useState({ amount: '', method: 'cash', cheque_no: '', cheque_bank: '', note: '' })
  const { busy, error, run } = useAction()
  const set = (k: keyof typeof f) => (e: { target: { value: string } }) => setF({ ...f, [k]: e.target.value })
  async function save() {
    const r = await run(() => ordexa().request('POST', '/api/v1/payments', {
      id: uid(), direction: 'out', party_id: supplier.id, amount: f.amount, method: f.method, note: f.note || null,
      ...(f.method === 'cheque' ? { cheque_no: f.cheque_no, cheque_bank: f.cheque_bank } : {}),
    }))
    if (r) onDone()
  }
  return (
    <Modal title={`Pay ${supplier.name}`} onClose={onClose}>
      <div className="form">
        <p className="muted">Current balance owed: {rs(supplier.balance)}</p>
        <div className="row-2">
          <Field label="Amount (Rs)"><input value={f.amount} onChange={set('amount')} autoFocus /></Field>
          <Field label="Method"><select value={f.method} onChange={set('method')}><option value="cash">Cash</option><option value="bank">Bank</option><option value="cheque">Cheque</option></select></Field>
        </div>
        {f.method === 'cheque' && (
          <div className="row-2">
            <Field label="Cheque no."><input value={f.cheque_no} onChange={set('cheque_no')} /></Field>
            <Field label="Bank"><input value={f.cheque_bank} onChange={set('cheque_bank')} /></Field>
          </div>
        )}
        <Field label="Note"><input value={f.note} onChange={set('note')} /></Field>
        <ErrorBox error={error} />
        <div className="modal-actions">
          <button className="btn" onClick={onClose}>Cancel</button>
          <button className="btn primary" disabled={busy || !f.amount} onClick={() => void save()}>Save payment</button>
        </div>
      </div>
    </Modal>
  )
}

