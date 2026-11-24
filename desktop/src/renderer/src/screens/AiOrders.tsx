// WhatsApp / AI order desk: every incoming order becomes a draft; a person fixes what the system flags and
// approves it into a bill with one click.

import { useEffect, useState } from 'react'

import type { AppState, CustomerHit, ProductHit } from '../../../core/types'
import { ordexa } from '../bridge'
import { ChatThread, Chats, ago, initials } from './Chats'
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
  stage?: string
  add_to_bill?: { invoice_id: string; number: string; order: string | null; possible: boolean; reason: string | null } | null
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
  message: 'Message for you',
  closed: 'Answered by bot',
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

type Filter = 'open' | 'messages' | 'approved' | 'rejected' | 'closed'
const FILTERS: { key: Filter; label: string; icon: string; help: string }[] = [
  { key: 'open', label: 'To do', icon: '📥', help: 'New orders. Approve the green ones, fix the amber ones; blue ones are waiting for the shop to answer the bot.' },
  { key: 'messages', label: 'Messages', icon: '💬', help: 'Complaints and questions from shops that need a person. Reply in Chats, then mark them handled.' },
  { key: 'approved', label: 'Billed', icon: '✅', help: 'Orders you approved. Their bills were sent to the shop on WhatsApp.' },
  { key: 'rejected', label: 'Rejected', icon: '⛔', help: 'Orders you rejected or the shop cancelled.' },
  { key: 'closed', label: 'Bot replied', icon: '🤖', help: 'Messages the bot answered by itself: balance, a copy of the last bill, rates, greetings. Nothing for you to do.' },
]

/** One clear label per order, from what has to happen next. */
const STAGE: Record<string, { label: string; tone: string }> = {
  ready: { label: 'Ready to approve', tone: 'ready' },
  fix: { label: 'Needs your fix', tone: 'fix' },
  waiting_shop: { label: 'Waiting for shop', tone: 'waiting' },
  reading: { label: 'AI reading…', tone: 'reading' },
  message: { label: 'Message', tone: 'message' },
  approved: { label: 'Billed', tone: 'billed' },
  rejected: { label: 'Rejected', tone: 'muted' },
  closed: { label: 'Auto-answered', tone: 'muted' },
}
function stageOf(o: Order) {
  if (o.status === 'rejected' && o.review_note?.startsWith('Cancelled by the shop')) return { label: 'Cancelled by shop', tone: 'muted' }
  return STAGE[o.stage ?? o.status] ?? { label: STATUS_LABEL[o.status] ?? o.status, tone: 'muted' }
}
// Open orders are grouped by what someone has to do next.
const GROUPS: { title: string; stages: string[]; tone: string }[] = [
  { title: 'Ready to approve', stages: ['ready'], tone: 'ready' },
  { title: 'Needs your fix', stages: ['fix'], tone: 'fix' },
  { title: 'Waiting for the shop to reply', stages: ['waiting_shop'], tone: 'waiting' },
  { title: 'AI is reading', stages: ['reading'], tone: 'reading' },
]

export function AiOrders({ state }: { state: AppState }) {
  const perms = state.user?.permissions
  const [view, setView] = useState<'orders' | 'chats'>('orders')
  const [filter, setFilter] = useState<Filter>('open')
  const [q, setQ] = useState('')
  const list = useApi<Order[]>(`/api/v1/ai/orders?status=${filter}`, [filter])
  const counts = useApi<Record<string, number>>('/api/v1/ai/orders/counts')
  const [openId, setOpenId] = useState<string | null>(null)
  const [newOrder, setNewOrder] = useState(false)
  const [selecting, setSelecting] = useState(false)
  const [picked, setPicked] = useState<Set<string>>(new Set())
  const { busy, error: delError, run } = useAction()
  const canDelete = can(perms, 'sales_order.approve')
  const reload = () => { void list.reload(); void counts.reload() }

  useEffect(() => {
    const t = setInterval(() => { void list.reload(); void counts.reload() }, 10_000) // new WhatsApp orders appear without refreshing
    return () => clearInterval(t)
  }, [list.reload, counts.reload])
  useEffect(() => { setPicked(new Set()); setSelecting(false) }, [filter])

  const needle = q.trim().toLowerCase()
  const rows = (list.data ?? []).filter((o) =>
    !needle || [o.number, o.customer?.name, o.customer?.code, o.sender, o.raw_text].some((v) => v?.toLowerCase().includes(needle)))
  const groups = filter === 'open'
    ? GROUPS.map((g) => ({ ...g, rows: rows.filter((o) => g.stages.includes(o.stage ?? '')) })).filter((g) => g.rows.length)
    : [{ title: '', stages: [], tone: '', rows }]
  const deletable = rows.filter((o) => o.status !== 'approved')

  function toggle(id: string) {
    setPicked((cur) => {
      const next = new Set(cur)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })
  }
  async function deletePicked() {
    if (!picked.size) return
    if (!window.confirm(`Delete ${picked.size} item(s) from the desk? Use this for spam, test messages and chats that were never orders. Billed orders are never deleted.`)) return
    const ok = await run(() => ordexa().request('POST', '/api/v1/ai/orders/delete', { ids: [...picked] }))
    if (ok) {
      if (openId && picked.has(openId)) setOpenId(null)
      setPicked(new Set())
      setSelecting(false)
      reload()
    }
  }

  const card = (o: Order) => {
    const who = o.customer?.name ?? o.sender ?? 'Unknown sender'
    const badge = shopBadge(o)
    const sel = picked.has(o.id)
    return (
      <button
        key={o.id}
        className={`order-card tone-${stageOf(o).tone} ${openId === o.id && !selecting ? 'active' : ''} ${sel ? 'picked' : ''}`}
        onClick={() => (selecting ? o.status !== 'approved' && toggle(o.id) : setOpenId(o.id))}
      >
        {selecting ? (
          <span className={`avatar check ${sel ? 'on' : ''}`}>{sel ? '✓' : ''}</span>
        ) : (
          <span className={`avatar ${o.customer ? '' : 'stranger'}`}>{initials(who)}</span>
        )}
        <div className="oc-top">
          <b title={who}>{who}</b>
          <span className={`stage-chip tone-${stageOf(o).tone}`}>{stageOf(o).tone === 'ready' && <i className="pulse" />}{stageOf(o).label}</span>
        </div>
        <div className="oc-text">
          {o.kind === 'image' ? '📷 Photo ' : o.kind === 'audio' ? '🎤 Voice note ' : ''}
          {o.raw_text.startsWith('[') ? '' : o.raw_text}
        </div>
        <div className="oc-meta">
          <span>{o.number}</span>
          <span className="dot-sep">{ago(o.received_at)}</span>
          {o.line_count > 0 && <span className="dot-sep">{o.line_count} item{o.line_count === 1 ? '' : 's'}</span>}
          {o.invoice && <span className="dot-sep">{o.invoice.number}</span>}
          {badge && <span className="shop-badge">{badge}</span>}
        </div>
      </button>
    )
  }

  return (
    <div className="wa">
      <div className="wa-top">
        <div>
          <h1>WhatsApp</h1>
          <div className="page-sub">Orders read by AI and approved by you, and every chat with your shops.</div>
        </div>
        <div className="view-switch" role="tablist">
          <button role="tab" aria-selected={view === 'orders'} className={view === 'orders' ? 'active' : ''} onClick={() => setView('orders')}>
            🧾 Orders {counts.data?.open ? <span className="count">{counts.data.open}</span> : null}
          </button>
          <button role="tab" aria-selected={view === 'chats'} className={view === 'chats' ? 'active' : ''} onClick={() => setView('chats')}>
            💬 Chats
          </button>
        </div>
        {view === 'orders' && can(perms, 'sales_order.create') && (
          <button className="btn primary" onClick={() => setNewOrder(true)}>+ Paste order</button>
        )}
      </div>
      {view === 'chats' ? (
        <Chats perms={perms} onOpenOrder={(id) => { setView('orders'); setOpenId(id) }} />
      ) : (
        <div className="desk">
          <div className="desk-list">
            <div className="desk-list-head">
              <div className="folders" role="tablist">
                {FILTERS.map((f) => {
                  const n = counts.data?.[f.key]
                  const hot = (f.key === 'open' && (counts.data?.ready ?? 0) + (counts.data?.fix ?? 0) > 0) || (f.key === 'messages' && (n ?? 0) > 0)
                  return (
                    <button key={f.key} role="tab" aria-selected={filter === f.key} className={`folder ${filter === f.key ? 'active' : ''}`} onClick={() => setFilter(f.key)} title={f.help}>
                      <span className="folder-icon">{f.icon}</span>
                      <span className="folder-label">{f.label}</span>
                      {n !== undefined && <span className={`count ${hot && filter !== f.key ? 'hot' : ''}`}>{n}</span>}
                    </button>
                  )
                })}
              </div>
              <div className="folder-help">{FILTERS.find((f) => f.key === filter)?.help}</div>
              <div className="list-tools">
                <input placeholder="Search shop, order no. or text" value={q} onChange={(e) => setQ(e.target.value)} />
                {canDelete && deletable.length > 0 && (
                  <button className={`btn ${selecting ? 'primary' : ''}`} onClick={() => { setSelecting(!selecting); setPicked(new Set()) }}>
                    {selecting ? 'Done' : 'Select'}
                  </button>
                )}
              </div>
              {selecting && (
                <div className="select-bar">
                  <span>{picked.size} selected</span>
                  <button className="btn small ghost" onClick={() => setPicked(new Set(deletable.map((o) => o.id)))}>Select all {deletable.length}</button>
                  <button className="btn small danger" disabled={!picked.size || busy} onClick={() => void deletePicked()}>🗑 Delete</button>
                </div>
              )}
              <ErrorBox error={delError} />
            </div>
            <ErrorBox error={list.error} />
            <div className="desk-items">
              {list.data && rows.length === 0 && (
                <div className="empty-state">
                  <div className="big-icon">{filter === 'messages' ? '📭' : '✓'}</div>
                  <div>
                    {needle
                      ? 'Nothing matches your search.'
                      : filter === 'open'
                        ? 'All caught up. New WhatsApp orders appear here.'
                        : filter === 'messages'
                          ? 'No messages waiting. Complaints and questions from shops land here.'
                          : `No ${FILTERS.find((f) => f.key === filter)?.label.toLowerCase()} items.`}
                  </div>
                </div>
              )}
              {groups.map((g) => (
                <div key={g.title || 'all'} style={{ display: 'contents' }}>
                  {g.title && <div className={`desk-group-title tone-${g.tone}`}><i />{g.title}<span>{g.rows.length}</span></div>}
                  {g.rows.map(card)}
                </div>
              ))}
            </div>
          </div>
          <div className="desk-detail">
            {openId ? (
              <OrderDetail key={openId} id={openId} state={state} onChanged={reload} onDeleted={() => { setOpenId(null); reload() }} />
            ) : (
              <div className="empty-state hero-empty">
                <div className="big-icon">🧾</div>
                <b>Select an order</b>
                <div className="small">Check the items the AI read, fix anything highlighted, and approve the bill in one click.</div>
              </div>
            )}
          </div>
        </div>
      )}
      {newOrder && <NewOrderModal onClose={() => setNewOrder(false)} onCreated={(id) => { setNewOrder(false); setOpenId(id); reload() }} />}
    </div>
  )
}

const STEPS: { key: string; label: string }[] = [
  { key: 'received', label: 'Received' },
  { key: 'read', label: 'Read by AI' },
  { key: 'checked', label: 'Checked' },
  { key: 'billed', label: 'Billed' },
]
function stepIndex(status: string): number {
  if (status === 'approved') return 3
  if (status === 'draft_ready') return 2
  if (status === 'needs_review' || status === 'message' || status === 'failed') return 1
  return 0
}
function OrderSteps({ status }: { status: string }) {
  if (['rejected', 'closed', 'deleted'].includes(status)) return null
  const at = stepIndex(status)
  return (
    <ol className="steps-line">
      {STEPS.map((s, i) => (
        <li key={s.key} className={i < at ? 'done' : i === at ? 'now' : ''}>
          <span className="step-dot">{i < at ? '✓' : i + 1}</span>
          {s.label}
        </li>
      ))}
    </ol>
  )
}

function OrderDetail({ id, state, onChanged, onDeleted }: { id: string; state: AppState; onChanged: () => void; onDeleted: () => void }) {
  const perms = state.user?.permissions
  const [order, setOrder] = useState<Order | null>(null)
  const [remember, setRemember] = useState(true)
  const [qtyEdit, setQtyEdit] = useState<Record<number, string>>({})
  const [reject, setReject] = useState<string | null>(null)
  const [adding, setAdding] = useState(false)
  const [showTrace, setShowTrace] = useState(false)
  const [pane, setPane] = useState<'order' | 'chat'>('order')
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
  async function remove() {
    if (!window.confirm('Delete this from the desk? Use it for spam, tests and chats that were never orders.')) return
    const ok = await run(() => ordexa().request('POST', '/api/v1/ai/orders/delete', { ids: [id] }))
    if (ok) onDeleted()
  }
  async function approve(newBill = false) {
    const soft = order!.warnings.some((w) => ['credit_review', 'customer_inactive'].includes(w.code)) || order!.lines!.some((l) => l.issues.includes('insufficient_stock'))
    if (soft && !window.confirm('This order has warnings (credit or stock). Approve and bill anyway?')) return
    await act('/approve', { acknowledge_warnings: soft, new_bill: newBill })
  }

  if (!order) return <div className="muted pad">Loading…</div>
  const editable = !['approved', 'rejected', 'received', 'processing', 'message', 'closed'].includes(order.status)
  const total = (order.lines ?? []).reduce((s, l) => s + (l.line_total ? Number(l.line_total) : 0), 0)

  return (
    <div className="page">
      <div className="card order-head">
        <div>
          <h1>{order.customer ? order.customer.name : 'Customer not linked'}</h1>
          <div className="meta">
            <span>{order.number}</span>
            {order.customer && <span>Shop code {order.customer.code}</span>}
            <span>{order.source === 'whatsapp' ? `WhatsApp ${order.sender ?? ''}` : 'Typed by staff'}</span>
            <span>{new Date(order.received_at).toLocaleString('en-PK')}</span>
          </div>
        </div>
        <div className="head-side">
          <span className={`stage-chip tone-${stageOf(order).tone}`}>{stageOf(order).tone === 'ready' && <i className="pulse" />}{stageOf(order).label}</span>
          {can(perms, 'sales_order.approve') && order.status !== 'approved' && (
            <button className="btn small ghost" title="Delete junk" onClick={() => void remove()}>🗑 Delete</button>
          )}
        </div>
        <OrderSteps status={order.status} />
        {order.source === 'whatsapp' && order.sender && (
          <div className="seg pane-switch">
            <button className={pane === 'order' ? 'active' : ''} onClick={() => setPane('order')}>🧾 Order</button>
            <button className={pane === 'chat' ? 'active' : ''} onClick={() => setPane('chat')}>💬 Chat with shop</button>
          </div>
        )}
      </div>
      {pane === 'chat' && order.sender ? (
        <div className="card chat-card"><ChatThread phone={order.sender} perms={perms} compact /></div>
      ) : (
      <>
      <div className="message-bubble">
        <div className="muted small">Shop wrote</div>
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
      {order.status === 'message' && (
        <div className="card pad">
          <div>The shop sent a message (not an order). Reply to them on WhatsApp or call them, then mark it as handled.</div>
          <button className="btn primary" disabled={busy} onClick={() => void act('/close')}>Mark as handled</button>
        </div>
      )}
      {order.status === 'closed' && order.review_note && <div className="alert ok">{order.review_note}</div>}
      {order.invoice && <div className="alert ok">Billed as {order.invoice.number} · {rs(order.invoice.total)}</div>}
      {order.status === 'rejected' && order.review_note && <div className="alert warn">{order.review_note.startsWith('Cancelled by the shop') ? order.review_note : `Rejected: ${order.review_note}`}</div>}
      {editable && order.add_to_bill && (
        <div className={`alert ${order.add_to_bill.possible ? 'ok' : 'warn'}`}>
          {order.add_to_bill.possible
            ? `The shop asked to add these items to today's bill ${order.add_to_bill.number}. Approving adds them to that bill (same number) and sends the shop the updated bill.`
            : `The shop asked to add these to bill ${order.add_to_bill.number}, but ${order.add_to_bill.reason}. Approving makes a separate bill.`}
        </div>
      )}
      {editable && (
        <div className="approve-bar">
          <span>Total <b>{rs(total.toFixed(2))}</b></span>
          <button className="btn danger" onClick={() => setReject('')}>Reject</button>
          {can(perms, 'sales_order.approve') && order.add_to_bill?.possible && (
            <button className="btn" disabled={busy || order.status !== 'draft_ready'} onClick={() => void approve(true)}>Make a separate bill</button>
          )}
          {can(perms, 'sales_order.approve') ? (
            <button className="btn primary big" disabled={busy || order.status !== 'draft_ready'} onClick={() => void approve()} title={order.status !== 'draft_ready' ? 'Fix the red lines first' : 'Create the bill'}>
              {order.add_to_bill?.possible ? `Add to bill ${order.add_to_bill.number}` : 'Approve & create bill'}
            </button>
          ) : (
            <span className="muted small">An approver must create the bill.</span>
          )}
        </div>
      )}
      </>
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
