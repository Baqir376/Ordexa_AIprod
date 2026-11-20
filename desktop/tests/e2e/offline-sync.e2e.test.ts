// Phase 0 exit test: two PCs bill offline, reconnect and sync; a replay after a lost response creates
// no duplicates; numbers stay unique; stock is reduced exactly once.
//
// Needs a running API and a fresh seeded business:
//   ORDEXA_E2E_URL=http://127.0.0.1:8000 ORDEXA_E2E_TENANT=<code> ORDEXA_E2E_PASSWORD=<admin password>

import { mkdtempSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'

import { afterAll, describe, expect, it } from 'vitest'

import { apiRequest } from '../../src/core/api'
import { OrdexaClient } from '../../src/core/client'

const URL = process.env.ORDEXA_E2E_URL ?? 'http://127.0.0.1:8000'
const TENANT = process.env.ORDEXA_E2E_TENANT ?? ''
const PASSWORD = process.env.ORDEXA_E2E_PASSWORD ?? ''
const DEAD = 'http://127.0.0.1:9' // nothing listens here: simulates the internet being down

const dir = mkdtempSync(join(tmpdir(), 'ordexa-e2e-'))
const clients: OrdexaClient[] = []

afterAll(() => {
  clients.forEach((c) => c.close())
  rmSync(dir, { recursive: true, force: true })
})

async function newPc(name: string): Promise<OrdexaClient> {
  const c = new OrdexaClient(join(dir, `${name}.db`))
  clients.push(c)
  await c.activate({ serverUrl: URL, tenantCode: TENANT, username: 'admin', password: PASSWORD, deviceName: name })
  return c
}

function bill(c: OrdexaClient, productCode: string, qty: string, customerCode?: string) {
  const product = c.billing.searchProducts(productCode)[0]
  const customer = customerCode ? c.billing.searchCustomers(customerCode)[0] : null
  const res = c.createInvoice({ customerId: customer?.id ?? null, lines: [{ productId: product.id, qty }] }, true)
  if (!res.ok) throw new Error(JSON.stringify(res))
  return res.invoice
}

describe.skipIf(!TENANT || !PASSWORD)('offline billing on two PCs', () => {
  it('syncs without duplicates and keeps stock exact', async () => {
    const pc1 = await newPc('PC1')
    const pc2 = await newPc('PC2')
    expect(pc1.state().terminal_code).not.toBe(pc2.state().terminal_code)
    expect(pc1.state().branding?.license_text).toMatch(/^Licensed to /)

    const panadol = pc1.billing.searchProducts('P001')[0]
    const startStock = panadol.available

    // Internet goes down on both PCs.
    pc1.store.setMeta('server_url', DEAD)
    pc2.store.setMeta('server_url', DEAD)
    const a = [bill(pc1, 'P001', '2', '101'), bill(pc1, 'P001', '2'), bill(pc1, 'P003', '1', '102')]
    const b = [bill(pc2, 'P001', '2'), bill(pc2, 'P001', '2', '103'), bill(pc2, 'P001', '2')]
    expect(await pc1.syncNow()).toBeNull()
    expect(pc1.state().sync.state).toBe('offline')
    expect(pc1.state().sync.pending).toBe(3)

    // Cashier restarts the app while offline: cached credentials unlock it.
    pc1.logout()
    const offlineUser = await pc1.login('admin', PASSWORD)
    expect(offlineUser.online).toBe(false)
    await expect(pc1.login('admin', 'wrong-password')).rejects.toThrow('Incorrect password')
    bill(pc1, 'P001', '1') // still billing while offline
    const shop = pc1.billing.searchCustomers('101')[0]
    const receipt = pc1.recordReceipt({ customerId: shop.id, amount: '500' }) // cash collected offline
    expect(receipt.number).toMatch(/^T\d+R-000001$/)

    // Internet is back.
    pc1.store.setMeta('server_url', URL)
    pc2.store.setMeta('server_url', URL)
    expect(await pc1.syncNow()).toBeNull()
    expect(pc1.state().sync.state).toBe('auth_required') // offline unlock has no server token
    expect((await pc1.login('admin', PASSWORD)).online).toBe(true)
    const s1 = await pc1.syncNow()
    const s2 = await pc2.syncNow()
    expect(s1?.accepted).toBe(5) // 4 invoices + 1 receipt
    expect(s2?.accepted).toBe(3)
    expect(pc1.state().sync.pending + pc2.state().sync.pending).toBe(0)

    // The server got PC1's invoices but the response was lost: PC1 pushes them all again.
    pc1.store.db.exec("UPDATE invoices SET sync_status = 'pending', synced_at = NULL")
    pc1.store.db.exec("UPDATE receipts SET sync_status = 'pending', synced_at = NULL")
    const replay = await pc1.syncNow()
    expect(replay).toMatchObject({ accepted: 0, duplicate: 5 })

    // Server view: exactly 7 invoices, unique numbers, from both series.
    const login = await apiRequest<{ access_token: string }>({ baseUrl: URL }, 'POST', '/api/v1/auth/login', {
      tenant_code: TENANT,
      username: 'admin',
      password: PASSWORD,
    })
    const invoices = await apiRequest<{ number: string; flags: { code: string }[] }[]>(
      { baseUrl: URL, token: login.access_token },
      'GET',
      '/api/v1/invoices',
    )
    expect(invoices).toHaveLength(7)
    expect(new Set(invoices.map((i) => i.number)).size).toBe(7)
    expect(new Set(invoices.map((i) => i.number.split('-')[0]))).toEqual(new Set(['T1', 'T2']))
    expect(new Set([...a, ...b].map((i) => i.number)).size).toBe(6)

    // Stock: PC1 and PC2 both see the server's number after pulling: 2+2+1 + 2+2+2 = 11 Panadol boxes gone.
    await pc2.syncNow()
    const after1 = pc1.billing.searchProducts('P001')[0].available
    const after2 = pc2.billing.searchProducts('P001')[0].available
    expect(after1).toBe(startStock - 11_000)
    expect(after2).toBe(after1)
  }, 60_000)
})
