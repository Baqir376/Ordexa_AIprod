import { Page, Stat, Table, rs, qtyText, useApi } from '../ui'

interface Dash {
  today: { date: string; invoices: number; sales: string; cash_collected: string }
  month: { invoices: number; sales: string }
  receivable: string
  payable: string
  pending_cheques: number
  flagged_invoices: number
  near_expiry_batches: number
  low_stock_products: number
  top_products: { name: string; quantity: string; amount: string }[]
  trend: { date: string; invoices: number; sales: string }[]
}

export function Dashboard({ go }: { go: (tab: string) => void }) {
  const { data, error, reload } = useApi<Dash>('/api/v1/reports/dashboard')
  if (error) return <Page title="Dashboard"><div className="alert error">{error}</div></Page>
  if (!data) return <Page title="Dashboard"><div className="muted">Loading…</div></Page>
  const max = Math.max(1, ...data.trend.map((t) => Number(t.sales)))
  return (
    <Page title="Dashboard" actions={<button className="btn" onClick={() => void reload()}>Refresh</button>}>
      <div className="stats">
        <Stat label={`Sales today (${data.today.invoices} bills)`} value={rs(data.today.sales)} />
        <Stat label="Cash collected today" value={rs(data.today.cash_collected)} />
        <Stat label={`Sales this month (${data.month.invoices} bills)`} value={rs(data.month.sales)} />
        <Stat label="Receivable (shops owe)" value={rs(data.receivable)} />
        <Stat label="Payable (we owe suppliers)" value={rs(data.payable)} />
      </div>
      <div className="alerts-row">
        <button className={`alert-tile ${data.near_expiry_batches ? 'warn' : ''}`} onClick={() => go('reports:expiry')}>
          <b>{data.near_expiry_batches}</b> batches expire within 90 days
        </button>
        <button className={`alert-tile ${data.low_stock_products ? 'warn' : ''}`} onClick={() => go('reports:low')}>
          <b>{data.low_stock_products}</b> products at reorder level
        </button>
        <button className={`alert-tile ${data.pending_cheques ? 'warn' : ''}`} onClick={() => go('cheques')}>
          <b>{data.pending_cheques}</b> cheques waiting to clear
        </button>
        <button className={`alert-tile ${data.flagged_invoices ? 'bad' : ''}`} onClick={() => go('reports:flagged')}>
          <b>{data.flagged_invoices}</b> flagged bills this month
        </button>
      </div>
      <div className="two-col">
        <div className="card pad">
          <h2>Sales, last 14 days</h2>
          <div className="bars" role="img" aria-label="Daily sales for the last 14 days">
            {data.trend.map((t) => (
              <div key={t.date} className="bar-col" title={`${t.date}: ${rs(t.sales)} (${t.invoices} bills)`}>
                <div className="bar" style={{ height: `${(Number(t.sales) / max) * 100}%` }} />
                <span>{t.date.slice(8)}</span>
              </div>
            ))}
          </div>
        </div>
        <div className="card pad">
          <h2>Top products this month</h2>
          <Table
            rows={data.top_products}
            empty="No sales yet this month."
            columns={[
              { key: 'name', label: 'Product' },
              { key: 'quantity', label: 'Qty', num: true, render: (r) => qtyText(r.quantity) },
              { key: 'amount', label: 'Amount', num: true, render: (r) => rs(r.amount) },
            ]}
          />
        </div>
      </div>
    </Page>
  )
}
