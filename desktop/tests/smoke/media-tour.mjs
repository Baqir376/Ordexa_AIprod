// UI check: shop answers the bot's question in the chat, then sends a photo order and a voice note.
// Usage: node tests/smoke/media-tour.mjs <business-code> <admin-password> <photo.png> <voice.wav> <screenshot-dir>
import { mkdtempSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'

import { _electron as electron } from 'playwright-core'

const [code, password, photo, voice, shots = '.'] = process.argv.slice(2)
const env = { ...process.env, ORDEXA_DATA_DIR: mkdtempSync(join(tmpdir(), 'ordexa-media-')) }
delete env.ELECTRON_RUN_AS_NODE
const app = await electron.launch({ args: ['.'], env })
const page = await app.firstWindow()
page.setDefaultTimeout(20_000)
const shot = (name) => page.screenshot({ path: join(shots, `${name}.png`) })
const nav = (label) => page.locator('.sidebar button', { hasText: label }).click()
const errors = []
page.on('pageerror', (e) => errors.push(String(e)))

try {
  await page.getByPlaceholder('e.g. ali-chemist').fill(code)
  await page.locator('input[type=password]').fill(password)
  await page.getByLabel('Name for this PC').fill('Media PC')
  await page.getByRole('button', { name: 'Activate' }).click()
  await page.getByPlaceholder(/Walk-in/).waitFor()

  await nav('WhatsApp sign-ups')
  await page.getByRole('button', { name: 'Test chat (simulator)' }).click()
  await page.getByLabel("Shop's phone").fill('+923001110102')
  const box = page.getByPlaceholder(/Type as the shop/)
  const send = async (m) => {
    await box.fill(m)
    await page.getByRole('button', { name: 'Send', exact: true }).click()
    await page.waitForTimeout(900)
  }
  for (const m of ['hi', '1', '102', '3 box panadol aur 2 botal calpol']) await send(m)
  await page.waitForTimeout(2500) // AI worker reads the order and asks which Panadol
  await page.getByRole('button', { name: 'Load chat' }).click()
  await page.getByText(/konsa chahiye/).last().waitFor()
  await send('2')
  await page.getByText(/update ho gaya/).last().waitFor()
  await shot('m1-answer-in-chat')

  // photo of an order list, then a voice note
  await page.locator('.file-btn input[type=file]').setInputFiles(photo)
  await page.waitForTimeout(2500)
  await page.locator('.file-btn input[type=file]').setInputFiles(voice)
  await page.waitForTimeout(2500)
  await page.getByRole('button', { name: 'Load chat' }).click()
  await page.getByText(/voice message mil gaya/).last().waitFor()
  await shot('m2-media-in-chat')

  await nav('WhatsApp orders')
  const cards = page.locator('.order-card')
  await cards.filter({ hasText: '[Photo]' }).first().click()
  await page.locator('img.attachment-img').waitFor()
  await page.getByText(/Photo order: look at the picture/).waitFor()
  await shot('m3-photo-order')
  await cards.filter({ hasText: '[Voice note]' }).first().click()
  await page.locator('audio.attachment-audio').waitFor()
  await shot('m4-voice-order')
  const answered = cards.filter({ hasText: '3 box panadol' }).first()
  await answered.click()
  await page.getByText('Ready to approve').first().waitFor()
  await shot('m5-answered-order-ready')
  if (errors.length) throw new Error(`page errors: ${errors.join('; ')}`)
  console.log('MEDIA TOUR OK')
} catch (e) {
  await shot('media-error')
  console.error('MEDIA TOUR FAILED', e, errors)
  process.exitCode = 1
} finally {
  await app.close()
}
