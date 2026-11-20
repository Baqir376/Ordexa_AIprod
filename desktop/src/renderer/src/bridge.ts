import type {
  AppState,
  BatchInfo,
  CreateResult,
  CustomerHit,
  DraftInvoice,
  InvoiceView,
  ProductHit,
  ReceiptView,
  SessionUser,
  SyncStatus,
} from '../../core/types'

export interface OrdexaBridge {
  state(): Promise<AppState>
  activate(input: { serverUrl: string; tenantCode: string; username: string; password: string; deviceName: string }): Promise<AppState>
  login(username: string, password: string): Promise<SessionUser>
  logout(): Promise<boolean>
  searchProducts(q: string): Promise<ProductHit[]>
  batches(productId: string): Promise<BatchInfo[]>
  searchCustomers(q: string): Promise<CustomerHit[]>
  createInvoice(draft: DraftInvoice, confirmed: boolean): Promise<CreateResult>
  listInvoices(): Promise<InvoiceView[]>
  syncNow(): Promise<SyncStatus>
  recordReceipt(input: { customerId: string; amount: string; note?: string }): Promise<ReceiptView>
  listReceipts(): Promise<ReceiptView[]>
  printers(): Promise<string[]>
  getPrinter(): Promise<{ name: string | null; auto: boolean }>
  setPrinter(name: string | null, auto: boolean): Promise<boolean>
  printReceipt(): Promise<boolean>
  backupNow(): Promise<string>
  backupInfo(): Promise<{ last: string | null; file: string | null; dir: string }>
  openBackups(): Promise<string>
  request<T = any>(method: 'GET' | 'POST' | 'PUT', path: string, body?: unknown): Promise<T>
  onState(fn: (s: AppState) => void): () => void
}

declare global {
  interface Window {
    ordexa: OrdexaBridge
  }
}

export const ordexa = (): OrdexaBridge => window.ordexa
