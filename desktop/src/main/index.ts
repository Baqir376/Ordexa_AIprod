// Electron main process. Owns the local database and the session token; the renderer only talks to it
// through the small IPC surface below (no Node access in the UI).

import { mkdirSync, writeFileSync } from 'node:fs'
import { join } from 'node:path'
import { gzipSync } from 'node:zlib'

import { app, BrowserWindow, ipcMain, Menu, shell } from 'electron'

import { OrdexaClient } from '../core/client'
import type { DraftInvoice } from '../core/types'

const SYNC_INTERVAL_MS = 30_000
const DEFAULT_SERVER = process.env.ORDEXA_SERVER_URL ?? 'http://127.0.0.1:8000'

let client: OrdexaClient
let win: BrowserWindow | null = null

// Separate profile (tests, demos of two PCs on one machine).
if (process.env.ORDEXA_DATA_DIR) app.setPath('userData', process.env.ORDEXA_DATA_DIR)

// Two copies writing the same local database would corrupt the invoice series.
if (!app.requestSingleInstanceLock()) {
  app.quit()
}

function notifyState(): void {
  win?.webContents.send('ordexa:state', client.state())
}

function createWindow(): void {
  win = new BrowserWindow({
    width: 1280,
    height: 800,
    minWidth: 1024,
    minHeight: 640,
    title: 'Ordexa ERP',
    backgroundColor: '#F6F7F9',
    show: false,
    webPreferences: {
      preload: join(__dirname, '../preload/index.js'),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
    },
  })
  win.once('ready-to-show', () => win?.show())
  win.webContents.setWindowOpenHandler(({ url }) => {
    if (url.startsWith('https://')) void shell.openExternal(url)
    return { action: 'deny' }
  })
  if (process.env.ELECTRON_RENDERER_URL) void win.loadURL(process.env.ELECTRON_RENDERER_URL)
  else void win.loadFile(join(__dirname, '../renderer/index.html'))
}

/** Wrap a handler so errors reach the UI as { error } instead of crashing the IPC call. */
function handle<A extends unknown[], R>(channel: string, fn: (...args: A) => R | Promise<R>): void {
  ipcMain.handle(channel, async (_e, ...args) => {
    try {
      return { ok: true, value: await fn(...(args as A)) }
    } catch (err) {
      return { ok: false, error: (err as Error).message }
    }
  })
}

function requireUser(): void {
  if (!client.currentUser()) throw new Error('Please sign in.')
}

function backupDir(): string {
  return join(app.getPath('documents'), 'Ordexa Backups')
}

/** Owner backup: full export of the business from the server, gzipped, saved on this PC. */
async function saveBackup(): Promise<string> {
  const data = await client.request('GET', '/api/v1/backup/export')
  const dir = backupDir()
  mkdirSync(dir, { recursive: true })
  const stamp = new Date().toISOString().slice(0, 16).replace(/[:T]/g, '-')
  const file = join(dir, `${client.store.getMeta('tenant_code')}-${stamp}.json.gz`)
  writeFileSync(file, gzipSync(JSON.stringify(data)))
  client.store.setMeta('last_backup_at', new Date().toISOString())
  client.store.setMeta('last_backup_file', file)
  return file
}

async function autoBackup(): Promise<void> {
  const user = client.currentUser()
  if (!user?.online || !user.permissions.includes('settings.configure')) return
  const last = client.store.getMeta('last_backup_at')
  if (last && Date.now() - Date.parse(last) < 20 * 3600_000) return
  try {
    await saveBackup()
  } catch {
    // retried at the next sign-in; billing is never affected
  }
}

async function syncAndNotify(): Promise<void> {
  await client.syncNow()
  notifyState()
}

app.whenReady().then(() => {
  client = new OrdexaClient(join(app.getPath('userData'), 'ordexa-local.db'), DEFAULT_SERVER)
  Menu.setApplicationMenu(null)

  handle('state', () => client.state())
  handle('activate', async (input: Parameters<OrdexaClient['activate']>[0]) => {
    const s = await client.activate(input)
    notifyState()
    return s
  })
  handle('login', async (username: string, password: string) => {
    const user = await client.login(username, password)
    void syncAndNotify().then(autoBackup)
    return user
  })
  handle('logout', () => {
    client.logout()
    notifyState()
    return true
  })
  handle('products.search', (q: string) => (requireUser(), client.billing.searchProducts(q)))
  handle('products.batches', (productId: string) => (requireUser(), client.billing.batches(productId)))
  handle('customers.search', (q: string) => (requireUser(), client.billing.searchCustomers(q)))
  handle('invoice.create', (draft: DraftInvoice, confirmed: boolean) => {
    const res = client.createInvoice(draft, confirmed)
    if (res.ok) {
      notifyState()
      void syncAndNotify() // deliver immediately when online; stays queued when offline
    }
    return res
  })
  handle('receipt.create', (input: { customerId: string; amount: string; note?: string }) => {
    const r = client.recordReceipt(input)
    notifyState()
    void syncAndNotify()
    return r
  })
  handle('receipts.list', () => (requireUser(), client.billing.listReceipts(200)))
  handle('api.request', (method: 'GET' | 'POST' | 'PUT', path: string, body?: unknown) => client.request(method, path, body))
  handle('api.dataUrl', (path: string) => client.requestDataUrl(path))
  handle('printers.list', async () => (win ? (await win.webContents.getPrintersAsync()).map((p) => p.name) : []))
  handle('printer.get', () => ({ name: client.store.getMeta('printer_name'), auto: client.store.getMeta('auto_print') === '1' }))
  handle('printer.set', (name: string | null, auto: boolean) => {
    client.store.setMeta('printer_name', name || null)
    client.store.setMeta('auto_print', auto ? '1' : '0')
    return true
  })
  // Silent printing to this PC's chosen receipt printer (thermal/dot-matrix); no dialog at the counter.
  handle('print.receipt', () => {
    const name = client.store.getMeta('printer_name')
    return new Promise<boolean>((resolve, reject) => {
      if (!win) return reject(new Error('no window'))
      win.webContents.print({ silent: Boolean(name), deviceName: name ?? undefined, printBackground: false, margins: { marginType: 'none' } }, (ok, reason) =>
        ok || reason === 'cancelled' ? resolve(ok) : reject(new Error(`Printing failed: ${reason}`)),
      )
    })
  })
  handle('backup.now', () => saveBackup())
  handle('backup.info', () => ({ last: client.store.getMeta('last_backup_at'), file: client.store.getMeta('last_backup_file'), dir: backupDir() }))
  handle('backup.open', () => shell.openPath(backupDir()))
  handle('invoices.list', () => (requireUser(), client.billing.listInvoices(200)))
  handle('sync.now', async () => {
    await syncAndNotify()
    return client.state().sync
  })

  createWindow()
  setInterval(() => void syncAndNotify(), SYNC_INTERVAL_MS)
})

app.on('second-instance', () => {
  if (win) {
    if (win.isMinimized()) win.restore()
    win.focus()
  }
})

app.on('window-all-closed', () => {
  client?.close()
  app.quit()
})
