import { useState } from 'react'

import type { AppState } from '../../../core/types'
import { ordexa } from '../bridge'
import { Chips, ErrorBox, Field, Figures, Modal, Monogram, Page, Table, can, qtyText, rs, uid, useAction, useApi } from '../ui'

interface Product {
  id: string
  code: string
  name: string
  generic_name: string | null
  manufacturer: string | null
  pack_size: string | null
  sale_unit: string
  sale_price: string
  barcode: string | null
  track_batches: boolean
  reorder_level: string | null
  is_active: boolean
}
interface StockRow { product_id: string; batch_id: string | null; batch_no: string | null; expiry_date: string | null; on_hand: string; cost_price: string | null; value: string | null }

type StockFilter = 'all' | 'low' | 'out' | 'inactive'

export function Products({ state }: { state: AppState }) {
  const perms = state.user?.permissions
  const [q, setQ] = useState('')
  const [filter, setFilter] = useState<StockFilter>('all')
  const list = useApi<Product[]>(`/api/v1/products?q=${encodeURIComponent(q)}`, [q])
  const stock = useApi<StockRow[]>('/api/v1/reports/stock')
  const [editing, setEditing] = useState<Partial<Product> | null>(null)
  const [detail, setDetail] = useState<Product | null>(null)
  const totals = new Map<string, number>()
  let value = 0
  for (const s of stock.data ?? []) {
    totals.set(s.product_id, (totals.get(s.product_id) ?? 0) + Number(s.on_hand))
    value += Number(s.value ?? 0)
  }
  const all = list.data ?? []
  const onHand = (p: Product) => totals.get(p.id) ?? 0
  const isOut = (p: Product) => p.is_active && onHand(p) <= 0
  const isLow = (p: Product) => p.is_active && !isOut(p) && p.reorder_level !== null && onHand(p) <= Number(p.reorder_level)
  const counts = { low: all.filter(isLow).length, out: all.filter(isOut).length, inactive: all.filter((p) => !p.is_active).length }
  const rows = list.data
    ? all.filter((p) => (filter === 'low' ? isLow(p) : filter === 'out' ? isOut(p) : filter === 'inactive' ? !p.is_active : true))
    : null

  return (
    <Page
      title="Products & stock"
      actions={
        <>
          <input className="search-small" placeholder="Search name, code, barcode" value={q} onChange={(e) => setQ(e.target.value)} />
          {can(perms, 'product.manage') && <button className="btn primary" onClick={() => setEditing({ track_batches: true, sale_unit: 'box' })}>+ New product</button>}
        </>
      }
    >
      <Figures
        items={[
          { label: 'Products', value: all.length.toLocaleString('en-PK'), icon: 'products', hue: 'indigo', hint: `${all.length - counts.inactive} active`, onClick: () => setFilter('all'), active: filter === 'all' },
          { label: 'Stock value (at cost)', value: rs(value.toFixed(2)), icon: 'expenses', hue: 'emerald', hint: 'All batches on hand' },
          { label: 'Low stock', value: counts.low, icon: 'alert', hue: 'amber', hint: 'At or below reorder level', onClick: () => setFilter('low'), active: filter === 'low' },
          { label: 'Out of stock', value: counts.out, icon: 'xCircle', hue: 'rose', hint: 'Nothing left to sell', onClick: () => setFilter('out'), active: filter === 'out' },
        ]}
      />
      <Chips
        value={filter}
        onChange={setFilter}
        options={[['all', 'All products', all.length], ['low', 'Low stock', counts.low], ['out', 'Out of stock', counts.out], ['inactive', 'Inactive', counts.inactive]]}
      />
      <ErrorBox error={list.error ?? stock.error} />
      <Table
        rows={rows}
        onRow={(p) => setDetail(p)}
        empty={filter === 'all' ? 'No products yet.' : 'Nothing here. Good news.'}
        columns={[
          {
            key: 'name',
            label: 'Product',
            render: (p) => (
              <span className="cell-main">
                <Monogram text={p.name} />
                <span>
                  <b>{p.name}</b>
                  <span className="cell-sub">{[p.code, p.generic_name, p.manufacturer, p.pack_size].filter(Boolean).join(' · ')}</span>
                </span>
              </span>
            ),
          },
          { key: 'sale_price', label: 'Price', num: true, render: (p) => <span className="price-tag">{rs(p.sale_price)}<small>/ {p.sale_unit}</small></span> },
          {
            key: 'stock',
            label: 'Stock',
            render: (p) => {
              const t = onHand(p)
              const re = Number(p.reorder_level ?? 0)
              const pct = Math.min(100, (t / Math.max(1, re * 4)) * 100)
              const tone = isOut(p) ? 'out' : isLow(p) ? 'low' : 'ok'
              return (
                <span className={`meter-cell tone-${tone}`}>
                  <span className="meter"><i style={{ width: `${tone === 'out' ? 0 : Math.max(4, pct)}%` }} /></span>
                  <span className="meter-num">{t.toLocaleString('en-PK')} <small>{p.sale_unit}</small></span>
                </span>
              )
            },
          },
          { key: 'reorder_level', label: 'Reorder at', num: true, render: (p) => qtyText(p.reorder_level) },
          {
            key: 'status',
            label: 'Status',
            render: (p) =>
              !p.is_active ? <span className="status s-muted">Inactive</span>
                : isOut(p) ? <span className="status s-bad">Out of stock</span>
                : isLow(p) ? <span className="status s-warn">Reorder</span>
                : <span className="status s-ok">In stock</span>,
          },
        ]}
      />
      {editing && <ProductForm initial={editing} onClose={() => setEditing(null)} onSaved={() => { setEditing(null); void list.reload() }} />}
      {detail && (
        <ProductDetail
          product={detail}
          stock={(stock.data ?? []).filter((s) => s.product_id === detail.id)}
          state={state}
          onEdit={() => { setEditing(detail); setDetail(null) }}
          onClose={() => setDetail(null)}
          onChanged={() => void stock.reload()}
        />
      )}
    </Page>
  )
}

function ProductForm({ initial, onClose, onSaved }: { initial: Partial<Product>; onClose: () => void; onSaved: () => void }) {
  const [f, setF] = useState({
    code: initial.code ?? '', name: initial.name ?? '', generic_name: initial.generic_name ?? '', manufacturer: initial.manufacturer ?? '',
    pack_size: initial.pack_size ?? '', sale_unit: initial.sale_unit ?? 'box', sale_price: initial.sale_price ?? '', barcode: initial.barcode ?? '',
    reorder_level: initial.reorder_level ?? '', track_batches: initial.track_batches ?? true, is_active: initial.is_active ?? true,
  })
  const { busy, error, run } = useAction()
  const set = (k: keyof typeof f) => (e: { target: { value: string } }) => setF({ ...f, [k]: e.target.value })
  async function save() {
    const body = {
      name: f.name, generic_name: f.generic_name || null, manufacturer: f.manufacturer || null, pack_size: f.pack_size || null,
      sale_unit: f.sale_unit, sale_price: f.sale_price, barcode: f.barcode || null, reorder_level: f.reorder_level === '' ? null : f.reorder_level,
    }
    const ok = await run(() =>
      initial.id
        ? ordexa().request('PUT', `/api/v1/products/${initial.id}`, { ...body, is_active: f.is_active })
        : ordexa().request('POST', '/api/v1/products', { ...body, code: f.code, track_batches: f.track_batches }),
    )
    if (ok) onSaved()
  }
  return (
    <Modal title={initial.id ? `Edit ${initial.name}` : 'New product'} onClose={onClose}>
      <div className="form">
        <div className="row-2">
          <Field label="Code"><input value={f.code} onChange={set('code')} disabled={Boolean(initial.id)} autoFocus /></Field>
          <Field label="Barcode"><input value={f.barcode} onChange={set('barcode')} /></Field>
        </div>
        <Field label="Name"><input value={f.name} onChange={set('name')} /></Field>
        <div className="row-2">
          <Field label="Generic / formula"><input value={f.generic_name} onChange={set('generic_name')} /></Field>
          <Field label="Manufacturer"><input value={f.manufacturer} onChange={set('manufacturer')} /></Field>
        </div>
        <div className="row-3">
          <Field label="Pack size"><input value={f.pack_size} onChange={set('pack_size')} placeholder="10x10" /></Field>
          <Field label="Sale unit"><input value={f.sale_unit} onChange={set('sale_unit')} /></Field>
          <Field label="Sale price (Rs)"><input value={f.sale_price} onChange={set('sale_price')} /></Field>
        </div>
        <Field label="Reorder level (alert when stock falls to this)"><input value={f.reorder_level} onChange={set('reorder_level')} /></Field>
        {!initial.id && <label className="check"><input type="checkbox" checked={f.track_batches} onChange={(e) => setF({ ...f, track_batches: e.target.checked })} /> Track batches and expiry</label>}
        {initial.id && <label className="check"><input type="checkbox" checked={f.is_active} onChange={(e) => setF({ ...f, is_active: e.target.checked })} /> Active</label>}
        <ErrorBox error={error} />
        <div className="modal-actions">
          <button className="btn" onClick={onClose}>Cancel</button>
          <button className="btn primary" disabled={busy || !f.name || !f.code || !f.sale_price} onClick={() => void save()}>Save</button>
        </div>
      </div>
    </Modal>
  )
}

function ProductDetail({ product, stock, state, onEdit, onClose, onChanged }: {
  product: Product; stock: StockRow[]; state: AppState; onEdit: () => void; onClose: () => void; onChanged: () => void
}) {
  const perms = state.user?.permissions
  const [adjust, setAdjust] = useState<{ batch_id: string | null; qty: string; reason: string } | null>(null)
  const [transfer, setTransfer] = useState<{ batch_id: string | null; to: string; qty: string } | null>(null)
  const tenant = useApi<{ warehouses: { id: string; code: string; name: string }[] }>('/api/v1/tenant')
  const warehouses = tenant.data?.warehouses ?? []
  async function saveTransfer() {
    const ok = await run(() => ordexa().request('POST', '/api/v1/inventory/transfers', {
      id: uid(), from_warehouse_id: warehouses[0].id, to_warehouse_id: transfer!.to, product_id: product.id,
      batch_id: transfer!.batch_id, quantity: transfer!.qty, note: 'store transfer',
    }))
    if (ok) { setTransfer(null); onChanged() }
  }
  const { busy, error, run } = useAction()
  async function saveAdjust() {
    const wh = (await ordexa().request<{ warehouses: { id: string }[] }>('GET', '/api/v1/tenant')).warehouses[0]
    const ok = await run(() => ordexa().request('POST', '/api/v1/inventory/adjustments', {
      id: uid(), warehouse_id: wh.id, product_id: product.id, batch_id: adjust!.batch_id, qty_delta: adjust!.qty, kind: 'adjustment', reason: adjust!.reason,
    }))
    if (ok) { setAdjust(null); onChanged() }
  }
  return (
    <Modal title={product.name} onClose={onClose} wide>
      <div className="muted">{[product.code, product.generic_name, product.manufacturer, product.pack_size].filter(Boolean).join(' · ')} · {rs(product.sale_price)} / {product.sale_unit}</div>
      <h3>Stock by batch</h3>
      <Table
        rows={stock}
        empty="No stock recorded."
        columns={[
          { key: 'batch_no', label: 'Batch', render: (s) => s.batch_no ?? '—' },
          { key: 'expiry_date', label: 'Expiry', render: (s) => s.expiry_date ?? '—' },
          { key: 'on_hand', label: 'On hand', num: true, render: (s) => qtyText(s.on_hand) },
          { key: 'cost_price', label: 'Cost', num: true, render: (s) => rs(s.cost_price) },
          { key: 'value', label: 'Value', num: true, render: (s) => rs(s.value) },
          { key: 'a', label: '', render: (s) => (
            <span className="row-actions">
              {can(perms, 'inventory.adjust') && <button className="btn small" onClick={() => setAdjust({ batch_id: s.batch_id, qty: '', reason: '' })}>Adjust</button>}
              {can(perms, 'inventory.transfer') && warehouses.length > 1 && <button className="btn small" onClick={() => setTransfer({ batch_id: s.batch_id, to: warehouses[1].id, qty: '' })}>Transfer</button>}
            </span>
          ) },
        ]}
      />
      {adjust && (
        <div className="form card pad inset">
          <div className="row-2">
            <Field label="Change (+ add, − remove)"><input value={adjust.qty} onChange={(e) => setAdjust({ ...adjust, qty: e.target.value })} placeholder="-2" autoFocus /></Field>
            <Field label="Reason (required)"><input value={adjust.reason} onChange={(e) => setAdjust({ ...adjust, reason: e.target.value })} placeholder="breakage, count correction…" /></Field>
          </div>
          <ErrorBox error={error} />
          <div className="modal-actions">
            <button className="btn" onClick={() => setAdjust(null)}>Cancel</button>
            <button className="btn primary" disabled={busy || !adjust.qty || adjust.reason.length < 3} onClick={() => void saveAdjust()}>Save adjustment</button>
          </div>
        </div>
      )}
      {transfer && (
        <div className="form card pad inset">
          <div className="row-2">
            <Field label={`From ${warehouses[0]?.name} to`}>
              <select value={transfer.to} onChange={(e) => setTransfer({ ...transfer, to: e.target.value })}>
                {warehouses.slice(1).map((w) => <option key={w.id} value={w.id}>{w.name}</option>)}
              </select>
            </Field>
            <Field label="Quantity"><input value={transfer.qty} onChange={(e) => setTransfer({ ...transfer, qty: e.target.value })} autoFocus /></Field>
          </div>
          <ErrorBox error={error} />
          <div className="modal-actions">
            <button className="btn" onClick={() => setTransfer(null)}>Cancel</button>
            <button className="btn primary" disabled={busy || !transfer.qty} onClick={() => void saveTransfer()}>Transfer stock</button>
          </div>
        </div>
      )}
      <div className="modal-actions">
        {can(perms, 'product.manage') && <button className="btn" onClick={onEdit}>Edit product</button>}
        <button className="btn primary" onClick={onClose}>Close</button>
      </div>
    </Modal>
  )
}
