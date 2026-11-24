// Screenshot every back-office screen (no data changes beyond activating a PC).
// Usage: node tests/smoke/screens-tour.mjs <business-code> <admin-password> <screenshot-dir>
import { mkdtempSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'

import { _electron as electron } from 'playwright-core'

const [code, password, shots = '.'] = process.argv.slice(2)
const env = { ...process.env, ORDEXA_DATA_DIR: mkdtempSync(join(tmpdir(), 'ordexa-screens-')) }
delete env.ELECTRON_RUN_AS_NODE
const app = await electron.launch({ args: ['.'], env })
const page = await app.firstWindow()
page.setDefaultTimeout(15_000)
await page.setViewportSize({ width: 1400, height: 860 })
const errors = []
page.on('pageerror', (e) => errors.push(String(e)))
const shot = (name) => page.screenshot({ path: join(shots, `${name}.png`) })

try {
  await page.getByPlaceholder('e.g. ali-chemist').fill(code)
  await page.locator('input[type=password]').fill(password)
  await page.getByLabel('Name for this PC').fill('Screens PC')
  await page.getByRole('button', { name: 'Activate' }).click()
  await page.getByPlaceholder(/Walk-in/).waitFor()
  await page.waitForTimeout(1500)
  await shot('01-new-bill')
  const screens = [
    ['Bills & receipts', '02-bills'],
    ['WhatsApp orders', '03-whatsapp-orders'],
    ['Dashboard', '05-dashboard'],
    ['Customers', '06-customers'],
    ['WhatsApp sign-ups', '07-signups'],
    ['Products & stock', '08-products'],
    ['Reports', '09-reports'],
    ['Settings', '10-settings'],
  ]
  for (const [label, name] of screens) {
    await page.locator('.sidebar button', { hasText: label }).click()
    await page.waitForTimeout(1800)
    await shot(name)
    if (label === 'WhatsApp orders') {
      const first = page.locator('.order-card').first()
      if (await first.isVisible()) {
        await first.click()
        await page.waitForTimeout(1500)
        await shot('04-order-detail')
        const chatTab = page.getByRole('button', { name: /Chat with shop/ })
        if (await chatTab.isVisible()) {
          await chatTab.click()
          await page.waitForTimeout(1500)
          await shot('04b-order-chat')
        }
      }
      await page.getByRole('tab', { name: /Chats/ }).click()
      await page.waitForTimeout(1500)
      const conv = page.locator('.conv').first()
      if (await conv.isVisible()) await conv.click()
      await page.waitForTimeout(1500)
      await shot('04c-chats')
      await page.getByRole('tab', { name: /Orders/ }).click()
    }
    if (label === 'Bills & receipts') {
      await page.getByRole('button', { name: /All bills/ }).click()
      await page.waitForTimeout(1500)
      await shot('02b-all-bills')
    }
  }
  console.log('SCREENS OK', errors)
} catch (e) {
  console.error('SCREENS FAILED', e, errors)
  await shot('zz-failure').catch(() => {})
  process.exitCode = 1
} finally {
  await app.close()
}
