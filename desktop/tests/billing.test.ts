import { beforeEach, describe, expect, it } from 'vitest'

import { Billing } from '../src/core/billing'
import { LocalStore } from '../src/core/localStore'
import { lineTotal, milliToString, paisaToString, toMilli, toPaisa } from '../src/core/money'
import type { SessionUser } from '../src/core/types'

const cashier: SessionUser = {
  username: 'cashier',
  full_name: 'Counter 1',
  role: 'staff',
  permissions: ['invoice.create', 'product.view', 'customer.view'],
  online: false,
}

function isoDays(days: number): string {
  const d = new Date()
  d.setUTCDate(d.getUTCDate() + days)
  return d.toISOString().slice(0, 10)
}

function seed(store: LocalStore) {
  const db = store.db
  store.setMeta('terminal_code', 'T1')
  store.setMeta('stock_snapshot_at', new Date(Date.now() - 60_000).toISOString())
  db.prepare('INSERT INTO products VALUES (?,?,?,?,?,?,?,?,?,?,?)').run('p1', 'P001', 'Panadol 500mg', 'Paracetamol', 'GSK', '20x10', 'box', 45000, null, 1, 1)
  db.prepare('INSERT INTO products VALUES (?,?,?,?,?,?,?,?,?,?,?)').run('p2', 'P010', 'Surgical Mask', null, null, '50', 'box', 35000, '8964000', 0, 1)
  db.prepare('INSERT INTO batches VALUES (?,?,?,?,?,?)').run('b-old', 'p1', 'OLD', isoDays(-5), null, 1) // expired
  db.prepare('INSERT INTO batches VALUES (?,?,?,?,?,?)').run('b-soon', 'p1', 'SOON', isoDays(30), null, 1)
  db.prepare('INSERT INTO batches VALUES (?,?,?,?,?,?)').run('b-late', 'p1', 'LATE', isoDays(400), 46000, 1)
  for (const [p, b, q] of [['p1', 'b-old', 50], ['p1', 'b-soon', 5], ['p1', 'b-late', 10], ['p2', '', 100]] as const) {
    db.prepare('INSERT INTO stock VALUES (?,?,?)').run(p, b, q * 1000)
  }
  db.prepare('INSERT INTO customers VALUES (?,?,?,?,?,?,?)').run('c101', '101', 'Madina Medical Store', null, null, 1000000, 1) // Rs 10,000 limit
  db.prepare('INSERT INTO customer_balances VALUES (?,?)').run('c101', 900000) // owes Rs 9,000
}

describe('money', () => {
  it('parses and rounds half-up like the backend', () => {
    expect(toPaisa('450')).toBe(45000)
    expect(toPaisa('1.005')).toBe(101)
    expect(toPaisa('-1.005')).toBe(-101)
    expect(toMilli('1.5')).toBe(1500)
    expect(lineTotal(toMilli('1.5'), toPaisa('450'))).toBe(67500)
    expect(lineTotal(toMilli('0.333'), toPaisa('1'))).toBe(33)
    expect(paisaToString(4500)).toBe('45.00')
    expect(milliToString(2000)).toBe('2')
    expect(() => toPaisa('12abc')).toThrow()
  })
})

describe('billing (offline)', () => {
  let store: LocalStore
  let billing: Billing

  beforeEach(() => {
    store = new LocalStore(':memory:')
    seed(store)
    billing = new Billing(store)
  })

  it('never offers expired batches and lists the rest earliest-expiry first', () => {
    const batches = billing.batches('p1')
    expect(batches.map((b) => b.batch_no)).toEqual(['SOON', 'LATE'])
    expect(batches[0].near_expiry).toBe(true)
    expect(batches[1].sale_price).toBe(46000) // batch price overrides product price
  })

  it('numbers invoices from this PC series, sequentially', () => {
    const a = billing.createInvoice({ customerId: null, lines: [{ productId: 'p2', qty: '1' }] }, cashier)
    const b = billing.createInvoice({ customerId: null, lines: [{ productId: 'p2', qty: '1' }] }, cashier)
    expect(a.ok && a.invoice.number).toBe('T1-000001')
    expect(b.ok && b.invoice.number).toBe('T1-000002')
    expect(a.ok && a.invoice.sync_status).toBe('pending')
  })

  it('splits a line across batches FEFO and asks to confirm near-expiry stock', () => {
    const draft = { customerId: null, lines: [{ productId: 'p1', qty: '8' }] }
    const first = billing.createInvoice(draft, cashier)
    expect(first.ok).toBe(false)
    expect(!first.ok && first.needsConfirmation && first.warnings.map((w) => w.code)).toEqual(['near_expiry'])
    const res = billing.createInvoice(draft, cashier, true)
    expect(res.ok).toBe(true)
    if (!res.ok) return
    expect(res.invoice.lines.map((l) => [l.batch_no, l.qty])).toEqual([['SOON', 5000], ['LATE', 3000]])
    expect(res.invoice.total).toBe(5 * 45000 + 3 * 46000)
    expect(billing.available('p1', 'b-soon')).toBe(0)
    expect(billing.available('p1', 'b-late')).toBe(7000)
  })

  it('warns on insufficient stock, counting earlier lines of the same bill', () => {
    const draft = { customerId: null, lines: [{ productId: 'p2', qty: '60' }, { productId: 'p2', qty: '60' }] }
    const res = billing.createInvoice(draft, cashier)
    expect(!res.ok && res.needsConfirmation && res.warnings.map((w) => w.code)).toEqual(['insufficient_stock'])
  })

  it('warns when the customer would exceed the credit limit, including unsynced bills', () => {
    const one = billing.createInvoice({ customerId: 'c101', lines: [{ productId: 'p2', qty: '2' }] }, cashier)
    expect(one.ok).toBe(true) // 9,000 + 700 = 9,700 <= 10,000
    const two = billing.createInvoice({ customerId: 'c101', lines: [{ productId: 'p2', qty: '1' }] }, cashier)
    expect(!two.ok && two.needsConfirmation && two.warnings[0].code).toBe('credit_exceeded')
  })

  it('refuses users without invoice.create and price overrides without price.override', () => {
    const viewer = { ...cashier, permissions: ['product.view'] }
    const r = billing.createInvoice({ customerId: null, lines: [{ productId: 'p2', qty: '1' }] }, viewer)
    expect(!r.ok && !r.needsConfirmation && r.error).toMatch(/permission/)
    const o = billing.createInvoice({ customerId: null, lines: [{ productId: 'p2', qty: '1', unitPrice: '1' }] }, cashier)
    expect(!o.ok && !o.needsConfirmation && o.error).toMatch(/change prices/)
  })

  it('stops subtracting a synced bill once a newer stock snapshot includes it', () => {
    const res = billing.createInvoice({ customerId: null, lines: [{ productId: 'p2', qty: '10' }] }, cashier)
    expect(res.ok).toBe(true)
    expect(billing.available('p2', null)).toBe(90_000)
    store.db.prepare("UPDATE invoices SET sync_status = 'synced', synced_at = ?").run(new Date().toISOString())
    expect(billing.available('p2', null)).toBe(90_000) // synced after the snapshot: still subtracted
    store.db.prepare('UPDATE stock SET on_hand = 90000 WHERE product_id = ?').run('p2')
    store.setMeta('stock_snapshot_at', new Date(Date.now() + 1000).toISOString())
    expect(billing.available('p2', null)).toBe(90_000) // new snapshot already includes it: not double-counted
  })

  it('records counter receipts in their own series and counts them against the balance', () => {
    const payer = { ...cashier, permissions: [...cashier.permissions, 'payment.record'] }
    const r = billing.recordReceipt({ customerId: 'c101', amount: '2500' }, payer)
    expect(r.number).toBe('T1R-000001')
    expect(r.sync_status).toBe('pending')
    expect(billing.customerOutstanding('c101')).toBe(900000 - 250000)
    expect(() => billing.recordReceipt({ customerId: 'c101', amount: '0' }, payer)).toThrow(/more than zero/)
    expect(() => billing.recordReceipt({ customerId: 'c101', amount: '10' }, cashier)).toThrow(/permission/)
  })

  it('finds products by name, code, generic name or exact barcode, and customers by shop code', () => {
    expect(billing.searchProducts('para').map((p) => p.code)).toEqual(['P001'])
    expect(billing.searchProducts('8964000').map((p) => p.code)).toEqual(['P010'])
    expect(billing.searchCustomers('101')[0].name).toBe('Madina Medical Store')
  })
})

describe('server address check', () => {
  it('requires https on the internet but allows local addresses', async () => {
    const { checkServerUrl } = await import('../src/core/api')
    expect(checkServerUrl('https://ordexa-api.onrender.com/')).toBe('https://ordexa-api.onrender.com')
    expect(checkServerUrl('http://127.0.0.1:8000')).toBe('http://127.0.0.1:8000')
    expect(checkServerUrl('http://192.168.1.20:8000')).toBe('http://192.168.1.20:8000')
    expect(() => checkServerUrl('http://ordexa.example.com')).toThrow(/https/)
    expect(() => checkServerUrl('not a url')).toThrow(/valid URL/)
  })
})
