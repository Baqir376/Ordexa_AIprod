import { useState } from 'react'

import type { AppState, BatchInfo, CustomerHit, ProductHit } from '../../../core/types'
import { ordexa } from '../bridge'
import { ErrorBox, Field, Modal, Page, Table, can, rs, today, uid, useAction, useApi } from '../ui'

interface Payment { id: string; number: string; direction: string; party_name: string; amount: string; method: string; payment_date: string; cheque_no: string | null; cheque_bank: string | null; cheque_date: string | null; cheque_status: string | null; note: string | null }

export function Cheques({ state }: { state: AppState }) {
  const list = useApi<Payment[]>('/api/v1/payments?cheques_only=true')
  const { error, run } = useAction()
  const canAct = can(state.user?.permissions, 'payment.record')
  async function setStatus(p: Payment, status: 'cleared' | 'bounced') {
    if (status === 'bounced' && !window.confirm(`Mark cheque #${p.cheque_no} from ${p.party_name} as bounced? The amount will be owed again.`)) return
    await run(() => ordexa().request('POST', `/api/v1/payments/${p.id}/cheque-status`, { status }))
    void list.reload()
  }
  return (
    <Page title="Cheques">
      <ErrorBox error={list.error ?? error} />
      <Table
        rows={list.data}
        empty="No cheques recorded."
        columns={[
          { key: 'number', label: 'Voucher' },
          { key: 'direction', label: 'Type', render: (p) => (p.direction === 'in' ? 'Received' : 'Issued') },
          { key: 'party_name', label: 'Party' },
          { key: 'cheque_no', label: 'Cheque', render: (p) => `#${p.cheque_no} ${p.cheque_bank ?? ''}` },
          { key: 'cheque_date', label: 'Cheque date', render: (p) => p.cheque_date ?? '' },
          { key: 'amount', label: 'Amount', num: true, render: (p) => rs(p.amount) },
          { key: 'cheque_status', label: 'Status', render: (p) => <span className={`tag ${p.cheque_status === 'cleared' ? 'synced' : p.cheque_status === 'bounced' ? 'rejected' : 'pending'}`}>{p.cheque_status}</span> },
          { key: 'a', label: '', render: (p) => canAct && p.cheque_status === 'pending' && (
            <span className="row-actions">
              <button className="btn small" onClick={() => void setStatus(p, 'cleared')}>Cleared</button>
              <button className="btn small danger" onClick={() => void setStatus(p, 'bounced')}>Bounced</button>
            </span>
          ) },
        ]}
      />
    </Page>
  )
}

interface ExpenseRow { id: string; category: string; amount: string; expense_date: string; method: string; note: string | null }
const CATEGORIES = ['Salaries', 'Rent', 'Electricity', 'Fuel / delivery', 'Tea / entertainment', 'Repairs', 'Other']

export function Expenses({ state }: { state: AppState }) {
  const list = useApi<ExpenseRow[]>('/api/v1/expenses')
  const [f, setF] = useState({ category: CATEGORIES[0], amount: '', expense_date: today(), method: 'cash', note: '' })
  const { busy, error, run } = useAction()
  async function save() {
    const r = await run(() => ordexa().request('POST', '/api/v1/expenses', { id: uid(), ...f, note: f.note || null }))
    if (r) { setF({ ...f, amount: '', note: '' }); void list.reload() }
  }
  return (
    <Page title="Expenses">
      {can(state.user?.permissions, 'expense.record') && (
        <div className="card pad form inline-form">
          <Field label="Category"><select value={f.category} onChange={(e) => setF({ ...f, category: e.target.value })}>{CATEGORIES.map((c) => <option key={c}>{c}</option>)}</select></Field>
          <Field label="Amount (Rs)"><input value={f.amount} onChange={(e) => setF({ ...f, amount: e.target.value })} /></Field>
          <Field label="Date"><input type="date" value={f.expense_date} onChange={(e) => setF({ ...f, expense_date: e.target.value })} /></Field>
          <Field label="Note"><input value={f.note} onChange={(e) => setF({ ...f, note: e.target.value })} /></Field>
          <button className="btn primary" disabled={busy || !f.amount} onClick={() => void save()}>Add expense</button>
        </div>
      )}
      <ErrorBox error={list.error ?? error} />
      <Table
        rows={list.data}
        empty="No expenses recorded."
        columns={[
          { key: 'expense_date', label: 'Date' },
          { key: 'category', label: 'Category' },
          { key: 'note', label: 'Note', render: (e) => e.note ?? '' },
          { key: 'amount', label: 'Amount', num: true, render: (e) => rs(e.amount) },
        ]}
      />
    </Page>
  )
}

interface CN { id: string; number: string; note_date: string; reason: string; total: string; lines: number }
interface RLine { key: string; product: ProductHit; batches: BatchInfo[]; batch_id: string; quantity: string; unit_price: string; restock: boolean }

export function Returns({ state }: { state: AppState }) {
  const list = useApi<CN[]>('/api/v1/credit-notes')
  const [creating, setCreating] = useState(false)
  return (
    <Page title="Sales returns (credit notes)" actions={can(state.user?.permissions, 'sales_order.cancel') && <button className="btn primary" onClick={() => setCreating(true)}>+ New return</button>}>
      <ErrorBox error={list.error} />
      <Table
        rows={list.data}
        empty="No returns yet."
        columns={[
          { key: 'number', label: 'No.' },
          { key: 'note_date', label: 'Date' },
          { key: 'reason', label: 'Reason' },
          { key: 'lines', label: 'Items', num: true },
          { key: 'total', label: 'Credit', num: true, render: (c) => rs(c.total) },
        ]}
      />
      {creating && <ReturnForm onClose={() => setCreating(false)} onSaved={() => { setCreating(false); void list.reload() }} />}
    </Page>
  )
}

function ReturnForm({ onClose, onSaved }: { onClose: () => void; onSaved: () => void }) {
  const [cq, setCq] = useState('')
  const [chits, setChits] = useState<CustomerHit[]>([])
  const [customer, setCustomer] = useState<CustomerHit | null>(null)
  const [q, setQ] = useState('')
  const [hits, setHits] = useState<ProductHit[]>([])
  const [lines, setLines] = useState<RLine[]>([])
  const [reason, setReason] = useState('')
  const { busy, error, run } = useAction()
  async function addProduct(p: ProductHit) {
    const batches = p.track_batches ? await ordexa().batches(p.id) : []
    setLines((ls) => [...ls, { key: uid(), product: p, batches, batch_id: batches[0]?.id ?? '', quantity: '1', unit_price: String(p.sale_price / 100), restock: true }])
    setQ('')
    setHits([])
  }
  const upd = (key: string, patch: Partial<RLine>) => setLines((ls) => ls.map((l) => (l.key === key ? { ...l, ...patch } : l)))
  async function save() {
    const r = await run(() => ordexa().request('POST', '/api/v1/credit-notes', {
      id: uid(), customer_id: customer!.id, reason,
      lines: lines.map((l) => ({ product_id: l.product.id, batch_id: l.batch_id || null, quantity: l.quantity, unit_price: l.unit_price, restock: l.restock })),
    }))
    if (r) onSaved()
  }
  return (
    <Modal title="New sales return" onClose={onClose} wide>
      <div className="form">
        {customer ? (
          <div className="chip"><b>{customer.code}</b> {customer.name}<button className="x" onClick={() => setCustomer(null)}>×</button></div>
        ) : (
          <div className="combo">
            <input autoFocus value={cq} placeholder="Customer: code or name" onChange={async (e) => { setCq(e.target.value); setChits(e.target.value ? await ordexa().searchCustomers(e.target.value) : []) }} />
            {chits.length > 0 && <ul className="dropdown">{chits.map((c) => <li key={c.id} onMouseDown={() => { setCustomer(c); setChits([]) }}><b>{c.code}</b> {c.name}</li>)}</ul>}
          </div>
        )}
        <div className="combo">
          <input value={q} placeholder="Returned product" onChange={async (e) => { setQ(e.target.value); setHits(e.target.value ? await ordexa().searchProducts(e.target.value) : []) }} />
          {hits.length > 0 && <ul className="dropdown">{hits.map((h) => <li key={h.id} onMouseDown={() => void addProduct(h)}><b>{h.code}</b> {h.name}</li>)}</ul>}
        </div>
        <table className="lines compact">
          <thead><tr><th>Product</th><th>Batch</th><th className="num">Qty</th><th className="num">Rate</th><th>Back to stock?</th><th /></tr></thead>
          <tbody>
            {lines.length === 0 && <tr><td colSpan={6} className="empty-row">Add the returned items.</td></tr>}
            {lines.map((l) => (
              <tr key={l.key}>
                <td>{l.product.name}</td>
                <td>{l.batches.length ? <select value={l.batch_id} onChange={(e) => upd(l.key, { batch_id: e.target.value })}>{l.batches.map((b) => <option key={b.id} value={b.id}>{b.batch_no} · exp {b.expiry_date}</option>)}</select> : '—'}</td>
                <td><input className="num-in" value={l.quantity} onChange={(e) => upd(l.key, { quantity: e.target.value })} /></td>
                <td><input className="num-in" value={l.unit_price} onChange={(e) => upd(l.key, { unit_price: e.target.value })} /></td>
                <td><label className="check"><input type="checkbox" checked={l.restock} onChange={(e) => upd(l.key, { restock: e.target.checked })} /> sellable</label></td>
                <td><button className="x" onClick={() => setLines((ls) => ls.filter((x) => x.key !== l.key))}>×</button></td>
              </tr>
            ))}
          </tbody>
        </table>
        <Field label="Reason"><input value={reason} onChange={(e) => setReason(e.target.value)} placeholder="damaged, near expiry, wrong item…" /></Field>
        <ErrorBox error={error} />
        <div className="modal-actions">
          <button className="btn" onClick={onClose}>Cancel</button>
          <button className="btn primary" disabled={busy || !customer || !lines.length || reason.length < 3} onClick={() => void save()}>Save credit note</button>
        </div>
      </div>
    </Modal>
  )
}
