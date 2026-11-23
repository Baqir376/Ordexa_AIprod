// UI smoke test of the real Electron app: activate -> sign in -> bill with the keyboard -> receipt -> bills list.
// Usage: node tests/smoke/electron-smoke.mjs <business-code> <admin-password> <screenshot-dir>
import { mkdtempSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'

import { _electron as electron } from 'playwright-core'

const [code, password, shots = '.'] = process.argv.slice(2)
const dataDir = mkdtempSync(join(tmpdir(), 'ordexa-smoke-'))
const env = { ...process.env, ORDEXA_DATA_DIR: dataDir }
delete env.ELECTRON_RUN_AS_NODE
const app = await electron.launch({ args: ['.'], env })
const page = await app.firstWindow()
page.on('console', (msg) => console.log('PAGE LOG:', msg.text()))
page.on('pageerror', (err) => console.log('PAGE ERR:', err))
const shot = (name) => page.screenshot({ path: join(shots, `${name}.png`) })
page.setDefaultTimeout(15_000)

try {
  await page.getByPlaceholder('e.g. ali-chemist').fill(code)
  await page.locator('input[type=password]').fill(password)
  await page.getByLabel('Name for this PC').fill('Smoke Counter')
  await shot('1-activate')
  await page.getByRole('button', { name: 'Activate' }).click()
  // Activation signs the administrator in; sign out to exercise the normal sign-in screen.
  await page.getByRole('button', { name: 'Sign out' }).click()

  await page.getByLabel('Username').fill('admin')
  await page.getByLabel('Password').fill(password)
  await shot('2-login')
  await page.getByRole('button', { name: 'Sign in' }).click()

  await page.getByPlaceholder(/Walk-in/).waitFor()
  await page.keyboard.press('F4')
  await page.keyboard.type('101')
  await page.waitForTimeout(300)
  await page.keyboard.press('Enter')

  for (const [q, qty] of [['panadol 500', '3'], ['calpol', '2'], ['brufen', '12']]) {
    await page.keyboard.type(q)
    await page.waitForTimeout(300)
    await page.keyboard.press('Enter')
    await page.keyboard.press('Control+A')
    await page.keyboard.type(qty)
    await page.keyboard.press('Enter')
  }
  await page.waitForTimeout(300)
  await shot('3-billing')

  await page.keyboard.press('F9')
  await page.waitForTimeout(500)
  if (await page.getByText('Please confirm').isVisible()) {
    await shot('4-confirm')
    await page.keyboard.press('Enter')
  }
  await page.getByText(/Bill T\d+-\d+ saved/).waitFor()
  await page.waitForTimeout(800)
  await shot('5-receipt')
  const receipt = await page.locator('#receipt').innerText()
  for (const want of ['Panadol 500mg', 'Calpol', 'Brufen', '3 x Rs 450.00', '2 x Rs 145.00', '12 x Rs 380.00', 'Madina Medical Store']) {
    if (!receipt.includes(want)) throw new Error(`receipt is missing "${want}":
${receipt}`)
  }

  await page.keyboard.press('Enter')
  await page.keyboard.press('F3')
  await page.waitForTimeout(1500)
  await shot('6-bills')
  console.log('SMOKE OK', dataDir)
} catch (e) {
  try { console.log('PAGE TEXT ON FAILURE:\n', await page.locator('body').innerText()) } catch {}
  await shot('error')
  console.error('SMOKE FAILED', e)
  process.exitCode = 1
} finally {
  await app.close()
}
