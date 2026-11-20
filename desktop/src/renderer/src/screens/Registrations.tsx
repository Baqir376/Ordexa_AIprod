// WhatsApp sign-ups (human in the loop) and a simulator to try the bot without a Meta account.

import { useState } from 'react'

import type { CustomerHit } from '../../../core/types'
import { ordexa } from '../bridge'
import { ErrorBox, Field, Modal, Page, Table, useAction, useApi } from '../ui'

interface Contact {
  id: string
  phone: string
  state: string
  customer: { id: string; code: string; name: string } | null
  claimed_customer: { id: string; code: string; name: string; phone_on_file: string | null } | null
  registration_details: string | null
  last_message_at: string | null
}
interface Msg { direction: 'in' | 'out'; body: string; status: string; created_at: string }

const STATE: Record<string, string> = {
  new: 'New', awaiting_choice: 'In menu', awaiting_code: 'Entering shop code', awaiting_otp: 'Waiting for code',
  awaiting_details: 'Writing details', pending_approval: 'Needs approval', verified: 'Can order', blocked: 'Blocked',
}

export function Registrations() {
  const [view, setView] = useState<'pending' | 'all' | 'simulator'>('pending')
  const list = useApi<Contact[]>(view === 'simulator' ? null : `/api/v1/channels/whatsapp/contacts${view === 'pending' ? '?state=pending' : ''}`, [view])
  const [approving, setApproving] = useState<Contact | null>(null)
  const { error, run } = useAction()
  async function act(c: Contact, path: string, confirmText: string) {
    if (!window.confirm(confirmText)) return
    await run(() => ordexa().request('POST', `/api/v1/channels/whatsapp/contacts/${c.id}/${path}`))
    void list.reload()
  }
  return (
    <Page title="WhatsApp sign-ups">
      <div className="subtabs">
        <button className={view === 'pending' ? 'active' : ''} onClick={() => setView('pending')}>Waiting for approval</button>
        <button className={view === 'all' ? 'active' : ''} onClick={() => setView('all')}>All numbers</button>
        <button className={view === 'simulator' ? 'active' : ''} onClick={() => setView('simulator')}>Test chat (simulator)</button>
      </div>
      <ErrorBox error={list.error ?? error} />
      {view === 'simulator' ? (
        <Simulator />
      ) : (
        <Table
          rows={list.data}
          empty={view === 'pending' ? 'No numbers waiting. New shops that message your WhatsApp number appear here.' : 'No WhatsApp contacts yet.'}
          columns={[
            { key: 'phone', label: 'Number' },
            { key: 'state', label: 'Status', render: (c) => <span className={`tag ${c.state === 'verified' ? 'synced' : c.state === 'blocked' ? 'rejected' : 'pending'}`}>{STATE[c.state] ?? c.state}</span> },
            { key: 'who', label: 'Shop', render: (c) => c.customer ? `${c.customer.code} ${c.customer.name}` : c.claimed_customer ? `claims ${c.claimed_customer.code} ${c.claimed_customer.name} (on file: ${c.claimed_customer.phone_on_file ?? 'no phone'})` : c.registration_details ?? '' },
            { key: 'last', label: 'Last message', render: (c) => (c.last_message_at ? new Date(c.last_message_at).toLocaleString('en-PK') : '') },
            { key: 'a', label: '', render: (c) => (
              <span className="row-actions">
                {c.state !== 'verified' && c.state !== 'blocked' && <button className="btn small primary" onClick={() => setApproving(c)}>Approve</button>}
                {c.state === 'verified' && <button className="btn small" onClick={() => void act(c, 'unlink', `Stop ${c.phone} from ordering for ${c.customer?.name}?`)}>Unlink</button>}
                {c.state !== 'blocked' && <button className="btn small danger" onClick={() => void act(c, 'block', `Block ${c.phone}? Messages from it will be ignored.`)}>Block</button>}
              </span>
            ) },
          ]}
        />
      )}
      {approving && <ApproveModal contact={approving} onClose={() => setApproving(null)} onDone={() => { setApproving(null); void list.reload() }} />}
    </Page>
  )
}

function ApproveModal({ contact, onClose, onDone }: { contact: Contact; onClose: () => void; onDone: () => void }) {
  const [mode, setMode] = useState<'existing' | 'new'>(contact.claimed_customer ? 'existing' : 'new')
  const [picked, setPicked] = useState<{ id: string; code: string; name: string } | null>(contact.claimed_customer)
  const [q, setQ] = useState('')
  const [hits, setHits] = useState<CustomerHit[]>([])
  const [nc, setNc] = useState({ code: '', name: contact.registration_details?.split(',')[0] ?? '', address: '' })
  const { busy, error, run } = useAction()
  async function save() {
    const r = await run(() => ordexa().request('POST', `/api/v1/channels/whatsapp/contacts/${contact.id}/approve`,
      mode === 'existing' ? { customer_id: picked!.id } : { new_customer: { ...nc, address: nc.address || null } }))
    if (r) onDone()
  }
  return (
    <Modal title={`Approve ${contact.phone}`} onClose={onClose}>
      <div className="form">
        {contact.registration_details && <div className="message-bubble">{contact.registration_details}</div>}
        <p className="muted small">Call the shop to confirm before approving. Once approved, this number can place orders on the shop's account.</p>
        <div className="seg">
          <button className={mode === 'existing' ? 'active' : ''} onClick={() => setMode('existing')}>Existing customer</button>
          <button className={mode === 'new' ? 'active' : ''} onClick={() => setMode('new')}>New customer</button>
        </div>
        {mode === 'existing' ? (
          picked ? <div className="chip"><b>{picked.code}</b> {picked.name}<button className="x" onClick={() => setPicked(null)}>×</button></div> : (
            <div className="combo">
              <input autoFocus value={q} placeholder="Search customer" onChange={async (e) => { setQ(e.target.value); setHits(e.target.value ? await ordexa().searchCustomers(e.target.value) : []) }} />
              {hits.length > 0 && <ul className="dropdown">{hits.map((c) => <li key={c.id} onMouseDown={() => { setPicked(c); setHits([]) }}><b>{c.code}</b> {c.name}</li>)}</ul>}
            </div>
          )
        ) : (
          <>
            <div className="row-2">
              <Field label="New shop code"><input value={nc.code} onChange={(e) => setNc({ ...nc, code: e.target.value })} autoFocus /></Field>
              <Field label="Shop name"><input value={nc.name} onChange={(e) => setNc({ ...nc, name: e.target.value })} /></Field>
            </div>
            <Field label="Address / area"><input value={nc.address} onChange={(e) => setNc({ ...nc, address: e.target.value })} /></Field>
          </>
        )}
        <ErrorBox error={error} />
        <div className="modal-actions">
          <button className="btn" onClick={onClose}>Cancel</button>
          <button className="btn primary" disabled={busy || (mode === 'existing' ? !picked : !nc.code || !nc.name)} onClick={() => void save()}>Approve number</button>
        </div>
      </div>
    </Modal>
  )
}

function Simulator() {
  const [phone, setPhone] = useState('+923001234567')
  const [text, setText] = useState('')
  const [msgs, setMsgs] = useState<Msg[]>([])
  const { busy, error, run } = useAction()
  async function load(p = phone) {
    setMsgs(await ordexa().request<Msg[]>('GET', `/api/v1/channels/whatsapp/messages?phone=${encodeURIComponent(p)}`))
  }
  async function send() {
    const r = await run(() => ordexa().request('POST', '/api/v1/channels/whatsapp/simulate', { phone, text }))
    if (r) { setText(''); await load() }
  }
  return (
    <div className="sim card pad">
      <p className="muted small">Pretend to be a shop messaging your WhatsApp number. Uses the real sign-up bot and AI order reading; replies are recorded instead of sent.</p>
      <div className="inline-form">
        <Field label="Shop's phone"><input value={phone} onChange={(e) => setPhone(e.target.value)} onBlur={() => void load()} /></Field>
        <button className="btn" onClick={() => void load()}>Load chat</button>
      </div>
      <div className="chat">
        {msgs.length === 0 && <div className="muted small">No messages yet. Send "hi" to start.</div>}
        {msgs.map((m, i) => <div key={i} className={`bubble ${m.direction}`}>{m.body}<span className="muted small"> {new Date(m.created_at).toLocaleTimeString('en-PK')}{m.status === 'failed' ? ' · failed' : ''}</span></div>)}
      </div>
      <div className="inline-form">
        <input value={text} onChange={(e) => setText(e.target.value)} onKeyDown={(e) => e.key === 'Enter' && text && void send()} placeholder="Type as the shop… e.g. hi, 1, 101, 10 box panadol" />
        <button className="btn primary" disabled={busy || !text} onClick={() => void send()}>Send</button>
      </div>
      <ErrorBox error={error} />
    </div>
  )
}
