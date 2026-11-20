// Outbox push + replica pull. Order matters: push first, then pull, so the new stock snapshot already
// includes the invoices just delivered (see Billing.unsyncedSoldSql).

import { apiRequest, type ApiOptions } from './api'
import type { LocalStore } from './localStore'
import { milliToString, paisaToString, toMilli, toPaisa } from './money'

const PUSH_BATCH = 100

interface PushResult {
  id: string
  number: string
  status: 'accepted' | 'duplicate' | 'conflict' | 'rejected'
  flags: { code: string }[]
  reason: string | null
}

interface PullResponse {
  cursor: number
  server_time: string
  branding: unknown | null
  products: Record<string, any>[]
  batches: Record<string, any>[]
  customers: Record<string, any>[]
  stock: { product_id: string; batch_id: string | null; on_hand: string }[]
  customer_balances: Record<string, string>
}

export interface SyncSummary {
  pushed: number
  accepted: number
  duplicate: number
  failed: number
  pulled: { products: number; batches: number; customers: number }
}

export class SyncEngine {
  constructor(
    private readonly store: LocalStore,
    private readonly api: () => ApiOptions,
  ) {}

  pendingCount(): number {
    return (
      this.store.db
        .prepare("SELECT (SELECT COUNT(*) FROM invoices WHERE sync_status = 'pending') + (SELECT COUNT(*) FROM receipts WHERE sync_status = 'pending') AS n")
        .get() as { n: number }
    ).n
  }

  attentionCount(): number {
    return (
      this.store.db
        .prepare(
          "SELECT (SELECT COUNT(*) FROM invoices WHERE sync_status IN ('rejected', 'conflict')) + (SELECT COUNT(*) FROM receipts WHERE sync_status IN ('rejected', 'conflict')) AS n",
        )
        .get() as { n: number }
    ).n
  }

  private payload(id: string) {
    const db = this.store.db
    const i = db.prepare('SELECT * FROM invoices WHERE id = ?').get(id) as Record<string, any>
    const lines = db.prepare('SELECT * FROM invoice_lines WHERE invoice_id = ? ORDER BY line_no').all(id) as Record<string, any>[]
    return {
      id: i.id,
      number: i.number,
      customer_id: i.customer_id,
      walk_in_name: i.walk_in_name,
      issued_at: i.issued_at,
      discount: paisaToString(i.discount),
      paid_amount: paisaToString(i.paid),
      total: paisaToString(i.total),
      lines: lines.map((l) => ({
        line_no: l.line_no,
        product_id: l.product_id,
        batch_id: l.batch_id,
        quantity: milliToString(l.qty),
        bonus_quantity: milliToString(l.bonus),
        unit_price: paisaToString(l.unit_price),
        discount: paisaToString(l.discount),
      })),
    }
  }

  async push(): Promise<Omit<SyncSummary, 'pulled'>> {
    const out = { pushed: 0, accepted: 0, duplicate: 0, failed: 0 }
    const db = this.store.db
    for (;;) {
      const ids = (
        db.prepare("SELECT id FROM invoices WHERE sync_status = 'pending' ORDER BY issued_at, number LIMIT ?").all(PUSH_BATCH) as { id: string }[]
      ).map((r) => r.id)
      if (!ids.length) break
      const res = await apiRequest<{ results: PushResult[] }>(this.api(), 'POST', '/api/v1/sync/push', {
        invoices: ids.map((id) => this.payload(id)),
      })
      const now = new Date().toISOString()
      this.store.tx(() => {
        const upd = db.prepare('UPDATE invoices SET sync_status = ?, sync_error = ?, server_flags = ?, synced_at = ? WHERE id = ?')
        for (const r of res.results) {
          out.pushed++
          if (r.status === 'accepted' || r.status === 'duplicate') {
            out[r.status]++
            upd.run('synced', null, JSON.stringify(r.flags ?? []), now, r.id)
          } else {
            out.failed++ // kept locally and shown to the user; never silently dropped
            upd.run(r.status, r.reason ?? r.status, '[]', null, r.id)
          }
        }
      })
      if (res.results.length < ids.length) throw new Error('server returned fewer results than invoices sent')
    }
    for (;;) {
      const rows = db
        .prepare("SELECT * FROM receipts WHERE sync_status = 'pending' ORDER BY received_at LIMIT ?")
        .all(PUSH_BATCH) as Record<string, any>[]
      if (!rows.length) return out
      const res = await apiRequest<{ receipts: PushResult[] }>(this.api(), 'POST', '/api/v1/sync/push', {
        receipts: rows.map((r) => ({
          id: r.id,
          number: r.number,
          customer_id: r.customer_id,
          amount: paisaToString(r.amount),
          received_at: r.received_at,
          note: r.note,
        })),
      })
      const now = new Date().toISOString()
      this.store.tx(() => {
        const upd = db.prepare('UPDATE receipts SET sync_status = ?, sync_error = ?, synced_at = ? WHERE id = ?')
        for (const r of res.receipts) {
          out.pushed++
          if (r.status === 'accepted' || r.status === 'duplicate') {
            out[r.status]++
            upd.run('synced', null, now, r.id)
          } else {
            out.failed++
            upd.run(r.status, r.reason ?? r.status, null, r.id)
          }
        }
      })
      if (res.receipts.length < rows.length) throw new Error('server returned fewer results than receipts sent')
    }
  }

  async pull(): Promise<SyncSummary['pulled']> {
    const cursor = Number(this.store.getMeta('cursor') ?? '0')
    const snapshotStartedAt = new Date().toISOString()
    const data = await apiRequest<PullResponse>(this.api(), 'GET', `/api/v1/sync/pull?cursor=${cursor}`)
    const db = this.store.db
    this.store.tx(() => {
      const up = (table: string, cols: string[]) =>
        db.prepare(
          `INSERT INTO ${table}(${cols.join(', ')}) VALUES (${cols.map(() => '?').join(', ')})
           ON CONFLICT(id) DO UPDATE SET ${cols.filter((c) => c !== 'id').map((c) => `${c} = excluded.${c}`).join(', ')}`,
        )
      const prod = up('products', ['id', 'code', 'name', 'generic_name', 'manufacturer', 'pack_size', 'sale_unit', 'sale_price', 'barcode', 'track_batches', 'is_active'])
      for (const p of data.products) {
        prod.run(p.id, p.code, p.name, p.generic_name, p.manufacturer, p.pack_size, p.sale_unit, toPaisa(p.sale_price), p.barcode, p.track_batches ? 1 : 0, p.is_active ? 1 : 0)
      }
      const bat = up('batches', ['id', 'product_id', 'batch_no', 'expiry_date', 'sale_price', 'is_active'])
      for (const b of data.batches) {
        bat.run(b.id, b.product_id, b.batch_no, b.expiry_date, b.sale_price === null ? null : toPaisa(b.sale_price), b.is_active ? 1 : 0)
      }
      const cus = up('customers', ['id', 'code', 'name', 'phone', 'address', 'credit_limit', 'is_active'])
      for (const c of data.customers) {
        cus.run(c.id, c.code, c.name, c.phone, c.address, c.credit_limit === null ? null : toPaisa(c.credit_limit), c.is_active ? 1 : 0)
      }
      db.exec('DELETE FROM stock; DELETE FROM customer_balances;')
      const st = db.prepare('INSERT INTO stock(product_id, batch_id, on_hand) VALUES (?, ?, ?)')
      for (const s of data.stock) st.run(s.product_id, s.batch_id ?? '', toMilli(s.on_hand))
      const cb = db.prepare('INSERT INTO customer_balances(customer_id, outstanding) VALUES (?, ?)')
      for (const [id, v] of Object.entries(data.customer_balances)) cb.run(id, toPaisa(v))
      if (data.branding) this.store.setJson('branding', data.branding)
      this.store.setMeta('cursor', String(data.cursor))
      this.store.setMeta('stock_snapshot_at', snapshotStartedAt)
      this.store.setMeta('last_sync_at', new Date().toISOString())
    })
    return { products: data.products.length, batches: data.batches.length, customers: data.customers.length }
  }

  async syncOnce(): Promise<SyncSummary> {
    const pushed = await this.push()
    const pulled = await this.pull()
    return { ...pushed, pulled }
  }
}
