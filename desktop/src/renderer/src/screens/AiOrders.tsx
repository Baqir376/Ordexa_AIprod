// WhatsApp / AI order desk: every incoming order becomes a draft; a person fixes what the system flags and
// approves it into a bill with one click.

import { useEffect, useState } from 'react'

import type { AppState, CustomerHit, ProductHit } from '../../../core/types'
import { ordexa } from '../bridge'
import { ErrorBox, Modal, can, rs, uid, useAction, useApi } from '../ui'

interface Line {
  line_no: number
  phrase: string
  quantity: number | null
  unit: string | null
  product_id: string | null
  product_name: string | null
  sale_unit: string | null
  resolution: string | null
  alias_version: number | null
  candidates: { product_id: string; name: string; code: string; score: number }[]
  issues: string[]
  unit_price: string | null
  line_total: string | null
  available: string | null
  read_as?: string | null
}
interface ShopUpdate {
  kind: 'answered' | 'changed' | 'cancelled' | 'message' | 'thanks'
  at: string | null
  message: string | null
  changes: string[]
}
interface Order {
  id: string
  number: string
  source: string
  sender: string | null
  status: string
  customer: { id: string; code: string; name: string } | null
  raw_text: string
  kind?: 'text' | 'image' | 'audio'
  has_attachment?: boolean
  transcript?: string | null
  received_at: string
  line_count: number
  warnings: { code: string; message: string }[]
  invoice: { id: string; number: string; total: string } | null
  review_note: string | null
  shop_updates?: ShopUpdate[]
  lines?: Line[]
  questions?: string[]
  trace?: Record<string, unknown>[]
  extractor?: string
}

const STATUS_LABEL: Record<string, string> = {
  received: 'Waiting for AI',
  processing: 'Reading…',
  needs_review: 'Needs your check',
  draft_ready: 'Ready to approve',
  approved: 'Billed',
  rejected: 'Rejected',
  failed: 'Failed',
}
const UPDATE_LABEL: Record<ShopUpdate['kind'], string> = {
  answered: 'Answered the question',
  changed: 'Changed the order',
  cancelled: 'Cancelled the order',
  message: 'Message for you',
  thanks: 'Said thanks',
}

function shopBadge(o: Order): string | null {
  if (o.status === 'rejected' && o.review_note?.startsWith('Cancelled by the shop')) return 'Cancelled by shop'
  const u = (o.shop_updates ?? []).filter((x) => x.kind !== 'thanks')
  if (!u.length) return null
  const last = u[u.length - 1]
  return last.kind === 'message' ? 'Shop asked a question' : last.kind === 'changed' ? 'Changed by shop' : 'Shop replied'
}

const ISSUE_TEXT: Record<string, string> = {
  ambiguous: 'Which product? Pick one below',
  not_found: 'Not in catalogue: choose a product',
  missing_quantity: 'Quantity missing',
  unit_mismatch: 'Different unit: confirm quantity',
  insufficient_stock: 'Not enough stock',
  inactive_product: 'Product inactive',
}

export function AiOrders({ state }: { state: AppState }) {
  const perms = state.user?.permissions
  const [filter, setFilter] = useState<'open' | 'approved' | 'rejected'>('open')
  const list = useApi<Order[]>(`/api/v1/ai/orders?status=${filter}`, [filter])
  const [openId, setOpenId] = useState<string | null>(null)
  const [newOrder, setNewOrder] = useState(false)

  useEffect(() => {
    const t = setInterval(() => void list.reload(), 10_000) // new WhatsApp orders appear without refreshing
    return () => clearInterval(t)
  }, [list.reload])

  return (
    <div className="desk">
      <div className="desk-list">
        <div className="page-head">
          <h1>WhatsApp orders</h1>
          {can(perms, 'sales_order.create') && <button className="btn primary small" onClick={() => setNewOrder(true)}>+ Paste order</button>}
        </div>
        <div className="subtabs">
          {(['open', 'approved', 'rejected'] as const).map((f) => (
            <button key={f} className={filter === f ? 'active' : ''} onClick={() => setFilter(f)}>{f}</button>
          ))}
        </div>
        <ErrorBox error={list.error} />
        {list.data?.length === 0 && <div className="muted pad">No {filter} orders.</div>}
        {list.data?.map((o) => (
          <button key={o.id} className={`order-card ${openId === o.id ? 'active' : ''}`} onClick={() => setOpenId(o.id)}>
            <div className="oc-top">
              <b>{o.customer?.name ?? o.sender ?? 'Unknown sender'}</b>
              <span className={`tag st-${o.status}`}>{STATUS_LABEL[o.status] ?? o.status}</span>
            </div>
            {shopBadge(o) && <div className="shop-badge">{shopBadge(o)}</div>}
            <div className="oc-text">{o.kind === 'image' ? '[Photo] ' : o.kind === 'audio' ? '[Voice note] ' : ''}{o.raw_text.startsWith('[') ? '' : o.raw_text}</div>
            <div className="muted small">{o.number} · {o.source === 'whatsapp' ? 'WhatsApp' : 'Typed'} · {new Date(o.received_at).toLocaleTimeString('en-PK')}</div>
          </button>
        ))}
      </div>
      <div className="desk-detail">
        {openId ? <OrderDetail key={openId} id={openId} state={state} onChanged={() => void list.reload()} /> : <div className="empty">Select an order on the left.</div>}
      </div>
      {newOrder && <NewOrderModal onClose={() => setNewOrder(false)} onCreated={(id) => { setNewOrder(false); setOpenId(id); void list.reload() }} />}
    </div>
  )
}

function OrderDetail({ id, state, onChanged }: { id: string; state: AppState; onChanged: () => void }) {
  const perms = state.user?.permissions
  const [order, setOrder] = useState<Order | null>(null)
  const [remember, setRemember] = useState(true)
  const [qtyEdit, setQtyEdit] = useState<Record<number, string>>({})
  const [reject, setReject] = useState<string | null>(null)
  const [adding, setAdding] = useState(false)
  const [showTrace, setShowTrace] = useState(false)
  const { busy, error, setError, run } = useAction()

  const [flash, setFlash] = useState(false)
  const load = async () => {
    const next = await ordexa().request<Order>('GET', `/api/v1/ai/orders/${id}`)
    setOrder((prev) => {
      if (prev && (next.shop_updates?.length ?? 0) > (prev.shop_updates?.length ?? 0)) {
        setFlash(true)
        onChanged()
      }
      return next
    })
  }
  const typing = Object.values(qtyEdit).some(Boolean) || adding || reject !== null || busy
  useEffect(() => {
    void load()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [id])
  useEffect(() => {
    const fast = order?.status === 'received' || order?.status === 'processing'
    // Keep the open order live: the AI may still be reading it, or the shop may change it in the chat.
    const t = setInterval(() => { if (!typing) void load() }, fast ? 3000 : 5000)
    return () => clearInterval(t)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [id, order?.status, typing])
  useEffect(() => {
    if (!flash) return
    const t = setTimeout(() => setFlash(false), 8000)
    return () => clearTimeout(t)
  }, [flash])

  async function act(path: string, body: unknown = {}) {
    const r = await run(() => ordexa().request<Order>('POST', `/api/v1/ai/orders/${id}${path}`, body))
    if (r) {
      setOrder(r)
      onChanged()
    }
    return r
  }
  async function approve() {
    const soft = order!.warnings.some((w) => ['credit_review', 'customer_inactive'].includes(w.code)) || order!.lines!.some((l) => l.issues.includes('insufficient_stock'))
    if (soft && !window.confirm('This order has warnings (credit or stock). Approve and bill anyway?')) return
    await act('/approve', { acknowledge_warnings: soft })
  }

  if (!order) return <div className="muted pad">Loading…</div>
  const editable = !['approved', 'rejected', 'received', 'processing'].includes(order.status)
  const total = (order.lines ?? []).reduce((s, l) => s + (l.line_total ? Number(l.line_total) : 0), 0)

  return (
    <div className="page">
      <div className="page-head">
        <h1>{order.number} · {order.customer ? `${order.customer.code} ${order.customer.name}` : 'Customer not linked'}</h1>
        <span className={`tag st-${order.status}`}>{STATUS_LABEL[order.status]}</span>
      </div>
      <div className="message-bubble">
        <div className="muted small">{order.source === 'whatsapp' ? `WhatsApp ${order.sender ?? ''}` : 'Typed by staff'} · {new Date(order.received_at).toLocaleString('en-PK')}</div>
        {order.raw_text.startsWith('[') && order.kind !== 'text' ? null : order.raw_text}
        {order.has_attachment && <Attachment orderId={order.id} kind={order.kind ?? 'text'} />}
        {order.transcript && <div className="transcript"><span className="muted small">Heard in the voice note:</span> {order.transcript}</div>}
      </div>
      {flash && <div className="alert ok">The shop just updated this order on WhatsApp. See below.</div>}
      {(order.shop_updates?.length ?? 0) > 0 && (
        <div className="card pad shop-updates">
          <div className="muted small">Updates from the shop on WhatsApp</div>
          <ul>
            {order.shop_updates!.map((u, i) => (
              <li key={i} className={`upd upd-${u.kind}`}>
                <span className="muted small">{u.at ? new Date(u.at).toLocaleTimeString('en-PK') : ''}</span>{' '}
                <b>{UPDATE_LABEL[u.kind]}</b>
                {u.message && <span>: “{u.message}”</span>}
                {u.changes.length > 0 && <div className="small">{u.changes.join(' ')}</div>}
              </li>
            ))}
          </ul>
        </div>
      )}
      {order.warnings.map((w) => <div key={w.code} className={`alert ${w.code === 'contains_instructions' ? 'error' : 'warn'}`}>{w.message}</div>)}
      {order.status === 'received' || order.status === 'processing' ? (
        <div className="muted">The AI worker is reading this order…</div>
      ) : (
        <table className="lines">
          <thead><tr><th>#</th><th>Customer wrote</th><th>Product</th><th className="num">Qty</th><th className="num">Rate</th><th className="num">Amount</th><th className="num">Stock</th><th /></tr></thead>
          <tbody>
            {(order.lines ?? []).map((l) => {
              const blocking = l.issues.filter((i) => i !== 'insufficient_stock')
              return (
                <tr key={l.line_no} className={blocking.length ? 'row-attention' : ''}>
                  <td>{l.line_no}</td>
                  <td>
                    <b>{l.phrase}</b>
                    {l.read_as && <div className="muted small">read as “{l.read_as}”</div>}
                    <div className="muted small">{l.quantity ?? '?'} {l.unit ?? ''}</div>
                  </td>
                  <td>
                    {l.product_name ? (
                      <>
                        {l.product_name}
                        <div className="muted small">{l.resolution === 'alias' ? `remembered for this shop (v${l.alias_version})` : l.resolution === 'operator' ? 'chosen by staff' : l.resolution === 'customer' ? 'confirmed by the shop' : 'matched catalogue'}</div>
                      </>
                    ) : null}
                    {l.issues.map((i) => <div key={i} className={i === 'insufficient_stock' ? 'warn-text small' : 'bad-text small'}>{ISSUE_TEXT[i] ?? i}</div>)}
                    {editable && (l.issues.includes('ambiguous') || l.issues.includes('not_found')) && (
                      <div className="cand-row">
                        {l.candidates.map((c) => (
                          <button key={c.product_id} className="btn small" disabled={busy} onClick={() => void act('/resolve-line', { line_no: l.line_no, product_id: c.product_id, remember })}>
                            {c.name}
                          </button>
                        ))}
                        <ProductPicker onPick={(p) => void act('/resolve-line', { line_no: l.line_no, product_id: p.id, remember })} />
                      </div>
                    )}
                  </td>
                  <td className="num">
                    {editable && (l.issues.includes('missing_quantity') || l.issues.includes('unit_mismatch')) ? (
                      <span className="qty-fix">
                        <input className="num-in" placeholder={l.sale_unit ?? 'qty'} value={qtyEdit[l.line_no] ?? ''} onChange={(e) => setQtyEdit({ ...qtyEdit, [l.line_no]: e.target.value })} />
                        <button className="btn small" disabled={!qtyEdit[l.line_no] || busy} onClick={() => void act('/resolve-line', { line_no: l.line_no, quantity: qtyEdit[l.line_no] })}>OK</button>
                      </span>
                    ) : (
                      <>{l.quantity ?? '—'} {l.sale_unit ?? ''}</>
                    )}
                  </td>
                  <td className="num">{rs(l.unit_price)}</td>
                  <td className="num">{rs(l.line_total)}</td>
                  <td className="num">{l.available ? Number(l.available) : '—'}</td>
                  <td>{editable && <button className="x" title="Remove line" onClick={() => void act(`/lines/${l.line_no}/remove`)}>×</button>}</td>
                </tr>
              )
            })}
          </tbody>
        </table>
      )}
      {editable && (
        <div className="desk-tools">
          <label className="check"><input type="checkbox" checked={remember} onChange={(e) => setRemember(e.target.checked)} disabled={!can(perms, 'ai_alias.approve')} /> Remember my product choices for this shop</label>
          <button className="btn small" onClick={() => setAdding(true)}>+ Add item</button>
          {!order.customer && <CustomerLinker onPick={(c) => void act('/customer', { customer_id: c.id })} />}
        </div>
      )}
      {order.questions && order.questions.length > 0 && editable && (
        <div className="card pad">
          <div className="muted small">Questions sent to the customer on WhatsApp:</div>
          <ul className="small">{order.questions.map((q, i) => <li key={i}>{q}</li>)}</ul>
        </div>
      )}
      <ErrorBox error={error} />
      {order.invoice && <div className="alert ok">Billed as {order.invoice.number} · {rs(order.invoice.total)}</div>}
      {order.review_note && <div className="alert warn">{order.review_note.startsWith('Cancelled by the shop') ? order.review_note : `Rejected: ${order.review_note}`}</div>}
      {editable && (
        <div className="approve-bar">
          <span>Total <b>{rs(total.toFixed(2))}</b></span>
          <button className="btn danger" onClick={() => setReject('')}>Reject</button>
          {can(perms, 'sales_order.approve') ? (
            <button className="btn primary big" disabled={busy || order.status !== 'draft_ready'} onClick={() => void approve()} title={order.status !== 'draft_ready' ? 'Fix the red lines first' : 'Create the bill'}>
              Approve & create bill
            </button>
          ) : (
            <span className="muted small">An approver must create the bill.</span>
          )}
        </div>
      )}
      <button className="btn ghost small" onClick={() => setShowTrace(!showTrace)}>{showTrace ? 'Hide' : 'Show'} how this was read ({order.extractor})</button>
      {showTrace && <pre className="trace">{JSON.stringify(order.trace, null, 2)}</pre>}
      {reject !== null && (
        <Modal title="Reject order" onClose={() => setReject(null)}>
          <div className="form">
            <input autoFocus value={reject} onChange={(e) => setReject(e.target.value)} placeholder="Reason (e.g. duplicate, customer cancelled)" />
            <div className="modal-actions">
              <button className="btn" onClick={() => setReject(null)}>Cancel</button>
              <button className="btn danger" disabled={reject.length < 3} onClick={() => void act('/reject', { reason: reject }).then(() => setReject(null))}>Reject</button>
            </div>
          </div>
        </Modal>
      )}
      {adding && (
        <Modal title="Add item" onClose={() => setAdding(false)}>
          <AddItem onAdd={(p, q) => { setAdding(false); setError(null); void act('/lines', { product_id: p.id, quantity: q }) }} />
        </Modal>
      )}
    </div>
  )
}

function ProductPicker({ onPick }: { onPick: (p: ProductHit) => void }) {
  const [q, setQ] = useState('')
  const [hits, setHits] = useState<ProductHit[]>([])
  return (
    <span className="combo inline-combo">
      <input value={q} placeholder="other product…" onChange={async (e) => { setQ(e.target.value); setHits(e.target.value ? await ordexa().searchProducts(e.target.value) : []) }} />
      {hits.length > 0 && <ul className="dropdown">{hits.slice(0, 8).map((h) => <li key={h.id} onMouseDown={() => { onPick(h); setQ(''); setHits([]) }}>{h.name}</li>)}</ul>}
    </span>
  )
}

function CustomerLinker({ onPick }: { onPick: (c: CustomerHit) => void }) {
  const [q, setQ] = useState('')
  const [hits, setHits] = useState<CustomerHit[]>([])
  return (
    <span className="combo inline-combo">
      <input value={q} placeholder="Link customer: code or name" onChange={async (e) => { setQ(e.target.value); setHits(e.target.value ? await ordexa().searchCustomers(e.target.value) : []) }} />
      {hits.length > 0 && <ul className="dropdown">{hits.map((c) => <li key={c.id} onMouseDown={() => { onPick(c); setQ(''); setHits([]) }}><b>{c.code}</b> {c.name}</li>)}</ul>}
    </span>
  )
}

function AddItem({ onAdd }: { onAdd: (p: ProductHit, qty: string) => void }) {
  const [picked, setPicked] = useState<ProductHit | null>(null)
  const [qty, setQty] = useState('1')
  return (
    <div className="form">
      {picked ? <div className="chip">{picked.name}<button className="x" onClick={() => setPicked(null)}>×</button></div> : <ProductPicker onPick={setPicked} />}
      <input value={qty} onChange={(e) => setQty(e.target.value)} placeholder="Quantity" />
      <div className="modal-actions"><button className="btn primary" disabled={!picked || !qty} onClick={() => onAdd(picked!, qty)}>Add</button></div>
    </div>
  )
}

function NewOrderModal({ onClose, onCreated }: { onClose: () => void; onCreated: (id: string) => void }) {
  const [customer, setCustomer] = useState<CustomerHit | null>(null)
  const [text, setText] = useState('')
  const { busy, error, run } = useAction()
  return (
    <Modal title="Order from text (phone call, SMS, forwarded WhatsApp)" onClose={onClose}>
      <div className="form">
        {customer ? <div className="chip"><b>{customer.code}</b> {customer.name}<button className="x" onClick={() => setCustomer(null)}>×</button></div> : <CustomerLinker onPick={setCustomer} />}
        <textarea rows={6} value={text} onChange={(e) => setText(e.target.value)} placeholder={'e.g.\n10 box panadol extra\n5 patta brufen aur 2 botal calpol'} />
        <ErrorBox error={error} />
        <div className="modal-actions">
          <button className="btn" onClick={onClose}>Cancel</button>
          <button className="btn primary" disabled={busy || !customer || !text.trim()} onClick={() => void run(() => ordexa().request<Order>('POST', '/api/v1/ai/orders', { id: uid(), customer_id: customer!.id, text })).then((o) => o && onCreated(o.id))}>Read order</button>
        </div>
      </div>
    </Modal>
  )
}

/** The shop's original photo (zoomable) or voice note (playable). */
function Attachment({ orderId, kind }: { orderId: string; kind: string }) {
  const [url, setUrl] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [zoom, setZoom] = useState(false)
  useEffect(() => {
    ordexa().dataUrl(`/api/v1/ai/orders/${orderId}/attachment`).then(setUrl, (e: Error) => setError(e.message))
  }, [orderId])
  if (error) return <div className="bad-text small">{error}</div>
  if (!url) return <div className="muted small">Loading {kind === 'audio' ? 'voice note' : 'photo'}…</div>
  if (kind === 'audio') return <audio className="attachment-audio" controls src={url} />
  return (
    <>
      <img className="attachment-img" src={url} alt="Order photo sent by the shop" onClick={() => setZoom(true)} title="Click to enlarge" />
      {zoom && (
        <div className="modal-backdrop" onMouseDown={() => setZoom(false)}>
          <img className="attachment-zoom" src={url} alt="Order photo, enlarged" />
        </div>
      )}
    </>
  )
}
