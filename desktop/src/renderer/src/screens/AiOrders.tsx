// WhatsApp / AI order desk: every incoming order becomes a draft; a person fixes what the system flags and
// approves it into a bill with one click.

import { useEffect, useState } from 'react'

import type { AppState, CustomerHit, ProductHit } from '../../../core/types'
import { ordexa } from '../bridge'
import { Icon } from '../icons'
import { ChatThread, Chats, ago, initials } from './Chats'
import { ErrorBox, Modal, can, rs, uid, useAction, useApi, useSlider } from '../ui'

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
  voice?: VoiceInfo | null
  received_at: string
  line_count: number
  warnings: { code: string; message: string }[]
  invoice: { id: string; number: string; total: string } | null
  review_note: string | null
  shop_updates?: ShopUpdate[]
  stage?: string
  add_to_bill?: { invoice_id: string; number: string; order: string | null; mode?: string; changes?: string[]; possible: boolean; reason: string | null } | null
  lines?: Line[]
  questions?: string[]
  trace?: Record<string, unknown>[]
  extractor?: string
}

interface VoiceInfo {
  text?: string | null
  alt?: string | null
  language?: string | null
  confidence?: number | null
  clarity: 'clear' | 'fair' | 'unclear' | 'none'
  cleaned?: boolean
  hints?: { heard: string; product: string }[]
  error?: string | null
  edited?: boolean
}

const CLARITY: Record<VoiceInfo['clarity'], [string, string]> = {
  clear: ['Clear', 'ok'],
  fair: ['Some noise', 'fair'],
  unclear: ['Hard to hear', 'bad'],
  none: ['Not heard yet', 'none'],
}
const LANG: Record<string, string> = { ur: 'Urdu', en: 'English', hi: 'Urdu (Hindi letters)', other: 'Mixed' }

/** How the voice note was heard: play it, read it, fix it, or hear it again. */
function VoiceCard({ order, busy, onFix, onListenAgain }: { order: Order; busy: boolean; onFix: (text: string) => void; onListenAgain: () => void }) {
  const v: VoiceInfo = order.voice ?? { clarity: order.transcript ? 'fair' : 'none' }
  const text = v.text ?? order.transcript ?? ''
  const [editing, setEditing] = useState<string | null>(null)
  const [showAlt, setShowAlt] = useState(false)
  const [label, tone] = v.edited ? ['Typed by staff', 'ok'] : CLARITY[v.clarity] ?? CLARITY.fair
  const pct = v.confidence != null ? Math.round(v.confidence * 100) : null
  const reading = order.status === 'received' || order.status === 'processing'
  return (
    <div className={`voice-card clarity-${tone}`}>
      <div className="voice-head">
        <span className="voice-ico"><Icon name="mic" size={16} /></span>
        <b>Voice note</b>
        <span className={`voice-clarity c-${tone}`} title={pct != null ? `Recognition confidence ${pct}%` : undefined}>
          <i className="bars"><i /><i /><i /></i>{label}
        </span>
        {v.language && !v.edited && <span className="voice-chip">{LANG[v.language] ?? v.language}</span>}
        {v.cleaned && !v.edited && <span className="voice-chip"><Icon name="wave" size={12} /> Noise removed</span>}
      </div>
      {order.has_attachment && <Attachment orderId={order.id} kind="audio" />}
      {reading && !text ? (
        <div className="voice-listening"><span className="eq"><i /><i /><i /><i /></span> Listening to the voice note…</div>
      ) : editing !== null ? (
        <div className="voice-edit">
          <textarea dir="auto" value={editing} autoFocus rows={3} onChange={(e) => setEditing(e.target.value)}
            placeholder="Type what the shop said, e.g. 10 dabbe Augmentin aur 5 Risek" />
          <div className="voice-actions">
            <button className="btn primary small" disabled={busy || !editing.trim()} onClick={() => { onFix(editing); setEditing(null) }}>Save and read again</button>
            <button className="btn small" onClick={() => setEditing(null)}>Cancel</button>
          </div>
        </div>
      ) : (
        <>
          {text ? (
            <div className="voice-heard">
              <span className="voice-label">What we heard</span>
              <p dir="auto">{text}</p>
            </div>
          ) : (
            <div className="voice-heard empty">{v.error ? `Could not hear it: ${v.error}` : 'Not heard yet. Play it, or press Listen again.'}</div>
          )}
          {v.alt && (
            <button className="voice-alt-toggle" onClick={() => setShowAlt(!showAlt)}>
              <Icon name="chevron" size={12} /> {showAlt ? 'Hide' : 'Show'} the other hearing
            </button>
          )}
          {v.alt && showAlt && <p className="voice-alt" dir="auto">{v.alt}</p>}
          {(v.hints?.length ?? 0) > 0 && (
            <div className="voice-hints">
              <span className="voice-label">Matched to your products</span>
              <div className="hint-chips">
                {v.hints!.slice(0, 6).map((h, i) => (
                  <span key={i} className="hint-chip"><q dir="auto">{h.heard}</q><Icon name="chevron" size={11} /><b>{h.product}</b></span>
                ))}
              </div>
            </div>
          )}
          {order.status !== 'approved' && order.status !== 'rejected' && (
            <div className="voice-actions">
              <button className="btn small" disabled={busy} onClick={() => setEditing(text)}><Icon name="edit" size={13} /> Fix what was said</button>
              <button className="btn small" disabled={busy} onClick={onListenAgain}><Icon name="refresh" size={13} /> Listen again</button>
            </div>
          )}
        </>
      )}
    </div>
  )
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
  { key: 'open', label: 'To do', icon: 'inbox', help: 'New orders. Approve the green ones, fix the amber ones; blue ones are waiting for the shop to answer the bot.' },
  { key: 'messages', label: 'Messages', icon: 'message', help: 'Complaints and questions from shops that need a person. Reply in Chats, then mark them handled.' },
  { key: 'approved', label: 'Billed', icon: 'checkCircle', help: 'Orders you approved. Their bills were sent to the shop on WhatsApp.' },
  { key: 'rejected', label: 'Rejected', icon: 'xCircle', help: 'Orders you rejected or the shop cancelled.' },
  { key: 'closed', label: 'Bot replied', icon: 'bot', help: 'Messages the bot answered by itself: balance, a copy of the last bill, rates, greetings. Nothing for you to do.' },
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
  const switchRef = useSlider<HTMLDivElement>(view)
  const foldersRef = useSlider<HTMLDivElement>(`${filter}:${view}:${counts.data?.open ?? ''}`)
  const [opened, setOpened] = useState<Set<string>>(() => loadOpened())
  const isNew = (o: Order) => ['ready', 'fix', 'message'].includes(o.stage ?? o.status) && !opened.has(o.id)
  const open = (id: string) => {
    setOpenId(id)
    setOpened((cur) => {
      const next = new Set(cur).add(id)
      saveOpened(next)
      return next
    })
  }
  useEffect(() => markDeskSeen(), [])

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
        onClick={() => (selecting ? o.status !== 'approved' && toggle(o.id) : open(o.id))}
      >
        {selecting ? (
          <span className={`avatar check ${sel ? 'on' : ''}`}>{sel && <Icon name="check" size={16} />}</span>
        ) : (
          <span className={`avatar ${o.customer ? '' : 'stranger'}`}>{initials(who)}</span>
        )}
        <div className="oc-top">
          <b title={who}>{isNew(o) && <span className="new-dot" title="New" />}{who}</b>
          <span className={`stage-chip tone-${stageOf(o).tone}`}><i />{stageOf(o).label}</span>
        </div>
        <div className="oc-text">
          {o.kind === 'image' && <span className="media-tag"><Icon name="image" size={13} /> Photo</span>}{o.kind === 'audio' && <span className="media-tag"><Icon name="mic" size={13} /> Voice note</span>}
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
        <div className="view-switch" role="tablist" ref={switchRef}>
          <span className="slider" aria-hidden="true" />
          <button role="tab" aria-selected={view === 'orders'} className={view === 'orders' ? 'active' : ''} onClick={() => setView('orders')}>
            <Icon name="receipt" size={16} /> Orders
          </button>
          <button role="tab" aria-selected={view === 'chats'} className={view === 'chats' ? 'active' : ''} onClick={() => setView('chats')}>
            <Icon name="message" size={16} /> Chats
          </button>
        </div>
        {view === 'orders' && can(perms, 'sales_order.create') && (
          <button className="btn primary" onClick={() => setNewOrder(true)}>+ Paste order</button>
        )}
      </div>
      {view === 'chats' ? (
        <Chats perms={perms} onOpenOrder={(id) => { setView('orders'); open(id) }} />
      ) : (
        <>
        <div className="kpis">
          {([
            ['ready', 'Ready to approve', 'checkCircle', 'Approve to create the bill', 'open'],
            ['fix', 'Needs your fix', 'alert', 'Pick a product or confirm a quantity', 'open'],
            ['waiting_shop', 'Waiting for shop', 'clock', 'The bot asked the shop a question', 'open'],
            ['messages', 'Messages', 'message', 'Complaints and questions to answer', 'messages'],
          ] as const).map(([k, label, icon, hint, tab]) => (
            <button key={k} className={`kpi kpi-${k} tilt`} onClick={() => setFilter(tab)}>
              <span className="kpi-ico"><Icon name={icon} size={18} /></span>
              <span className="kpi-body">
                <span className="kpi-label">{label}</span>
                <span className="kpi-value">{counts.data?.[k] ?? '–'}</span>
                <span className="kpi-hint">{hint}</span>
              </span>
            </button>
          ))}
        </div>
        <div className="desk">
          <div className="desk-list">
            <div className="desk-list-head">
              <div className="folders" role="tablist" ref={foldersRef}>
                <span className="slider" aria-hidden="true" />
                {FILTERS.map((f) => {
                  const n = counts.data?.[f.key]
                  const hot = (f.key === 'open' && (counts.data?.ready ?? 0) + (counts.data?.fix ?? 0) > 0) || (f.key === 'messages' && (n ?? 0) > 0)
                  return (
                    <button key={f.key} role="tab" aria-selected={filter === f.key} className={`folder ${filter === f.key ? 'active' : ''}`} onClick={() => setFilter(f.key)} title={f.help}>
                      <span className="folder-icon"><Icon name={f.icon} size={16} /></span>
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
                  <button className="btn small danger" disabled={!picked.size || busy} onClick={() => void deletePicked()}><Icon name="trash" size={14} /> Delete</button>
                </div>
              )}
              <ErrorBox error={delError} />
            </div>
            <ErrorBox error={list.error} />
            <div className="desk-items">
              {list.data && rows.length === 0 && (
                <div className="empty-state">
                  <div className="empty-ico"><Icon name={filter === 'messages' ? 'message' : 'checkCircle'} size={26} /></div>
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
                <div className="empty-ico"><Icon name="receipt" size={26} /></div>
                <b>Select an order</b>
                <div className="small">Check the items the AI read, fix anything highlighted, and approve the bill in one click.</div>
              </div>
            )}
          </div>
        </div>
      </>
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
          <span className="step-dot">{i < at ? <Icon name="check" size={12} /> : i + 1}</span>
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

  if (!order) return <div className="od-loading"><span className="spinner" /> Loading order…</div>
  const editable = !['approved', 'rejected', 'received', 'processing', 'message', 'closed'].includes(order.status)
  const lines = order.lines ?? []
  const total = lines.reduce((s, l) => s + (l.line_total ? Number(l.line_total) : 0), 0)
  const fixes = lines.filter((l) => l.issues.some((i) => i !== 'insufficient_stock')).length
  const canApprove = can(perms, 'sales_order.approve')
  const who = order.customer?.name ?? order.sender ?? 'Unknown sender'
  const ask = order.questions?.[0]
  const next =
    order.status === 'received' || order.status === 'processing'
      ? { tone: 'reading', icon: 'sparkle', title: 'The AI is reading this order', text: 'This usually takes a few seconds.' }
      : order.status === 'message'
        ? { tone: 'message', icon: 'message', title: 'This is a message, not an order', text: 'Answer the shop in “Chat with shop”, then mark it as handled.' }
        : order.status === 'approved'
          ? { tone: 'billed', icon: 'checkCircle', title: `Billed as ${order.invoice?.number ?? ''}`, text: 'The shop received the bill on WhatsApp.' }
          : order.status === 'rejected'
            ? { tone: 'muted', icon: 'xCircle', title: order.review_note?.startsWith('Cancelled by the shop') ? 'Cancelled by the shop' : 'Rejected', text: order.review_note ?? '' }
            : order.status === 'closed'
              ? { tone: 'muted', icon: 'bot', title: 'Answered by the bot', text: order.review_note ?? 'Nothing for you to do.' }
              : !order.customer
                ? { tone: 'fix', icon: 'alert', title: 'Link this message to a customer', text: 'Pick the shop below so the bill goes to the right account.' }
                : order.status === 'draft_ready'
                  ? {
                      tone: 'ready',
                      icon: 'checkCircle',
                      title: order.add_to_bill?.possible ? `Ready to update bill ${order.add_to_bill.number}` : 'Everything checks out',
                      text: order.add_to_bill?.possible
                        ? 'Approve to revise that same bill; the shop gets the complete updated bill on WhatsApp.'
                        : `Approve to create the bill for ${rs(total.toFixed(2))} and send it to the shop on WhatsApp.`,
                    }
                  : ask && fixes
                    ? { tone: 'waiting', icon: 'clock', title: 'Waiting for the shop to answer', text: `The bot asked: “${ask.split('\n')[0]}” You can also fix it yourself below.` }
                    : { tone: 'fix', icon: 'alert', title: `${fixes} item${fixes === 1 ? '' : 's'} need${fixes === 1 ? 's' : ''} you`, text: 'Choose the right product or confirm the quantity on the highlighted items.' }
  const resolvedBy = (l: Line) =>
    l.resolution === 'alias' ? 'remembered for this shop' : l.resolution === 'operator' ? 'chosen by staff' : l.resolution === 'customer' ? 'confirmed by the shop' : 'matched by AI'

  return (
    <div className="od">
      <section className="od-hero card">
        <div className="od-who">
          <span className={`od-avatar ${order.customer ? '' : 'stranger'}`}>{initials(who)}</span>
          <div>
            <h1>{who}</h1>
            <div className="od-meta">
              <span className="pill-mini">{order.number}</span>
              {order.customer && <span>Shop {order.customer.code}</span>}
              <span>{order.source === 'whatsapp' ? order.sender : 'Typed by staff'}</span>
              <span>{new Date(order.received_at).toLocaleString('en-PK', { day: 'numeric', month: 'short', hour: 'numeric', minute: '2-digit' })}</span>
            </div>
          </div>
        </div>
        <div className="od-actions">
          {editable && canApprove && (
            <button className="btn primary big" disabled={busy || order.status !== 'draft_ready'} onClick={() => void approve()} title={order.status !== 'draft_ready' ? 'Fix the highlighted items first' : 'Create the bill'}>
              <Icon name="checkCircle" size={17} />
              {order.add_to_bill?.possible ? `Update bill ${order.add_to_bill.number}` : 'Approve & bill'}
            </button>
          )}
          {editable && <button className="btn" onClick={() => setReject('')}>Reject</button>}
          {editable && canApprove && order.add_to_bill?.possible && (
            <button className="btn ghost" disabled={busy || order.status !== 'draft_ready'} onClick={() => void approve(true)}>Separate bill</button>
          )}
          {canApprove && order.status !== 'approved' && (
            <button className="icon-btn" title="Delete (spam, test or not an order)" onClick={() => void remove()}><Icon name="trash" size={16} /></button>
          )}
          {!editable && order.status === 'message' && (
            <button className="btn primary" disabled={busy} onClick={() => void act('/close')}><Icon name="check" size={16} /> Mark as handled</button>
          )}
        </div>
        <OrderSteps status={order.status} />
      </section>

      <div className={`od-next tone-${next.tone}`}>
        <span className="od-next-ico"><Icon name={next.icon} size={18} /></span>
        <div>
          <b>{next.title}</b>
          {next.text && <p>{next.text}</p>}
        </div>
        {editable && <span className="od-total"><small>Total</small>{rs(total.toFixed(2))}</span>}
      </div>
      {flash && <div className="alert ok">The shop just updated this order on WhatsApp.</div>}
      <ErrorBox error={error} />

      {order.source === 'whatsapp' && order.sender && (
        <div className="seg pane-switch">
          <button className={pane === 'order' ? 'active' : ''} onClick={() => setPane('order')}><Icon name="receipt" size={15} /> Order</button>
          <button className={pane === 'chat' ? 'active' : ''} onClick={() => setPane('chat')}><Icon name="message" size={15} /> Chat with shop</button>
        </div>
      )}

      {pane === 'chat' && order.sender ? (
        <div className="card chat-card"><ChatThread phone={order.sender} perms={perms} compact /></div>
      ) : (
        <>
          {editable && order.add_to_bill && (
            <div className={`revision card ${order.add_to_bill.possible ? '' : 'blocked'}`}>
              <div className="revision-head">
                <span className="revision-ico"><Icon name="receipt" size={18} /></span>
                <div>
                  <b>{order.add_to_bill.possible ? `Change to bill ${order.add_to_bill.number}` : `Bill ${order.add_to_bill.number} can no longer change`}</b>
                  <div className="muted small">
                    {order.add_to_bill.possible
                      ? 'The shop asked to change a bill from the last business day. Approving revises that same bill.'
                      : `${order.add_to_bill.reason}. Approving makes a separate bill.`}
                  </div>
                </div>
              </div>
              {order.add_to_bill.possible && (order.add_to_bill.changes?.length ?? 0) > 0 && (
                <ul className="revision-list">{order.add_to_bill.changes!.map((c, i) => <li key={i}>{c.replace(' -> ', ' → ')}</li>)}</ul>
              )}
            </div>
          )}

          <section className="od-msg card">
            <div className="od-sec-title"><Icon name="message" size={15} /> What the shop sent</div>
            <div className="od-quote">
              {order.raw_text.startsWith('[') && order.kind !== 'text' ? null : order.raw_text}
              {order.kind === 'audio' ? (
                <VoiceCard order={order} busy={busy} onFix={(t) => void act('/transcript', { text: t })} onListenAgain={() => void act('/listen-again')} />
              ) : (
                order.has_attachment && <Attachment orderId={order.id} kind={order.kind ?? 'text'} />
              )}
            </div>
            {(order.shop_updates?.length ?? 0) > 0 && (
              <ul className="od-updates">
                {order.shop_updates!.map((u, i) => (
                  <li key={i} className={`upd upd-${u.kind}`}>
                    <i />
                    <span><b>{UPDATE_LABEL[u.kind]}</b>{u.message && <> · “{u.message}”</>}{u.changes.length > 0 && <small> {u.changes.join(' ')}</small>}</span>
                    <time>{u.at ? new Date(u.at).toLocaleTimeString('en-PK', { hour: 'numeric', minute: '2-digit' }) : ''}</time>
                  </li>
                ))}
              </ul>
            )}
          </section>

          {order.warnings.map((w) => <div key={w.code} className={`alert ${w.code === 'contains_instructions' ? 'error' : 'warn'}`}>{w.message}</div>)}

          {order.status === 'received' || order.status === 'processing' ? (
            <div className="od-reading card"><span className="spinner" /> Reading the items…</div>
          ) : lines.length > 0 || editable ? (
            <section className="od-items card">
              <div className="od-items-head">
                <div className="od-sec-title"><Icon name="products" size={15} /> Items the AI read <span className="count-pill">{lines.length}</span></div>
                {editable && <button className="btn small" onClick={() => setAdding(true)}><Icon name="plus" size={14} /> Add item</button>}
              </div>
              <div className="items">
                {lines.map((l) => {
                  const blocking = l.issues.filter((i) => i !== 'insufficient_stock')
                  const low = l.issues.includes('insufficient_stock')
                  return (
                    <article key={l.line_no} className={`item ${blocking.length ? 'needs' : 'ok'}`}>
                      <span className="item-no">{blocking.length ? <Icon name="alert" size={14} /> : <Icon name="check" size={14} />}</span>
                      <div className="item-main">
                        <div className="item-name">{l.product_name ?? <span className="muted">Not matched yet</span>}</div>
                        <div className="item-sub">
                          Shop wrote <q>{l.phrase}</q>{l.read_as && <> (read as “{l.read_as}”)</>}
                          {l.product_name && <> · {resolvedBy(l)}</>}
                        </div>
                        {blocking.map((i) => <div key={i} className="item-issue">{ISSUE_TEXT[i] ?? i}</div>)}
                        {editable && (l.issues.includes('ambiguous') || l.issues.includes('not_found')) && (
                          <div className="cand-row">
                            {l.candidates.map((c) => (
                              <button key={c.product_id} className="choice" disabled={busy} onClick={() => void act('/resolve-line', { line_no: l.line_no, product_id: c.product_id, remember })}>
                                {c.name}
                              </button>
                            ))}
                            <ProductPicker onPick={(p) => void act('/resolve-line', { line_no: l.line_no, product_id: p.id, remember })} />
                          </div>
                        )}
                        {editable && (l.issues.includes('missing_quantity') || l.issues.includes('unit_mismatch')) && (
                          <span className="qty-fix">
                            <input className="num-in" placeholder={`qty in ${l.sale_unit ?? 'units'}`} value={qtyEdit[l.line_no] ?? ''} onChange={(e) => setQtyEdit({ ...qtyEdit, [l.line_no]: e.target.value })} />
                            <button className="btn small primary" disabled={!qtyEdit[l.line_no] || busy} onClick={() => void act('/resolve-line', { line_no: l.line_no, quantity: qtyEdit[l.line_no] })}>Set</button>
                          </span>
                        )}
                      </div>
                      <div className="item-fig">
                        <div className="item-qty">{l.quantity ?? '?'} {l.sale_unit ?? l.unit ?? ''}{l.unit_price && <> × {rs(l.unit_price)}</>}</div>
                        <div className="item-amt">{rs(l.line_total)}</div>
                        {l.available && <span className={`stock-chip ${low ? 'low' : ''}`}>{low ? 'Low stock' : 'In stock'} · {Number(l.available)}</span>}
                      </div>
                      {editable && <button className="icon-btn subtle" title="Remove this item" onClick={() => void act(`/lines/${l.line_no}/remove`)}><Icon name="xCircle" size={16} /></button>}
                    </article>
                  )
                })}
                {lines.length === 0 && <div className="muted small">No items were read. Add them with “Add item”.</div>}
              </div>
              <div className="od-items-foot">
                {editable ? (
                  <label className="check"><input type="checkbox" checked={remember} onChange={(e) => setRemember(e.target.checked)} disabled={!can(perms, 'ai_alias.approve')} /> Remember my product choices for this shop</label>
                ) : <span />}
                <span className="od-sum">Total <b>{rs(total.toFixed(2))}</b></span>
              </div>
              {editable && !order.customer && <div className="od-link"><CustomerLinker onPick={(c) => void act('/customer', { customer_id: c.id })} /></div>}
            </section>
          ) : null}

          {order.questions && order.questions.length > 0 && editable && (
            <section className="card od-asked">
              <div className="od-sec-title"><Icon name="send" size={15} /> Asked the shop on WhatsApp</div>
              {order.questions.map((q, i) => <p key={i}>{q}</p>)}
            </section>
          )}
          {order.invoice && <div className="alert ok">Billed as {order.invoice.number} · {rs(order.invoice.total)}</div>}
        </>
      )}
      <button className="btn ghost small od-trace" onClick={() => setShowTrace(!showTrace)}>{showTrace ? 'Hide' : 'Show'} how the AI read this ({order.extractor})</button>
      {showTrace && <pre className="trace">{JSON.stringify(order.trace, null, 2)}</pre>}
      {reject !== null && (
        <Modal title="Reject order" onClose={() => setReject(null)}>
          <div className="form">
            <input autoFocus value={reject} onChange={(e) => setReject(e.target.value)} placeholder="Reason (sent to the shop, e.g. out of stock)" />
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


// "New" = arrived since this person last opened the WhatsApp desk, and not opened yet. Kept on this PC.
const OPENED_KEY = 'ordexa.desk.opened'
const SEEN_KEY = 'ordexa.desk.seenAt'
function loadOpened(): Set<string> {
  try {
    return new Set(JSON.parse(localStorage.getItem(OPENED_KEY) ?? '[]') as string[])
  } catch {
    return new Set()
  }
}
function saveOpened(ids: Set<string>) {
  try {
    localStorage.setItem(OPENED_KEY, JSON.stringify([...ids].slice(-800)))
  } catch {
    /* storage full or blocked: "new" markers just reappear */
  }
}
export function deskSeenAt(): string | null {
  try {
    return localStorage.getItem(SEEN_KEY)
  } catch {
    return null
  }
}
export function markDeskSeen() {
  try {
    localStorage.setItem(SEEN_KEY, new Date().toISOString())
    window.dispatchEvent(new Event('ordexa:desk-seen'))
  } catch {
    /* ignore */
  }
}
