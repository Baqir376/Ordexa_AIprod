import { useState } from 'react'

import { ErrorBox, Page, Table, qtyText, rs, today, useApi } from '../ui'

const TABS = [
  ['sales', 'Sales by day'],
  ['products', 'Sales by product'],
  ['customers', 'Sales by customer'],
  ['profit', 'Profit'],
  ['stock', 'Stock valuation'],
  ['expiry', 'Expiry'],
  ['low', 'Reorder (low stock)'],
  ['receivable', 'Receivable aging'],
  ['payable', 'Payable aging'],
  ['flagged', 'Flagged bills'],
] as const
type Tab = (typeof TABS)[number][0]

function monthStart() {
  return today().slice(0, 8) + '01'
}

function exportCsv(name: string, rows: Record<string, unknown>[] | null) {
  if (!rows?.length) return
  const keys = Object.keys(rows[0])
  const esc = (v: unknown) => `"${String(v ?? '').replace(/"/g, '""')}"`
  const csv = [keys.join(','), ...rows.map((r) => keys.map((k) => esc(r[k])).join(','))].join('\r\n')
  const a = document.createElement('a')
  a.href = URL.createObjectURL(new Blob(['﻿' + csv], { type: 'text/csv' }))
  a.download = `${name}-${today()}.csv`
  a.click()
}

export function Reports({ initial }: { initial?: string }) {
  const [tab, setTab] = useState<Tab>((TABS.find((t) => t[0] === initial)?.[0] ?? 'sales') as Tab)
  const [start, setStart] = useState(monthStart())
  const [end, setEnd] = useState(today())
  const range = `start=${start}&end=${end}`
  const path: Record<Tab, string> = {
    sales: `/api/v1/reports/sales-by-day?${range}`,
    products: `/api/v1/reports/sales-by-product?${range}`,
    customers: `/api/v1/reports/sales-by-customer?${range}`,
    profit: `/api/v1/reports/profit?${range}`,
    stock: '/api/v1/reports/stock',
    expiry: '/api/v1/reports/expiry?days=120',
    low: '/api/v1/reports/low-stock',
    receivable: '/api/v1/aging/customer',
    payable: '/api/v1/aging/supplier',
    flagged: '/api/v1/invoices?flagged=true',
  }
  const { data, error } = useApi<any>(path[tab], [tab, range])
  const rows = Array.isArray(data) ? data : null
  const dated = ['sales', 'products', 'customers', 'profit'].includes(tab)
  const aging = [
    { key: 'name', label: 'Name' },
    { key: '0_30', label: '0–30 days', num: true, render: (r: any) => rs(r['0_30']) },
    { key: '31_60', label: '31–60', num: true, render: (r: any) => rs(r['31_60']) },
    { key: '61_90', label: '61–90', num: true, render: (r: any) => rs(r['61_90']) },
    { key: 'over_90', label: 'Over 90', num: true, render: (r: any) => rs(r.over_90) },
    { key: 'balance', label: 'Balance', num: true, render: (r: any) => <b>{rs(r.balance)}</b> },
  ]
  const columns: Record<Tab, any[]> = {
    sales: [{ key: 'date', label: 'Date' }, { key: 'invoices', label: 'Bills', num: true }, { key: 'sales', label: 'Sales', num: true, render: (r: any) => rs(r.sales) }],
    products: [{ key: 'name', label: 'Product' }, { key: 'quantity', label: 'Qty', num: true, render: (r: any) => qtyText(r.quantity) }, { key: 'bonus', label: 'Bonus', num: true, render: (r: any) => qtyText(r.bonus) }, { key: 'amount', label: 'Amount', num: true, render: (r: any) => rs(r.amount) }],
    customers: [{ key: 'customer', label: 'Customer' }, { key: 'invoices', label: 'Bills', num: true }, { key: 'amount', label: 'Amount', num: true, render: (r: any) => rs(r.amount) }],
    profit: [],
    stock: [{ key: 'code', label: 'Code' }, { key: 'name', label: 'Product' }, { key: 'batch_no', label: 'Batch', render: (r: any) => r.batch_no ?? '—' }, { key: 'expiry_date', label: 'Expiry', render: (r: any) => r.expiry_date ?? '—' }, { key: 'on_hand', label: 'On hand', num: true, render: (r: any) => qtyText(r.on_hand) }, { key: 'cost_price', label: 'Cost', num: true, render: (r: any) => rs(r.cost_price) }, { key: 'value', label: 'Value', num: true, render: (r: any) => rs(r.value) }],
    expiry: [{ key: 'name', label: 'Product' }, { key: 'batch_no', label: 'Batch' }, { key: 'expiry_date', label: 'Expiry' }, { key: 'days_left', label: 'Days left', num: true, render: (r: any) => <span className={r.expired ? 'bad-text' : r.days_left < 30 ? 'warn-text' : ''}>{r.expired ? 'EXPIRED' : r.days_left}</span> }, { key: 'on_hand', label: 'On hand', num: true, render: (r: any) => qtyText(r.on_hand) }, { key: 'value', label: 'Value at cost', num: true, render: (r: any) => rs(r.value) }],
    low: [{ key: 'code', label: 'Code' }, { key: 'name', label: 'Product' }, { key: 'on_hand', label: 'On hand', num: true, render: (r: any) => qtyText(r.on_hand) }, { key: 'reorder_level', label: 'Reorder level', num: true, render: (r: any) => qtyText(r.reorder_level) }, { key: 'sold_last_30_days', label: 'Sold (30 days)', num: true, render: (r: any) => qtyText(r.sold_last_30_days) }, { key: 'suggested_order', label: 'Suggested order', num: true, render: (r: any) => <b>{qtyText(r.suggested_order)}</b> }],
    receivable: aging,
    payable: aging,
    flagged: [{ key: 'number', label: 'Bill' }, { key: 'issued_at', label: 'Time', render: (r: any) => new Date(r.issued_at).toLocaleString('en-PK') }, { key: 'customer_name', label: 'Customer' }, { key: 'total', label: 'Total', num: true, render: (r: any) => rs(r.total) }, { key: 'flags', label: 'Why flagged', render: (r: any) => r.flags.map((f: any) => f.code.replace(/_/g, ' ')).join(', ') }],
  }

  return (
    <Page
      title="Reports"
      actions={
        <>
          {dated && (
            <>
              <input type="date" value={start} onChange={(e) => setStart(e.target.value)} aria-label="From" />
              <input type="date" value={end} onChange={(e) => setEnd(e.target.value)} aria-label="To" />
            </>
          )}
          {rows && <button className="btn" onClick={() => exportCsv(tab, rows)}>Export CSV</button>}
        </>
      }
    >
      <div className="subtabs">
        {TABS.map(([k, label]) => (
          <button key={k} className={tab === k ? 'active' : ''} onClick={() => setTab(k)}>{label}</button>
        ))}
      </div>
      <ErrorBox error={error} />
      {tab === 'profit' ? (
        data && !Array.isArray(data) && (
          <div className="card pad profit">
            {[['Sales', data.sales], ['Cost of goods sold', data.cost_of_goods], ['Gross profit', data.gross_profit], ['Expenses', data.expenses], ['Net profit', data.net_profit], ['Purchases in period', data.purchases]].map(([l, v]) => (
              <div key={l} className={`sum-row ${l === 'Net profit' ? 'total' : ''}`}><span>{l}</span><span>{rs(v)}</span></div>
            ))}
            {data.lines_without_cost > 0 && <p className="muted small">{data.lines_without_cost} sold lines have no purchase cost recorded and are counted at zero cost.</p>}
          </div>
        )
      ) : (
        <Table rows={rows} columns={columns[tab]} empty="Nothing to show for this period." />
      )}
    </Page>
  )
}
