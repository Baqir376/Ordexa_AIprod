// Counter billing against the local replica. Works fully offline.
//
// Stock shown to the cashier = last server snapshot - invoices this PC made since that snapshot
// (pending ones, and ones synced after the snapshot was taken).

import { randomUUID } from 'node:crypto'

import type { LocalStore } from './localStore'
import { lineTotal, toMilli, toPaisa, type Milli, type Paisa } from './money'
import type {
  BatchInfo,
  CreateResult,
  CustomerHit,
  DraftInvoice,
  InvoiceLineView,
  InvoiceView,
  ProductHit,
  ReceiptView,
  SessionUser,
  Warning,
} from './types'

const NEAR_EXPIRY_DAYS = 90

function today(): string {
  return new Date().toISOString().slice(0, 10)
}

function addDays(iso: string, days: number): string {
  const d = new Date(`${iso}T00:00:00Z`)
  d.setUTCDate(d.getUTCDate() + days)
  return d.toISOString().slice(0, 10)
}

export class Billing {
  constructor(private readonly store: LocalStore) {}

  private get db() {
    return this.store.db
  }

  /** Quantity sold locally that the current stock snapshot does not include yet, per (product, batch). */
  private unsyncedSoldSql = `
    SELECT l.product_id, COALESCE(l.batch_id, '') AS batch_id, SUM(l.qty + l.bonus) AS sold
    FROM invoice_lines l JOIN invoices i ON i.id = l.invoice_id
    WHERE i.sync_status IN ('pending', 'rejected', 'conflict')
       OR (i.synced_at IS NOT NULL AND i.synced_at > COALESCE((SELECT value FROM meta WHERE key = 'stock_snapshot_at'), ''))
    GROUP BY l.product_id, COALESCE(l.batch_id, '')`

  available(productId: string, batchId: string | null): Milli {
    const row = this.db
      .prepare(
        `SELECT COALESCE((SELECT on_hand FROM stock WHERE product_id = ?1 AND batch_id = ?2), 0)
              - COALESCE((SELECT sold FROM (${this.unsyncedSoldSql}) WHERE product_id = ?1 AND batch_id = ?2), 0) AS avail`,
      )
      .get(productId, batchId ?? '') as { avail: number }
    return row.avail
  }

  searchProducts(query: string, limit = 25): ProductHit[] {
    const q = query.trim()
    const like = `%${q.replace(/[%_]/g, '')}%`
    const rows = this.db
      .prepare(
        `SELECT p.*, (COALESCE((SELECT SUM(on_hand) FROM stock s WHERE s.product_id = p.id), 0)
                    - COALESCE((SELECT SUM(sold) FROM (${this.unsyncedSoldSql}) u WHERE u.product_id = p.id), 0)) AS available
         FROM products p
         WHERE p.is_active = 1 AND (?1 = '' OR p.name LIKE ?2 OR p.code LIKE ?2 OR p.generic_name LIKE ?2 OR p.barcode = ?1)
         ORDER BY CASE WHEN p.code = ?1 OR p.barcode = ?1 THEN 0 WHEN p.name LIKE ?3 THEN 1 ELSE 2 END, p.name
         LIMIT ?4`,
      )
      .all(q, like, `${q}%`, limit) as Record<string, unknown>[]
    return rows.map((r) => ({
      id: r.id as string,
      code: r.code as string,
      name: r.name as string,
      generic_name: r.generic_name as string | null,
      manufacturer: r.manufacturer as string | null,
      pack_size: r.pack_size as string | null,
      sale_unit: r.sale_unit as string,
      sale_price: r.sale_price as number,
      track_batches: r.track_batches === 1,
      available: r.available as number,
    }))
  }

  /** Sellable batches, earliest expiry first (FEFO). Expired batches are never offered. */
  batches(productId: string): BatchInfo[] {
    const product = this.db.prepare('SELECT sale_price FROM products WHERE id = ?').get(productId) as
      | { sale_price: number }
      | undefined
    if (!product) return []
    const t = today()
    const rows = this.db
      .prepare(
        `SELECT * FROM batches WHERE product_id = ? AND is_active = 1 AND (expiry_date IS NULL OR expiry_date > ?)
         ORDER BY expiry_date IS NULL, expiry_date, batch_no`,
      )
      .all(productId, t) as Record<string, unknown>[]
    const soon = addDays(t, NEAR_EXPIRY_DAYS)
    return rows.map((b) => ({
      id: b.id as string,
      batch_no: b.batch_no as string,
      expiry_date: b.expiry_date as string | null,
      sale_price: (b.sale_price as number | null) ?? product.sale_price,
      available: this.available(productId, b.id as string),
      near_expiry: b.expiry_date !== null && (b.expiry_date as string) <= soon,
    }))
  }

  customerOutstanding(customerId: string): Paisa {
    // Server ledger balance + local documents the server has not included yet.
    const unsynced = `(sync_status IN ('pending', 'rejected', 'conflict')
      OR (synced_at IS NOT NULL AND synced_at > COALESCE((SELECT value FROM meta WHERE key = 'stock_snapshot_at'), '')))`
    const row = this.db
      .prepare(
        `SELECT COALESCE((SELECT outstanding FROM customer_balances WHERE customer_id = ?1), 0)
              + COALESCE((SELECT SUM(total - paid) FROM invoices WHERE customer_id = ?1 AND ${unsynced}), 0)
              - COALESCE((SELECT SUM(amount) FROM receipts WHERE customer_id = ?1 AND ${unsynced}), 0) AS o`,
      )
      .get(customerId) as { o: number }
    return row.o
  }

  /** Cash received from a shop at the counter. Saved locally first; synced like invoices. */
  recordReceipt(input: { customerId: string; amount: string; note?: string }, user: SessionUser): ReceiptView {
    if (!user.permissions.includes('payment.record')) throw new Error('You do not have permission to record payments.')
    const terminal = this.store.getMeta('terminal_code')
    if (!terminal) throw new Error('This PC is not activated.')
    const amount = toPaisa(input.amount)
    if (amount <= 0) throw new Error('Amount must be more than zero.')
    const c = this.db.prepare('SELECT id, name FROM customers WHERE id = ?').get(input.customerId) as
      | { id: string; name: string }
      | undefined
    if (!c) throw new Error('Customer not found.')
    const id = randomUUID()
    this.store.tx(() => {
      const seq = Number(this.store.getMeta('next_receipt_seq') ?? '1')
      this.store.setMeta('next_receipt_seq', String(seq + 1))
      this.db
        .prepare('INSERT INTO receipts(id, number, customer_id, customer_name, amount, received_at, note, created_by) VALUES (?,?,?,?,?,?,?,?)')
        .run(id, `${terminal}R-${String(seq).padStart(6, '0')}`, c.id, c.name, amount, new Date().toISOString(), input.note?.trim() || null, user.username)
    })
    return this.listReceipts(1)[0]
  }

  listReceipts(limit = 100): ReceiptView[] {
    return this.db.prepare('SELECT * FROM receipts ORDER BY received_at DESC LIMIT ?').all(limit) as unknown as ReceiptView[]
  }

  searchCustomers(query: string, limit = 20): CustomerHit[] {
    const q = query.trim()
    const like = `%${q.replace(/[%_]/g, '')}%`
    const rows = this.db
      .prepare(
        `SELECT * FROM customers WHERE is_active = 1 AND (?1 = '' OR code = ?1 OR name LIKE ?2 OR phone LIKE ?2)
         ORDER BY CASE WHEN code = ?1 THEN 0 ELSE 1 END, name LIMIT ?3`,
      )
      .all(q, like, limit) as Record<string, unknown>[]
    return rows.map((c) => ({
      id: c.id as string,
      code: c.code as string,
      name: c.name as string,
      phone: c.phone as string | null,
      credit_limit: c.credit_limit as number | null,
      outstanding: this.customerOutstanding(c.id as string),
    }))
  }

  /**
   * Create and save an invoice locally. If there are business warnings (stock, credit, expiry) and
   * ``confirmed`` is false, nothing is saved and the warnings are returned for the cashier to confirm.
   * Lines without a batch on batch-tracked products are split across batches FEFO.
   */
  createInvoice(draft: DraftInvoice, user: SessionUser, confirmed = false): CreateResult {
    if (!user.permissions.includes('invoice.create')) {
      return { ok: false, needsConfirmation: false, error: 'You do not have permission to create invoices.' }
    }
    const terminal = this.store.getMeta('terminal_code')
    if (!terminal) return { ok: false, needsConfirmation: false, error: 'This PC is not activated.' }
    if (!draft.lines.length) return { ok: false, needsConfirmation: false, error: 'Add at least one item.' }

    const warnings: Warning[] = []
    type Planned = Omit<InvoiceLineView, 'line_no'> & { product_id: string; batch_id: string | null; discount: Paisa }
    const planned: Planned[] = []
    const inBill = new Map<string, Milli>() // stock already taken by earlier lines of this bill
    const key = (productId: string, batchId: string | null) => `${productId}|${batchId ?? ''}`

    try {
      for (const dl of draft.lines) {
        const p = this.db.prepare('SELECT * FROM products WHERE id = ? AND is_active = 1').get(dl.productId) as
          | Record<string, unknown>
          | undefined
        if (!p) throw new Error('A product on this bill is no longer available.')
        const qty = toMilli(dl.qty)
        const bonus = toMilli(dl.bonus || '0')
        const discount = toPaisa(dl.discount || '0')
        if (qty <= 0 || bonus < 0 || discount < 0) throw new Error(`Invalid quantity for ${p.name}.`)
        if (dl.unitPrice !== undefined && !user.permissions.includes('price.override')) {
          throw new Error('You do not have permission to change prices.')
        }

        const allBatches = (p.track_batches === 1 ? this.batches(p.id as string) : []).map((b) => ({
          ...b,
          available: b.available - (inBill.get(key(p.id as string, b.id)) ?? 0),
        }))
        let allocations: { batch: BatchInfo | null; qty: Milli; bonus: Milli }[]
        if (p.track_batches !== 1) {
          allocations = [{ batch: null, qty, bonus }]
        } else if (dl.batchId) {
          const b = allBatches.find((x) => x.id === dl.batchId)
          if (!b) throw new Error(`Batch for ${p.name} is expired or unavailable.`)
          allocations = [{ batch: b, qty, bonus }]
        } else {
          if (!allBatches.length) throw new Error(`${p.name} has no sellable (unexpired) batch.`)
          allocations = this.fefo(allBatches, qty, bonus)
        }

        for (const a of allocations) {
          const price = dl.unitPrice !== undefined ? toPaisa(dl.unitPrice) : (a.batch?.sale_price ?? (p.sale_price as number))
          const lineDiscount = allocations.length === 1 ? discount : 0 // a split line carries no manual discount
          const k = key(p.id as string, a.batch?.id ?? null)
          const avail = this.available(p.id as string, a.batch?.id ?? null) - (inBill.get(k) ?? 0)
          inBill.set(k, (inBill.get(k) ?? 0) + a.qty + a.bonus)
          if (a.qty + a.bonus > avail) {
            warnings.push({
              code: 'insufficient_stock',
              message: `${p.name}${a.batch ? ` (batch ${a.batch.batch_no})` : ''}: only ${avail / 1000} in stock.`,
            })
          }
          if (a.batch?.near_expiry) {
            warnings.push({ code: 'near_expiry', message: `${p.name} batch ${a.batch.batch_no} expires ${a.batch.expiry_date}.` })
          }
          planned.push({
            product_id: p.id as string,
            batch_id: a.batch?.id ?? null,
            product_name: p.name as string,
            batch_no: a.batch?.batch_no ?? null,
            expiry_date: a.batch?.expiry_date ?? null,
            qty: a.qty,
            bonus: a.bonus,
            unit: p.sale_unit as string,
            unit_price: price,
            discount: lineDiscount,
            line_total: lineTotal(a.qty, price, lineDiscount),
          })
        }
      }
    } catch (e) {
      return { ok: false, needsConfirmation: false, error: (e as Error).message }
    }

    const subtotal = planned.reduce((s, l) => s + l.line_total, 0)
    let discount: Paisa
    let paid: Paisa
    try {
      discount = toPaisa(draft.discount || '0')
      paid = toPaisa(draft.paid || '0')
    } catch (e) {
      return { ok: false, needsConfirmation: false, error: (e as Error).message }
    }
    const total = subtotal - discount
    if (discount < 0 || total < 0 || paid < 0) return { ok: false, needsConfirmation: false, error: 'Invalid discount or payment.' }

    let customerName = draft.walkInName?.trim() || 'Walk-in customer'
    if (draft.customerId) {
      const c = this.db.prepare('SELECT * FROM customers WHERE id = ?').get(draft.customerId) as Record<string, unknown> | undefined
      if (!c) return { ok: false, needsConfirmation: false, error: 'Customer not found.' }
      customerName = c.name as string
      if (c.is_active !== 1) warnings.push({ code: 'customer_inactive', message: `${c.name} is marked inactive.` })
      if (c.credit_limit !== null) {
        const after = this.customerOutstanding(c.id as string) + total - paid
        if (after > (c.credit_limit as number)) {
          warnings.push({
            code: 'credit_exceeded',
            message: `${c.name} will owe Rs ${(after / 100).toFixed(2)}, above the credit limit of Rs ${((c.credit_limit as number) / 100).toFixed(2)}.`,
          })
        }
      }
    }

    if (warnings.length && !confirmed) return { ok: false, needsConfirmation: true, warnings }

    const id = randomUUID()
    const issuedAt = new Date().toISOString()
    this.store.tx(() => {
      const seq = Number(this.store.getMeta('next_invoice_seq') ?? '1')
      const num = `${terminal}-${String(seq).padStart(6, '0')}`
      this.store.setMeta('next_invoice_seq', String(seq + 1))
      this.db
        .prepare(
          `INSERT INTO invoices(id, number, customer_id, customer_name, walk_in_name, issued_at, subtotal, discount, total, paid, created_by)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
        )
        .run(id, num, draft.customerId, customerName, draft.walkInName?.trim() || null, issuedAt, subtotal, discount, total, paid, user.username)
      const ins = this.db.prepare(
        `INSERT INTO invoice_lines(invoice_id, line_no, product_id, batch_id, product_name, batch_no, expiry_date, unit, qty, bonus, unit_price, discount, line_total)
         VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
      )
      planned.forEach((l, i) =>
        ins.run(id, i + 1, l.product_id, l.batch_id, l.product_name, l.batch_no, l.expiry_date, l.unit, l.qty, l.bonus, l.unit_price, l.discount, l.line_total),
      )
      return num
    })
    return { ok: true, invoice: this.getInvoice(id)!, warnings }
  }

  /** Split a quantity across batches, earliest expiry first. Any shortfall stays on the last batch. */
  private fefo(batches: BatchInfo[], qty: Milli, bonus: Milli): { batch: BatchInfo; qty: Milli; bonus: Milli }[] {
    const out: { batch: BatchInfo; qty: Milli; bonus: Milli }[] = []
    let need = qty
    for (const b of batches) {
      if (need <= 0) break
      const take = Math.min(need, Math.max(b.available, 0))
      if (take > 0) {
        out.push({ batch: b, qty: take, bonus: 0 })
        need -= take
      }
    }
    if (need > 0) {
      const last = out.at(-1)
      if (last && last.batch === batches.at(-1)) last.qty += need
      else out.push({ batch: batches.at(-1)!, qty: need, bonus: 0 })
    }
    out[0].bonus = bonus // bonus goes with the first (earliest-expiry) batch
    return out
  }

  getInvoice(id: string): InvoiceView | null {
    const i = this.db.prepare('SELECT * FROM invoices WHERE id = ?').get(id) as Record<string, unknown> | undefined
    if (!i) return null
    const lines = this.db.prepare('SELECT * FROM invoice_lines WHERE invoice_id = ? ORDER BY line_no').all(id) as Record<string, unknown>[]
    return {
      id: i.id as string,
      number: i.number as string,
      customer_name: i.customer_name as string,
      issued_at: i.issued_at as string,
      subtotal: i.subtotal as number,
      discount: i.discount as number,
      total: i.total as number,
      paid: i.paid as number,
      sync_status: i.sync_status as InvoiceView['sync_status'],
      sync_error: i.sync_error as string | null,
      server_flags: JSON.parse(i.server_flags as string),
      lines: lines.map((l) => ({
        line_no: l.line_no as number,
        product_name: l.product_name as string,
        batch_no: l.batch_no as string | null,
        expiry_date: l.expiry_date as string | null,
        qty: l.qty as number,
        bonus: l.bonus as number,
        unit: l.unit as string,
        unit_price: l.unit_price as number,
        line_total: l.line_total as number,
      })),
    }
  }

  listInvoices(limit = 100): InvoiceView[] {
    const ids = this.db.prepare('SELECT id FROM invoices ORDER BY issued_at DESC, number DESC LIMIT ?').all(limit) as { id: string }[]
    return ids.map((r) => this.getInvoice(r.id)!)
  }
}
