import { useState } from 'react'

import type { AppState } from '../../../core/types'
import { ordexa } from '../bridge'
import { ErrorBox, Field, Figures, Modal, Monogram, Page, Table, can, rs, today, uid, useAction, useApi } from '../ui'

interface Customer {
  id: string
  code: string
  name: string
  phone: string | null
  address: string | null
  credit_limit: string | null
  is_active: boolean
}
interface AgingRow { party_id: string; balance: string; '0_30': string; '31_60': string; '61_90': string; over_90: string }
interface Ledger { balance: string; entries: { date: string; kind: string; reference: string; memo: string | null; debit: string | null; credit: string | null; balance: string }[] }

export function Customers({ state }: { state: AppState }) {
  const perms = state.user?.permissions
  const [q, setQ] = useState('')
  const list = useApi<Customer[]>(`/api/v1/customers?q=${encodeURIComponent(q)}`, [q])
  const aging = useApi<AgingRow[]>(can(perms, 'finance.view') ? '/api/v1/aging/customer' : null)
  const [editing, setEditing] = useState<Partial<Customer> | null>(null)
  const [ledgerFor, setLedgerFor] = useState<Customer | null>(null)
  const [paying, setPaying] = useState<Customer | null>(null)
  const bal = new Map((aging.data ?? []).map((a) => [a.party_id, a]))
  const owedTotal = (aging.data ?? []).reduce((t, a) => t + Math.max(0, Number(a.balance)), 0)
  const over90 = (aging.data ?? []).reduce((t, a) => t + Number(a.over_90), 0)
  const overLimit = (list.data ?? []).filter((c) => c.credit_limit && Number(bal.get(c.id)?.balance ?? 0) > Number(c.credit_limit)).length

  return (
    <Page
      title="Customers (shops)"
      actions={
        <>
          <input className="search-small" placeholder="Search name or code" value={q} onChange={(e) => setQ(e.target.value)} />
          {can(perms, 'customer.create') && (
            <button className="btn primary" onClick={() => setEditing({})}>
              + New customer
            </button>
          )}
        </>
      }
    >
      <Figures
        items={[
          { label: 'Shops', value: (list.data ?? []).length, icon: 'customers', hue: 'sky', hint: `${(list.data ?? []).filter((c) => c.is_active).length} active` },
          { label: 'Shops owe you', value: rs(owedTotal.toFixed(2)), icon: 'expenses', hue: 'indigo', hint: `${(aging.data ?? []).filter((a) => Number(a.balance) > 0).length} shops with a balance` },
          { label: 'Over credit limit', value: overLimit, icon: 'alert', hue: 'amber', hint: 'Balance above the agreed limit' },
          { label: 'Older than 90 days', value: rs(over90.toFixed(2)), icon: 'clock', hue: 'rose', hint: 'Follow up these payments' },
        ]}
      />
      <ErrorBox error={list.error} />
      <Table
        rows={list.data}
        columns={[
          {
            key: 'name',
            label: 'Shop',
            render: (c) => (
              <span className="cell-main">
                <Monogram text={c.name} />
                <span>
                  <b>{c.name}</b>
                  <span className="cell-sub">{[`Code ${c.code}`, c.phone, c.address].filter(Boolean).join(' · ')}{!c.is_active && ' · inactive'}</span>
                </span>
              </span>
            ),
          },
          {
            key: 'balance',
            label: 'Balance',
            render: (c) => {
              const b = Number(bal.get(c.id)?.balance ?? 0)
              const lim = c.credit_limit ? Number(c.credit_limit) : null
              const pct = lim ? Math.min(100, (b / lim) * 100) : 0
              const tone = lim && b > lim ? 'out' : lim && pct > 75 ? 'low' : 'ok'
              return (
                <span className={`meter-cell tone-${tone}`}>
                  <span className="meter-num"><b>{rs(b.toFixed(2))}</b> <small>{lim ? `of ${rs(c.credit_limit)}` : 'no limit'}</small></span>
                  {lim !== null && <span className="meter"><i style={{ width: `${Math.max(3, pct)}%` }} /></span>}
                </span>
              )
            },
          },
          { key: 'over_90', label: 'Over 90 days', num: true, render: (c) => { const v = bal.get(c.id)?.over_90; return v && v !== '0.00' ? <span className="bad-text">{rs(v)}</span> : '' } },
          {
            key: 'actions',
            label: '',
            render: (c) => (
              <span className="row-actions">
                {can(perms, 'finance.view') && <button className="btn small" onClick={() => setLedgerFor(c)}>Ledger</button>}
                {can(perms, 'payment.record') && <button className="btn small" onClick={() => setPaying(c)}>Receive</button>}
                {can(perms, 'customer.update') && <button className="btn small" onClick={() => setEditing(c)}>Edit</button>}
              </span>
            ),
          },
        ]}
      />
      {editing && <CustomerForm initial={editing} canCredit={can(perms, 'customer.credit_limit.change')} onClose={() => setEditing(null)} onSaved={() => { setEditing(null); void list.reload() }} />}
      {ledgerFor && <LedgerModal party="customer" id={ledgerFor.id} name={ledgerFor.name} onClose={() => setLedgerFor(null)} />}
      {paying && <ReceiveModal customer={paying} onClose={() => setPaying(null)} onDone={() => { setPaying(null); void aging.reload() }} />}
    </Page>
  )
}

function CustomerForm({ initial, canCredit, onClose, onSaved }: { initial: Partial<Customer>; canCredit: boolean; onClose: () => void; onSaved: () => void }) {
  const [f, setF] = useState({
    code: initial.code ?? '',
    name: initial.name ?? '',
    phone: initial.phone ?? '',
    address: initial.address ?? '',
    credit_limit: initial.credit_limit ?? '',
    is_active: initial.is_active ?? true,
  })
  const { busy, error, run } = useAction()
  const set = (k: keyof typeof f) => (e: { target: { value: string } }) => setF({ ...f, [k]: e.target.value })
  async function save() {
    const body: Record<string, unknown> = { name: f.name, phone: f.phone || null, address: f.address || null }
    if (canCredit) body.credit_limit = f.credit_limit === '' ? null : f.credit_limit
    const ok = await run(() =>
      initial.id
        ? ordexa().request('PUT', `/api/v1/customers/${initial.id}`, { ...body, is_active: f.is_active })
        : ordexa().request('POST', '/api/v1/customers', { ...body, code: f.code }),
    )
    if (ok) onSaved()
  }
  return (
    <Modal title={initial.id ? `Edit ${initial.name}` : 'New customer'} onClose={onClose}>
      <div className="form">
        <div className="row-2">
          <Field label="Shop code (used for WhatsApp login)"><input value={f.code} onChange={set('code')} disabled={Boolean(initial.id)} autoFocus /></Field>
          <Field label="Phone / WhatsApp"><input value={f.phone} onChange={set('phone')} /></Field>
        </div>
        <Field label="Name"><input value={f.name} onChange={set('name')} /></Field>
        <Field label="Address"><input value={f.address} onChange={set('address')} /></Field>
        {canCredit && <Field label="Credit limit (Rs, empty = no limit)"><input value={f.credit_limit} onChange={set('credit_limit')} /></Field>}
        {initial.id && (
          <label className="check"><input type="checkbox" checked={f.is_active} onChange={(e) => setF({ ...f, is_active: e.target.checked })} /> Active</label>
        )}
        <ErrorBox error={error} />
        <div className="modal-actions">
          <button className="btn" onClick={onClose}>Cancel</button>
          <button className="btn primary" disabled={busy || !f.name || !f.code} onClick={() => void save()}>Save</button>
        </div>
      </div>
    </Modal>
  )
}

export function LedgerModal({ party, id, name, onClose }: { party: 'customer' | 'supplier'; id: string; name: string; onClose: () => void }) {
  const { data, error } = useApi<Ledger>(`/api/v1/ledger/${party}/${id}`)
  return (
    <Modal title={`Ledger: ${name}`} onClose={onClose} wide>
      <ErrorBox error={error} />
      <div className="ledger-balance">Balance: <b>{rs(data?.balance)}</b></div>
      <div className="scroll-box">
        <Table
          rows={data?.entries ?? null}
          empty="No transactions."
          columns={[
            { key: 'date', label: 'Date' },
            { key: 'reference', label: 'Document' },
            { key: 'kind', label: 'Type', render: (e) => e.kind.replace(/_/g, ' ') },
            { key: 'memo', label: 'Note', render: (e) => e.memo ?? '' },
            { key: 'debit', label: 'Debit', num: true, render: (e) => (e.debit ? rs(e.debit) : '') },
            { key: 'credit', label: 'Credit', num: true, render: (e) => (e.credit ? rs(e.credit) : '') },
            { key: 'balance', label: 'Balance', num: true, render: (e) => rs(e.balance) },
          ]}
        />
      </div>
    </Modal>
  )
}

/** Cash works offline (saved on this PC, synced later); cheque/bank need the server. */
export function ReceiveModal({ customer, onClose, onDone }: { customer: { id: string; name: string }; onClose: () => void; onDone: () => void }) {
  const [method, setMethod] = useState<'cash' | 'cheque' | 'bank'>('cash')
  const [f, setF] = useState({ amount: '', cheque_no: '', cheque_bank: '', cheque_date: today(), note: '' })
  const [done, setDone] = useState<string | null>(null)
  const { busy, error, run } = useAction()
  const set = (k: keyof typeof f) => (e: { target: { value: string } }) => setF({ ...f, [k]: e.target.value })
  async function save() {
    const r = await run(async () => {
      if (method === 'cash') return (await ordexa().recordReceipt({ customerId: customer.id, amount: f.amount, note: f.note })).number
      const p = await ordexa().request<{ number: string }>('POST', '/api/v1/payments', {
        id: uid(), direction: 'in', party_id: customer.id, amount: f.amount, method, note: f.note || null,
        ...(method === 'cheque' ? { cheque_no: f.cheque_no, cheque_bank: f.cheque_bank, cheque_date: f.cheque_date } : {}),
      })
      return p.number
    })
    if (r) setDone(r)
  }
  return (
    <Modal title={`Receive payment: ${customer.name}`} onClose={done ? onDone : onClose}>
      {done ? (
        <div className="form">
          <div className="alert ok">Saved {done}.</div>
          <div className="modal-actions"><button className="btn primary" autoFocus onClick={onDone}>Done</button></div>
        </div>
      ) : (
        <div className="form">
          <div className="seg">
            {(['cash', 'cheque', 'bank'] as const).map((m) => (
              <button key={m} className={method === m ? 'active' : ''} onClick={() => setMethod(m)}>{{ cash: 'Cash', cheque: 'Cheque', bank: 'Bank transfer' }[m]}</button>
            ))}
          </div>
          <Field label="Amount (Rs)"><input value={f.amount} onChange={set('amount')} autoFocus /></Field>
          {method === 'cheque' && (
            <div className="row-3">
              <Field label="Cheque no."><input value={f.cheque_no} onChange={set('cheque_no')} /></Field>
              <Field label="Bank"><input value={f.cheque_bank} onChange={set('cheque_bank')} /></Field>
              <Field label="Cheque date"><input type="date" value={f.cheque_date} onChange={set('cheque_date')} /></Field>
            </div>
          )}
          <Field label="Note"><input value={f.note} onChange={set('note')} /></Field>
          {method === 'cash' && <p className="muted small">Cash receipts work offline and sync automatically.</p>}
          <ErrorBox error={error} />
          <div className="modal-actions">
            <button className="btn" onClick={onClose}>Cancel</button>
            <button className="btn primary" disabled={busy || !f.amount} onClick={() => void save()}>Save receipt</button>
          </div>
        </div>
      )}
    </Modal>
  )
}
