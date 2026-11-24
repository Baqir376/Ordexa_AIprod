// Dashboard: the business at a glance. Greeting and quick actions, four headline figures, a 14-day sales chart,
// the WhatsApp desk, things that need attention, and the best-selling products.

import { useMemo, useState } from 'react'

import { Icon } from '../icons'
import { ErrorBox, qtyText, rs, useApi } from '../ui'

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
interface Desk { ready: number; fix: number; waiting_shop: number; messages: number }

const compact = (v: number) =>
  v >= 1e7 ? `${(v / 1e7).toFixed(1)} Cr` : v >= 1e5 ? `${(v / 1e5).toFixed(1)} L` : v >= 1e3 ? `${(v / 1e3).toFixed(1)}k` : String(Math.round(v))

function greeting() {
  const h = new Date().getHours()
  return h < 12 ? 'Good morning' : h < 17 ? 'Good afternoon' : 'Good evening'
}

/** Smooth area chart (SVG, no library) with hover read-out. */
function SalesChart({ trend }: { trend: Dash['trend'] }) {
  const [hover, setHover] = useState<number | null>(null)
  const W = 640
  const H = 220
  const pad = { l: 8, r: 8, t: 16, b: 26 }
  const vals = trend.map((t) => Number(t.sales))
  const max = Math.max(1, ...vals) * 1.15
  const x = (i: number) => pad.l + (i * (W - pad.l - pad.r)) / Math.max(1, trend.length - 1)
  const y = (v: number) => pad.t + (1 - v / max) * (H - pad.t - pad.b)
  const pts = vals.map((v, i) => [x(i), y(v)] as const)
  const line = pts.reduce((d, [px, py], i) => {
    if (i === 0) return `M${px},${py}`
    const [qx, qy] = pts[i - 1]
    const cx = (qx + px) / 2
    return `${d} C${cx},${qy} ${cx},${py} ${px},${py}`
  }, '')
  const area = `${line} L${x(trend.length - 1)},${H - pad.b} L${x(0)},${H - pad.b} Z`
  const h = hover !== null ? trend[hover] : null
  return (
    <div className="chart-wrap">
      <svg viewBox={`0 0 ${W} ${H}`} className="area-chart" role="img" aria-label="Daily sales for the last 14 days" onMouseLeave={() => setHover(null)}>
        <defs>
          <linearGradient id="salesFill" x1="0" x2="0" y1="0" y2="1">
            <stop offset="0%" stopColor="var(--accent)" stopOpacity="0.28" />
            <stop offset="100%" stopColor="var(--accent)" stopOpacity="0" />
          </linearGradient>
        </defs>
        {[0.25, 0.5, 0.75].map((g) => (
          <line key={g} x1={pad.l} x2={W - pad.r} y1={pad.t + g * (H - pad.t - pad.b)} y2={pad.t + g * (H - pad.t - pad.b)} className="grid" />
        ))}
        <path d={area} fill="url(#salesFill)" className="area" />
        <path d={line} className="line" />
        {pts.map(([px, py], i) => (
          <g key={i}>
            <rect x={px - (W / trend.length) / 2} y={0} width={W / trend.length} height={H} fill="transparent" onMouseEnter={() => setHover(i)} />
            <circle cx={px} cy={py} r={hover === i ? 5.5 : 3} className={`pt ${hover === i ? 'on' : ''}`} />
            <text x={px} y={H - 8} className="tick">{trend[i].date.slice(8)}</text>
          </g>
        ))}
        {hover !== null && <line x1={pts[hover][0]} x2={pts[hover][0]} y1={pad.t} y2={H - pad.b} className="cursor" />}
      </svg>
      {h && (
        <div className="chart-tip" style={{ left: `${(pts[hover!][0] / W) * 100}%` }}>
          <b>{rs(h.sales)}</b>
          <span>{new Date(h.date).toLocaleDateString('en-PK', { weekday: 'short', day: 'numeric', month: 'short' })} · {h.invoices} bills</span>
        </div>
      )}
    </div>
  )
}

export function Dashboard({ go }: { go: (tab: string) => void }) {
  const { data, error, reload, loading } = useApi<Dash>('/api/v1/reports/dashboard')
  const desk = useApi<Desk>('/api/v1/ai/orders/counts')
  const weekTotal = useMemo(() => (data ? data.trend.slice(-7).reduce((s, t) => s + Number(t.sales), 0) : 0), [data])
  const prevWeek = useMemo(() => (data ? data.trend.slice(-14, -7).reduce((s, t) => s + Number(t.sales), 0) : 0), [data])
  const change = prevWeek > 0 ? ((weekTotal - prevWeek) / prevWeek) * 100 : null
  const topMax = Math.max(1, ...(data?.top_products ?? []).map((p) => Number(p.amount)))

  return (
    <div className="dash">
      <section className="dash-hero">
        <div>
          <div className="dash-date">{new Date().toLocaleDateString('en-PK', { weekday: 'long', day: 'numeric', month: 'long', year: 'numeric' })}</div>
          <h1>{greeting()}</h1>
          <p>Here is how the business is doing today.</p>
        </div>
        <div className="dash-actions">
          <button className="btn" onClick={() => void reload()} disabled={loading}>Refresh</button>
          <button className="btn" onClick={() => go('ai')}><Icon name="message" size={16} /> WhatsApp orders</button>
          <button className="btn primary" onClick={() => go('billing')}><Icon name="plus" size={16} /> New bill</button>
        </div>
      </section>
      <ErrorBox error={error} />
      {!data ? (
        <div className="muted">Loading…</div>
      ) : (
        <>
          <section className="dash-kpis">
            <article className="dk tilt dk-sales">
              <span className="dk-ico"><Icon name="receipt" size={18} /></span>
              <span className="dk-label">Sales today</span>
              <span className="dk-value">{rs(data.today.sales)}</span>
              <span className="dk-sub">{data.today.invoices} bills today</span>
            </article>
            <article className="dk tilt dk-month">
              <span className="dk-ico"><Icon name="reports" size={18} /></span>
              <span className="dk-label">This month</span>
              <span className="dk-value">{rs(data.month.sales)}</span>
              <span className="dk-sub">
                {data.month.invoices} bills
                {change !== null && <em className={change >= 0 ? 'up' : 'down'}>{change >= 0 ? '▲' : '▼'} {Math.abs(change).toFixed(0)}% vs last week</em>}
              </span>
            </article>
            <article className="dk tilt dk-recv" onClick={() => go('customers')} role="button">
              <span className="dk-ico"><Icon name="customers" size={18} /></span>
              <span className="dk-label">Shops owe you</span>
              <span className="dk-value">{rs(data.receivable)}</span>
              <span className="dk-sub">Payable to suppliers {rs(data.payable)}</span>
            </article>
            <article className="dk tilt dk-cash">
              <span className="dk-ico"><Icon name="expenses" size={18} /></span>
              <span className="dk-label">Cash collected today</span>
              <span className="dk-value">{rs(data.today.cash_collected)}</span>
              <span className="dk-sub">Counter and payments received</span>
            </article>
          </section>

          <section className="dash-grid">
            <article className="card dash-chart">
              <header>
                <div>
                  <h2>Sales, last 14 days</h2>
                  <span className="muted small">Last 7 days: Rs {compact(weekTotal)}</span>
                </div>
                <span className="legend"><i /> Daily sales</span>
              </header>
              <SalesChart trend={data.trend} />
            </article>

            <article className="card dash-desk">
              <header>
                <h2>WhatsApp desk</h2>
                <button className="btn small ghost" onClick={() => go('ai')}>Open <Icon name="chevron" size={14} /></button>
              </header>
              <div className="desk-rows">
                {([
                  ['ready', 'Ready to approve', 'checkCircle'],
                  ['fix', 'Needs your fix', 'alert'],
                  ['waiting_shop', 'Waiting for the shop', 'clock'],
                  ['messages', 'Messages to answer', 'message'],
                ] as const).map(([k, label, icon]) => (
                  <button key={k} className={`desk-row dr-${k}`} onClick={() => go('ai')}>
                    <span className="dr-ico"><Icon name={icon} size={16} /></span>
                    <span>{label}</span>
                    <b>{desk.data?.[k] ?? '–'}</b>
                  </button>
                ))}
              </div>
            </article>

            <article className="card dash-attn">
              <header><h2>Needs attention</h2></header>
              {([
                ['reports:expiry', data.near_expiry_batches, 'batches expire within 90 days', 'clock', 'warn'],
                ['reports:low', data.low_stock_products, 'products at their reorder level', 'products', 'warn'],
                ['cheques', data.pending_cheques, 'cheques waiting to clear', 'cheques', 'info'],
                ['reports:flagged', data.flagged_invoices, 'bills flagged this month', 'alert', 'bad'],
              ] as const).map(([to, n, text, icon, tone]) => (
                <button key={to} className={`attn ${n ? `attn-${tone}` : 'attn-clear'}`} onClick={() => go(to)}>
                  <span className="attn-ico"><Icon name={n ? icon : 'checkCircle'} size={16} /></span>
                  <span className="attn-text"><b>{n}</b> {text}</span>
                  <Icon name="chevron" size={14} />
                </button>
              ))}
            </article>

            <article className="card dash-top">
              <header><h2>Top products this month</h2></header>
              {data.top_products.length === 0 && <div className="muted small">No sales yet this month.</div>}
              {data.top_products.map((p, i) => (
                <div key={p.name} className="top-row">
                  <span className="top-rank">{i + 1}</span>
                  <div className="top-main">
                    <div className="top-line"><b>{p.name}</b><span>{rs(p.amount)}</span></div>
                    <div className="top-bar"><i style={{ width: `${(Number(p.amount) / topMax) * 100}%` }} /></div>
                    <span className="muted small">{qtyText(p.quantity)} sold</span>
                  </div>
                </div>
              ))}
            </article>
          </section>
        </>
      )}
    </div>
  )
}
