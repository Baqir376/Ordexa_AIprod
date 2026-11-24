// WhatsApp chats: what each shop wrote and what the bot (or staff) replied, with a reply box for staff.

import { useEffect, useRef, useState } from 'react'

import { ordexa } from '../bridge'
import { Icon } from '../icons'
import { ErrorBox, can, useAction, useApi } from '../ui'

export interface ChatMessage {
  direction: 'in' | 'out'
  body: string
  status: string
  error: string | null
  created_at: string
  ai_order_id: string | null
  sent_by: 'bot' | 'staff' | null
  media: { kind: 'pdf' | 'image' | 'audio'; path: string; name: string } | null
  order: { id: string; number: string; status: string } | null
}

const ORDER_STATE: Record<string, string> = {
  received: 'AI reading', processing: 'AI reading', needs_review: 'Needs attention', draft_ready: 'Ready to approve',
  approved: 'Billed', rejected: 'Rejected', message: 'Message', failed: 'Needs attention',
}

/** A photo or voice note from the shop, or a bill PDF we sent: shown and played right in the chat. */
function MediaView({ media }: { media: NonNullable<ChatMessage['media']> }) {
  const [url, setUrl] = useState<string | null>(null)
  const [err, setErr] = useState<string | null>(null)
  const [zoom, setZoom] = useState(false)
  const [opening, setOpening] = useState(false)
  useEffect(() => {
    if (media.kind === 'pdf') return
    let alive = true
    ordexa().dataUrl(media.path).then((u) => alive && setUrl(u), (e: Error) => alive && setErr(e.message))
    return () => { alive = false }
  }, [media.path, media.kind])
  if (media.kind === 'pdf') {
    return (
      <button
        className="pdf-card"
        disabled={opening}
        onClick={() => {
          setOpening(true)
          void ordexa().openFile(media.path, media.name).catch((e: Error) => setErr(e.message)).finally(() => setOpening(false))
        }}
        title="Open the bill"
      >
        <span className="pdf-icon">PDF</span>
        <span className="pdf-name">{media.name}<small>{opening ? 'Opening…' : 'Click to open'}</small></span>
        {err && <span className="bubble-error">{err}</span>}
      </button>
    )
  }
  if (err) return <div className="bubble-error">{err}</div>
  if (!url) return <div className="media-loading"><Icon name={media.kind === 'audio' ? 'mic' : 'image'} size={14} /> Loading {media.kind === 'audio' ? 'voice note' : 'photo'}…</div>
  if (media.kind === 'audio') return <audio className="chat-audio" controls src={url} />
  return (
    <>
      <img className="chat-photo" src={url} alt="Photo sent by the shop" onClick={() => setZoom(true)} />
      {zoom && (
        <div className="modal-backdrop" onMouseDown={() => setZoom(false)}>
          <img className="attachment-zoom" src={url} alt="Photo, enlarged" />
        </div>
      )}
    </>
  )
}
interface Conversation {
  phone: string
  name: string | null
  customer_code: string | null
  state: string | null
  last_body: string
  last_direction: 'in' | 'out'
  last_at: string
  in_last_24h: number
  unread: number
}

export function initials(name: string): string {
  const words = name.replace(/[^\p{L}\p{N} ]/gu, ' ').trim().split(/\s+/).filter(Boolean)
  if (!words.length) return '#'
  return (words[0][0] + (words[1]?.[0] ?? '')).toUpperCase()
}

export function ago(iso: string): string {
  const mins = Math.max(0, Math.round((Date.now() - new Date(iso).getTime()) / 60_000))
  if (mins < 1) return 'just now'
  if (mins < 60) return `${mins} min ago`
  const h = Math.round(mins / 60)
  if (h < 24) return `${h} h ago`
  return new Date(iso).toLocaleDateString('en-PK', { day: 'numeric', month: 'short' })
}

const time = (iso: string) => new Date(iso).toLocaleTimeString('en-PK', { hour: 'numeric', minute: '2-digit' })
const day = (iso: string) => new Date(iso).toLocaleDateString('en-PK', { weekday: 'short', day: 'numeric', month: 'short' })

/** One chat thread, WhatsApp style. Refreshes itself; optional reply box for staff. */
export function ChatThread({ phone, perms, onOpenOrder, onRead, compact = false }: {
  phone: string
  perms?: string[]
  onOpenOrder?: (id: string) => void
  onRead?: () => void
  compact?: boolean
}) {
  const msgs = useApi<ChatMessage[]>(`/api/v1/channels/whatsapp/messages?phone=${encodeURIComponent(phone)}`, [phone])
  const [text, setText] = useState('')
  const { busy, error, run } = useAction()
  const end = useRef<HTMLDivElement>(null)
  const count = msgs.data?.length ?? 0

  useEffect(() => {
    const t = setInterval(() => void msgs.reload(), 5000)
    return () => clearInterval(t)
  }, [msgs.reload])
  useEffect(() => {
    if (count > 0) void ordexa().request('POST', '/api/v1/channels/whatsapp/conversations/read', { phone }).then(onRead, () => undefined)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [count, phone])
  useEffect(() => {
    // braces matter: newer Chromium returns a Promise from scrollIntoView, and React would call it as a cleanup
    end.current?.scrollIntoView({ block: 'end' })
  }, [count])

  async function send() {
    const body = text.trim()
    if (!body) return
    const ok = await run(() => ordexa().request('POST', '/api/v1/channels/whatsapp/reply', { phone, text: body }))
    if (ok) {
      setText('')
      void msgs.reload()
    }
  }

  let lastDay = ''
  return (
    <div className={`thread ${compact ? 'compact' : ''}`}>
      <div className="chat wallpaper">
        {msgs.data?.length === 0 && <div className="chat-empty">No messages yet.</div>}
        {msgs.data?.map((m, i) => {
          const d = day(m.created_at)
          const sep = d !== lastDay
          lastDay = d
          const body = m.body.replace(/^\[PDF: [^\]]+\]\s*/, '').replace(/^\[(image|audio)\]\s*/, '')
          return (
            <div key={i} style={{ display: 'contents' }}>
              {sep && <div className="day-sep"><span>{d}</span></div>}
              <div className={`bubble ${m.direction}`}>
                {m.direction === 'out' && <div className={`who ${m.sent_by === 'staff' ? 'staff' : ''}`}>{m.sent_by === 'staff' ? 'Staff' : 'Bot'}</div>}
                {m.media && <MediaView media={m.media} />}
                {body && <div className="bubble-text">{body}</div>}
                <div className="meta">
                  <span>{time(m.created_at)}</span>
                  {m.direction === 'out' && <span className={`tick ${m.status}`} title={m.error ?? m.status}>{m.status === 'failed' ? '!' : <><Icon name="check" size={13} /><Icon name="check" size={13} /></>}</span>}
                </div>
                {m.status === 'failed' && m.error && <div className="bubble-error">{m.error}</div>}
              </div>
              {m.order && onOpenOrder && (
                <button className="order-chip" onClick={() => onOpenOrder(m.order!.id)}>
                  <span className="oc-ico"><Icon name="receipt" size={14} /></span>
                  <span><b>{m.order.number}</b> · {ORDER_STATE[m.order.status] ?? m.order.status}</span>
                  <span className="oc-go">Open ›</span>
                </button>
              )}
            </div>
          )
        })}
        <div ref={end} />
      </div>
      {can(perms, 'sales_order.create') && (
        <div className="composer">
          <input
            value={text}
            onChange={(e) => setText(e.target.value)}
            onKeyDown={(e) => e.key === 'Enter' && !e.shiftKey && (e.preventDefault(), void send())}
            placeholder="Reply to the shop as staff…"
          />
          <button className="btn primary" disabled={busy || !text.trim()} onClick={() => void send()}>Send</button>
        </div>
      )}
      <ErrorBox error={error} />
    </div>
  )
}

/** All chats: list on the left, the conversation on the right. */
export function Chats({ perms, onOpenOrder }: { perms?: string[]; onOpenOrder: (id: string) => void }) {
  const [q, setQ] = useState('')
  const list = useApi<Conversation[]>(`/api/v1/channels/whatsapp/conversations${q ? `?q=${encodeURIComponent(q)}` : ''}`, [q])
  const [phone, setPhone] = useState<string | null>(null)
  useEffect(() => {
    const t = setInterval(() => void list.reload(), 8000)
    return () => clearInterval(t)
  }, [list.reload])
  const current = list.data?.find((c) => c.phone === phone)

  return (
    <div className="chats">
      <div className="chat-list card">
        <div className="chat-list-head">
          <input placeholder="Search chats" value={q} onChange={(e) => setQ(e.target.value)} />
        </div>
        <div className="chat-list-items">
          {list.data?.length === 0 && <div className="chat-empty">No chats yet.</div>}
          {list.data?.map((c) => (
            <button key={c.phone} className={`conv ${phone === c.phone ? 'active' : ''} ${c.unread && phone !== c.phone ? 'unread' : ''}`} onClick={() => setPhone(c.phone)}>
              <span className={`avatar ${c.customer_code ? '' : 'stranger'}`}>{initials(c.name ?? c.phone.slice(-2))}</span>
              <span className="conv-main">
                <span className="conv-top">
                  <b>{c.name ?? c.phone}</b>
                  <span className="conv-time">{ago(c.last_at)}</span>
                </span>
                <span className="conv-last">
                  {c.last_direction === 'out' && <span className="muted">You: </span>}
                  {c.last_body.replace(/^\[PDF: [^\]]+\]\s*/, 'Bill PDF · ')}
                </span>
              </span>
              {c.unread > 0 && phone !== c.phone && <span className="conv-count" title="unread messages">{c.unread}</span>}
            </button>
          ))}
        </div>
      </div>
      <div className="chat-pane card">
        {phone ? (
          <>
            <div className="chat-head">
              <span className={`avatar ${current?.customer_code ? '' : 'stranger'}`}>{initials(current?.name ?? phone.slice(-2))}</span>
              <div>
                <b>{current?.name ?? phone}</b>
                <div className="muted small">
                  {phone}
                  {current?.customer_code && ` · shop ${current.customer_code}`}
                  {current?.state && current.state !== 'verified' && ` · ${current.state.replace(/_/g, ' ')}`}
                </div>
              </div>
            </div>
            <ChatThread key={phone} phone={phone} perms={perms} onOpenOrder={onOpenOrder} onRead={() => void list.reload()} />
          </>
        ) : (
          <div className="empty-state" style={{ height: '100%' }}>
            <div className="empty-ico"><Icon name="message" size={26} /></div>
            <b>Pick a chat</b>
            <div className="small">See exactly what each shop wrote and what the bot replied.</div>
          </div>
        )}
      </div>
    </div>
  )
}
