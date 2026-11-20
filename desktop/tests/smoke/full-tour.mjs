// Full UI tour of the real Electron app against a live API (AI inline, rules provider is fine).
// Usage: node tests/smoke/full-tour.mjs <business-code> <admin-password> <screenshot-dir>
import { mkdtempSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'

import { _electron as electron } from 'playwright-core'

const [code, password, shots = '.'] = process.argv.slice(2)
const app = await electron.launch({ args: ['.'], env: { ...process.env, ORDEXA_DATA_DIR: mkdtempSync(join(tmpdir(), 'ordexa-tour-')) } })
const page = await app.firstWindow()
page.setDefaultTimeout(15_000)
const shot = (name) => page.screenshot({ path: join(shots, `${name}.png`) })
const nav = (label) => page.locator('.sidebar button', { hasText: label }).click()
const errors = []
page.on('pageerror', (e) => errors.push(String(e)))

try {
  await page.getByPlaceholder('e.g. ali-chemist').fill(code)
  await page.locator('input[type=password]').fill(password)
  await page.getByLabel('Name for this PC').fill('Counter 1')
  await page.getByRole('button', { name: 'Activate' }).click()
  await page.getByPlaceholder(/Walk-in/).waitFor()

  // 1. Counter bill for shop 103
  await page.keyboard.press('F4')
  await page.keyboard.type('103')
  await page.waitForTimeout(300)
  await page.keyboard.press('Enter')
  for (const [q, qty] of [['risek', '4'], ['ors', '10']]) {
    await page.keyboard.type(q)
    await page.waitForTimeout(300)
    await page.keyboard.press('Enter')
    await page.keyboard.press('Control+A')
    await page.keyboard.type(qty)
    await page.keyboard.press('Enter')
  }
  await page.keyboard.press('F9')
  await page.waitForTimeout(600)
  if (await page.getByText('Please confirm').isVisible()) await page.keyboard.press('Enter')
  await page.getByText(/Bill T\d+-\d+ saved/).waitFor()
  await shot('01-receipt')
  await page.keyboard.press('Enter')

  // 2. WhatsApp: shop 102 messages from its registered number, then orders in Roman Urdu
  await nav('WhatsApp sign-ups')
  await page.getByRole('button', { name: 'Test chat (simulator)' }).click()
  const phoneBox = page.getByLabel("Shop's phone")
  await phoneBox.fill('+923001110102')
  const msgBox = page.getByPlaceholder(/Type as the shop/)
  for (const m of ['hi', '1', '102', 'bhai 10 dabba panadol extra aur 5 botal calpol, 3 box panadol bhej dena']) {
    await msgBox.fill(m)
    await page.getByRole('button', { name: 'Send', exact: true }).click()
    await page.waitForTimeout(900)
  }
  await shot('02-whatsapp-chat')

  // 3. Order desk: fix the ambiguous "panadol" with one click, approve into a bill
  await nav('WhatsApp orders')
  await page.locator('.order-card').first().click()
  await page.getByText('Which product? Pick one below').waitFor()
  await shot('03-order-needs-review')
  await page.locator('.cand-row button', { hasText: 'Panadol 500mg' }).first().click()
  await page.getByRole('button', { name: 'Approve & create bill' }).waitFor()
  await page.waitForTimeout(500)
  page.once('dialog', (d) => d.accept())
  await page.getByRole('button', { name: 'Approve & create bill' }).click()
  await page.getByText(/Billed as W-\d+/).waitFor()
  await shot('04-order-approved')

  // 4. Back office screens
  for (const [label, name] of [['Dashboard', '05-dashboard'], ['Customers', '06-customers'], ['Products & stock', '07-products'],
    ['Purchases', '08-purchases'], ['Reports', '09-reports'], ['Staff', '10-staff'], ['Settings', '11-settings']]) {
    await nav(label)
    await page.waitForTimeout(1200)
    await shot(name)
  }
  // customer ledger shows the W- bill
  await nav('Customers')
  await page.locator('tr', { hasText: 'Shifa Pharmacy' }).getByRole('button', { name: 'Ledger' }).click()
  await page.getByText(/W-\d+/).first().waitFor()
  await shot('12-ledger')
  await page.keyboard.press('Escape')
  if (errors.length) throw new Error(`page errors: ${errors.join('; ')}`)
  console.log('TOUR OK')
} catch (e) {
  await shot('error')
  console.error('TOUR FAILED', e, errors)
  process.exitCode = 1
} finally {
  await app.close()
}
