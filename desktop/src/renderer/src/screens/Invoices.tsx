// Bills & receipts: bills made on this PC (work offline), every bill of the business from the server
// (all PCs and WhatsApp), and cash received at the counter.

import { useEffect, useState } from 'react'

import { formatRs } from '../../../core/money'
import type { AppState, InvoiceView, ReceiptView } from '../../../core/types'
import { ordexa } from '../bridge'
import { ErrorBox, Modal, can, rs, useApi } from '../ui'
import { Receipt } from './Receipt'

const STATUS: Record<InvoiceView['sync_status'], string> = {
  pending: 'Waiting to sync',
  synced: 'Synced',
  rejected: 'Rejected by server',
  conflict: 'Conflict',
}

interface ServerBill {
  id: string
  number: string
  customer_name: string
  total: string
  paid_amount: string
  issued_at: string
  source: string
  source_label: string
  line_count: number
  updated: boolean
  flags: { code: string }[]
}
interface ServerBillDetail extends ServerBill {
  subtotal: string
  discount: string
  lines: { line_no: number; product_name: string; batch_no: string | null; quantity: string; bonus_quantity: string; unit: string; unit_price: string; line_total: string }[]
}

const isToday = (iso: string) => new Date(iso).toDateString() === new Date().toDateString()
const when = (iso: string) =>
  isToday(iso)
    ? new Date(iso).toLocaleTimeString('en-PK', { hour: 'numeric', minute: '2-digit' })
    : new Date(iso).toLocaleString('en-PK', { day: 'numeric', month: 'short', hour: 'numeric', minute: '2-digit' })
const qty = (v: string) => String(Number(v))

export function Invoices({ state }: { state: AppState }) {
  const [rows, setRows] = useState<InvoiceView[]>([])
  const [receipts, setReceipts] = useState<ReceiptView[]>([])
  const online = state.user?.online !== false && can(state.user?.permissions, 'invoice.view')
  const [view, setView] = useState<'pc' | 'all' | 'receipts'>('pc')
  const [q, setQ] = useState('')
  const [open, setOpen] = useState<InvoiceView | null>(null)
  const [serverOpen, setServerOpen] = useState<string | null>(null)
  const all = useApi<ServerBill[]>(view === 'all' ? `/api/v1/invoices?limit=300${q ? `&q=${encodeURIComponent(q)}` : ''}` : null, [view, q])

  useEffect(() => {
    void ordexa().listInvoices().then(setRows)
    void ordexa().listReceipts().then(setReceipts)
  }, [state.sync.pending, state.sync.last_sync_at])

  if (open) return <Receipt invoice={open} state={state} onNext={() => setOpen(null)} nextLabel="Back to bills" />

  const needle = q.trim().toLowerCase()
  const match = (...v: string[]) => !needle || v.some((x) => x.toLowerCase().includes(needle))
  const today = rows.filter((r) => isToday(r.issued_at))
  const todayTotal = today.reduce((s, r) => s + r.total, 0)
  const unsynced = rows.filter((r) => r.sync_status !== 'synced').length
  const cashToday = receipts.filter((r) => isToday(r.received_at)).reduce((s, r) => s + r.amount, 0)
  const pcRows = rows.filter((r) => match(r.number, r.customer_name))
  const rcRows = receipts.filter((r) => match(r.number, r.customer_name))

  return (
    <div className="page invoices">
      <div className="page-head">
        <div>
          <h1>Bills &amp; receipts</h1>
          <div className="page-sub">Reprint any bill, check what reached the server, and see every bill of the business.</div>
        </div>
      </div>

      <div className="stats">
        <div className="stat card"><div className="stat-label">Bills today (this PC)</div><div className="stat-value">{today.length}</div></div>
        <div className="stat card ok"><div className="stat-label">Sales today (this PC)</div><div className="stat-value">{formatRs(todayTotal)}</div></div>
        <div className="stat card"><div className="stat-label">Cash received today</div><div className="stat-value">{formatRs(cashToday)}</div></div>
        <div className={`stat card ${unsynced ? 'warn' : 'ok'}`}>
          <div className="stat-label">Waiting to sync</div>
          <div className="stat-value">{unsynced}</div>
        </div>
      </div>

      <div className="toolbar">
        <div className="subtabs">
          <button className={view === 'pc' ? 'active' : ''} onClick={() => setView('pc')}>This PC <span className="count">{rows.length}</span></button>
          {online && <button className={view === 'all' ? 'active' : ''} onClick={() => setView('all')}>All bills (server)</button>}
          <button className={view === 'receipts' ? 'active' : ''} onClick={() => setView('receipts')}>Cash receipts <span className="count">{receipts.length}</span></button>
        </div>
        <input className="search-small" placeholder="Search bill no. or customer" value={q} onChange={(e) => setQ(e.target.value)} />
      </div>

      {view === 'pc' && (
        <table className="lines">
          <thead>
            <tr><th>Bill</th><th>Time</th><th>Customer</th><th className="num">Items</th><th className="num">Total</th><th>Status</th><th>Notes</th></tr>
          </thead>
          <tbody>
            {pcRows.length === 0 && <tr><td colSpan={7} className="empty-row">{needle ? 'No bill matches your search.' : 'No bills on this PC yet. Press F2 to make one.'}</td></tr>}
            {pcRows.map((r) => (
              <tr key={r.id} className="clickable" onClick={() => setOpen(r)} title="Open to reprint">
                <td><b className="bill-no">{r.number}</b></td>
                <td>{when(r.issued_at)}</td>
                <td>{r.customer_name}</td>
                <td className="num">{r.lines.length}</td>
                <td className="num"><b>{formatRs(r.total)}</b></td>
                <td><span className={`tag ${r.sync_status}`}>{STATUS[r.sync_status]}</span></td>
                <td className="small muted">{r.sync_error ?? r.server_flags.map((f) => f.code.replace(/_/g, ' ')).join(', ')}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      {view === 'all' && (
        <>
          <ErrorBox error={all.error} />
          <table className="lines">
            <thead>
              <tr><th>Bill</th><th>Time</th><th>Customer</th><th>Made at</th><th className="num">Items</th><th className="num">Total</th><th className="num">Paid</th><th /></tr>
            </thead>
            <tbody>
              {all.data === null && <tr><td colSpan={8} className="empty-row">Loading…</td></tr>}
              {all.data?.length === 0 && <tr><td colSpan={8} className="empty-row">No bills found.</td></tr>}
              {all.data?.map((r) => (
                <tr key={r.id} className="clickable" onClick={() => setServerOpen(r.id)}>
                  <td><b className="bill-no">{r.number}</b>{r.updated && <span className="tag synced" style={{ marginLeft: 6 }}>updated</span>}</td>
                  <td>{when(r.issued_at)}</td>
                  <td>{r.customer_name}</td>
                  <td><span className={`src src-${r.source}`}>{r.source_label}</span></td>
                  <td className="num">{r.line_count}</td>
                  <td className="num"><b>{rs(r.total)}</b></td>
                  <td className="num">{rs(r.paid_amount)}</td>
                  <td className="small muted">{r.flags.filter((f) => f.code !== 'items_added').map((f) => f.code.replace(/_/g, ' ')).join(', ')}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </>
      )}

      {view === 'receipts' && (
        <table className="lines">
          <thead>
            <tr><th>Receipt</th><th>Time</th><th>Customer</th><th className="num">Amount</th><th>Status</th></tr>
          </thead>
          <tbody>
            {rcRows.length === 0 && <tr><td colSpan={5} className="empty-row">No cash receipts on this PC. Use Customers → Receive.</td></tr>}
            {rcRows.map((r) => (
              <tr key={r.id}>
                <td><b className="bill-no">{r.number}</b></td>
                <td>{when(r.received_at)}</td>
                <td>{r.customer_name}</td>
                <td className="num"><b>{formatRs(r.amount)}</b></td>
                <td><span className={`tag ${r.sync_status}`}>{STATUS[r.sync_status]}</span>{r.sync_error && <span className="small"> {r.sync_error}</span>}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      {serverOpen && <ServerBillModal id={serverOpen} onClose={() => setServerOpen(null)} />}
    </div>
  )
}

function ServerBillModal({ id, onClose }: { id: string; onClose: () => void }) {
  const bill = useApi<ServerBillDetail>(`/api/v1/invoices/${id}`, [id])
  const b = bill.data
  return (
    <Modal title={b ? `Bill ${b.number}` : 'Bill'} onClose={onClose} wide>
      <ErrorBox error={bill.error} />
      {b && (
        <div className="form">
          <div className="bill-summary">
            <div><span className="muted small">Customer</span><b>{b.customer_name}</b></div>
            <div><span className="muted small">Date</span><b>{new Date(b.issued_at).toLocaleString('en-PK')}</b></div>
            <div><span className="muted small">Made at</span><b>{b.source_label}</b></div>
            <div><span className="muted small">Total</span><b className="big-money">{rs(b.total)}</b></div>
          </div>
          {b.updated && <div className="alert ok">Items were added to this bill later the same day; the shop got the updated bill.</div>}
          <table className="lines">
            <thead>
              <tr><th>#</th><th>Item</th><th>Batch</th><th className="num">Qty</th><th className="num">Bonus</th><th className="num">Rate</th><th className="num">Amount</th></tr>
            </thead>
            <tbody>
              {b.lines.map((l) => (
                <tr key={l.line_no}>
                  <td>{l.line_no}</td>
                  <td>{l.product_name}</td>
                  <td className="muted">{l.batch_no ?? '—'}</td>
                  <td className="num">{qty(l.quantity)} {l.unit}</td>
                  <td className="num">{Number(l.bonus_quantity) ? qty(l.bonus_quantity) : '—'}</td>
                  <td className="num">{rs(l.unit_price)}</td>
                  <td className="num"><b>{rs(l.line_total)}</b></td>
                </tr>
              ))}
            </tbody>
          </table>
          <div className="bill-totals">
            <span>Subtotal <b>{rs(b.subtotal)}</b></span>
            {Number(b.discount) > 0 && <span>Discount <b>−{rs(b.discount)}</b></span>}
            <span>Paid <b>{rs(b.paid_amount)}</b></span>
            <span className="grand">Total <b>{rs(b.total)}</b></span>
          </div>
        </div>
      )}
    </Modal>
  )
}
