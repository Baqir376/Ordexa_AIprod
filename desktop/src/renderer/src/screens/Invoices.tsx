import { useEffect, useState } from 'react'

import { formatRs } from '../../../core/money'
import type { AppState, InvoiceView, ReceiptView } from '../../../core/types'
import { ordexa } from '../bridge'
import { Receipt } from './Receipt'

const STATUS: Record<InvoiceView['sync_status'], string> = {
  pending: 'Waiting to sync',
  synced: 'Synced',
  rejected: 'Rejected by server',
  conflict: 'Conflict',
}

export function Invoices({ state }: { state: AppState }) {
  const [rows, setRows] = useState<InvoiceView[]>([])
  const [receipts, setReceipts] = useState<ReceiptView[]>([])
  const [view, setView] = useState<'bills' | 'receipts'>('bills')
  const [open, setOpen] = useState<InvoiceView | null>(null)
  useEffect(() => {
    void ordexa().listInvoices().then(setRows)
    void ordexa().listReceipts().then(setReceipts)
  }, [state.sync.pending, state.sync.last_sync_at])

  if (open) return <Receipt invoice={open} state={state} onNext={() => setOpen(null)} nextLabel="Back to bills" />

  return (
    <div className="page invoices">
      <div className="page-head">
        <div>
          <h1>Bills &amp; receipts</h1>
          <div className="page-sub">Bills made on this PC (also while offline) and cash received at the counter.</div>
        </div>
      </div>
      <div className="subtabs">
        <button className={view === 'bills' ? 'active' : ''} onClick={() => setView('bills')}>Bills on this PC <span className="count">{rows.length}</span></button>
        <button className={view === 'receipts' ? 'active' : ''} onClick={() => setView('receipts')}>Cash receipts <span className="count">{receipts.length}</span></button>
      </div>
      {view === 'bills' ? (
        <table className="lines">
          <thead>
            <tr><th>Bill</th><th>Time</th><th>Customer</th><th className="num">Items</th><th className="num">Total</th><th>Status</th><th>Notes from server</th></tr>
          </thead>
          <tbody>
            {rows.length === 0 && <tr><td colSpan={7} className="empty-row">No bills on this PC yet.</td></tr>}
            {rows.map((r) => (
              <tr key={r.id} className="clickable" onClick={() => setOpen(r)} title="Open to reprint">
                <td><b>{r.number}</b></td>
                <td>{new Date(r.issued_at).toLocaleString('en-PK')}</td>
                <td>{r.customer_name}</td>
                <td className="num">{r.lines.length}</td>
                <td className="num">{formatRs(r.total)}</td>
                <td><span className={`tag ${r.sync_status}`}>{STATUS[r.sync_status]}</span></td>
                <td className="small">{r.sync_error ?? r.server_flags.map((f) => f.code.replace(/_/g, ' ')).join(', ')}</td>
              </tr>
            ))}
          </tbody>
        </table>
      ) : (
        <table className="lines">
          <thead>
            <tr><th>Receipt</th><th>Time</th><th>Customer</th><th className="num">Amount</th><th>Status</th></tr>
          </thead>
          <tbody>
            {receipts.length === 0 && <tr><td colSpan={5} className="empty-row">No cash receipts on this PC. Use Customers → Receive.</td></tr>}
            {receipts.map((r) => (
              <tr key={r.id}>
                <td><b>{r.number}</b></td>
                <td>{new Date(r.received_at).toLocaleString('en-PK')}</td>
                <td>{r.customer_name}</td>
                <td className="num">{formatRs(r.amount)}</td>
                <td><span className={`tag ${r.sync_status}`}>{STATUS[r.sync_status]}</span>{r.sync_error && <span className="small"> {r.sync_error}</span>}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  )
}
