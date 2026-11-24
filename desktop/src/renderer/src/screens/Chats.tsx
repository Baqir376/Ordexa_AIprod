// WhatsApp chats: what each shop wrote and what the bot (or staff) replied, with a reply box for staff.

import { useEffect, useRef, useState } from 'react'

import { ordexa } from '../bridge'
import { ErrorBox, can, useAction, useApi } from '../ui'

export interface ChatMessage {
  direction: 'in' | 'out'
  body: string
  status: string
  error: string | null
  created_at: string
  ai_order_id: string | null
  sent_by: 'bot' | 'staff' | null
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
export function ChatThread({ phone, perms, onOpenOrder, compact = false }: {
  phone: string
  perms?: string[]
  onOpenOrder?: (id: string) => void
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
          const pdf = m.body.startsWith('[PDF: ')
          const body = pdf ? m.body.replace(/^\[PDF: [^\]]+\]\s*/, '') : m.body
          return (
            <div key={i} style={{ display: 'contents' }}>
              {sep && <div className="day-sep"><span>{d}</span></div>}
              <div className={`bubble ${m.direction}`}>
                {m.direction === 'out' && <div className={`who ${m.sent_by === 'staff' ? 'staff' : ''}`}>{m.sent_by === 'staff' ? 'Staff' : 'Bot'}</div>}
                {pdf && <div className="pdf-chip">📄 {m.body.match(/^\[PDF: ([^\]]+)\]/)?.[1]}</div>}
                {body}
                <div className="meta">
                  {m.ai_order_id && onOpenOrder && m.direction === 'in' && (
                    <button className="link" onClick={() => onOpenOrder(m.ai_order_id!)}>open order</button>
                  )}
                  <span>{time(m.created_at)}</span>
                  {m.direction === 'out' && <span className={`tick ${m.status}`} title={m.error ?? m.status}>{m.status === 'failed' ? '!' : '✓✓'}</span>}
                </div>
                {m.status === 'failed' && m.error && <div className="bubble-error">{m.error}</div>}
              </div>
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
            <button key={c.phone} className={`conv ${phone === c.phone ? 'active' : ''}`} onClick={() => setPhone(c.phone)}>
              <span className={`avatar ${c.customer_code ? '' : 'stranger'}`}>{initials(c.name ?? c.phone.slice(-2))}</span>
              <span className="conv-main">
                <span className="conv-top">
                  <b>{c.name ?? c.phone}</b>
                  <span className="conv-time">{ago(c.last_at)}</span>
                </span>
                <span className="conv-last">
                  {c.last_direction === 'out' && <span className="muted">You: </span>}
                  {c.last_body.replace(/^\[PDF: [^\]]+\]\s*/, '📄 ')}
                </span>
              </span>
              {c.in_last_24h > 0 && <span className="conv-count" title="messages in the last 24 hours">{c.in_last_24h}</span>}
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
            <ChatThread key={phone} phone={phone} perms={perms} onOpenOrder={onOpenOrder} />
          </>
        ) : (
          <div className="empty-state" style={{ height: '100%' }}>
            <div className="big-icon">💬</div>
            <b>Pick a chat</b>
            <div className="small">See exactly what each shop wrote and what the bot replied.</div>
          </div>
        )}
      </div>
    </div>
  )
}
