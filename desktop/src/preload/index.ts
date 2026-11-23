// The only bridge between the UI and the main process. Exposes a fixed, typed set of calls.

import { contextBridge, ipcRenderer } from 'electron'

import type { AppState } from '../core/types'

type Result<T> = { ok: true; value: T } | { ok: false; error: string }

async function call<T>(channel: string, ...args: unknown[]): Promise<T> {
  const r = (await ipcRenderer.invoke(channel, ...args)) as Result<T>
  if (!r.ok) throw new Error(r.error)
  return r.value
}

const api = {
  state: () => call('state'),
  activate: (input: unknown) => call('activate', input),
  login: (username: string, password: string) => call('login', username, password),
  logout: () => call('logout'),
  searchProducts: (q: string) => call('products.search', q),
  batches: (productId: string) => call('products.batches', productId),
  searchCustomers: (q: string) => call('customers.search', q),
  createInvoice: (draft: unknown, confirmed: boolean) => call('invoice.create', draft, confirmed),
  listInvoices: () => call('invoices.list'),
  syncNow: () => call('sync.now'),
  recordReceipt: (input: unknown) => call('receipt.create', input),
  listReceipts: () => call('receipts.list'),
  printers: () => call('printers.list'),
  getPrinter: () => call('printer.get'),
  setPrinter: (name: string | null, auto: boolean) => call('printer.set', name, auto),
  printReceipt: () => call('print.receipt'),
  backupNow: () => call('backup.now'),
  backupInfo: () => call('backup.info'),
  openBackups: () => call('backup.open'),
  request: (method: string, path: string, body?: unknown) => call('api.request', method, path, body),
  dataUrl: (path: string) => call('api.dataUrl', path),
  onState: (fn: (s: AppState) => void) => {
    const listener = (_: unknown, s: AppState) => fn(s)
    ipcRenderer.on('ordexa:state', listener)
    return () => ipcRenderer.removeListener('ordexa:state', listener)
  },
}

contextBridge.exposeInMainWorld('ordexa', api)
