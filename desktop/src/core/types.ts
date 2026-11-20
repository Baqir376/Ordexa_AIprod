// Types shared by the Electron main process (core) and the renderer UI.

export interface Branding {
  display_name: string
  license_text: string
  logo_data_url: string | null
  primary_color: string
  address: string | null
  phone: string | null
  powered_by: string
  license_plan: string
  currency: string
  timezone: string
  license_expires_at: string | null
}

export interface ProductHit {
  id: string
  code: string
  name: string
  generic_name: string | null
  manufacturer: string | null
  pack_size: string | null
  sale_unit: string
  sale_price: number // paisa
  track_batches: boolean
  available: number // milli
}

export interface BatchInfo {
  id: string
  batch_no: string
  expiry_date: string | null
  sale_price: number // paisa (batch override or product price)
  available: number // milli
  near_expiry: boolean
}

export interface CustomerHit {
  id: string
  code: string
  name: string
  phone: string | null
  credit_limit: number | null // paisa
  outstanding: number // paisa, incl. unsynced local invoices
}

export interface DraftLine {
  productId: string
  batchId?: string | null
  qty: string // as typed, e.g. "2" or "1.5"
  bonus?: string
  unitPrice?: string // only with price.override
  discount?: string
}

export interface DraftInvoice {
  customerId: string | null
  walkInName?: string
  lines: DraftLine[]
  discount?: string
  paid?: string
}

export interface Warning {
  code: 'insufficient_stock' | 'credit_exceeded' | 'near_expiry' | 'customer_inactive'
  message: string
}

export interface InvoiceLineView {
  line_no: number
  product_name: string
  batch_no: string | null
  expiry_date: string | null
  qty: number
  bonus: number
  unit: string
  unit_price: number
  line_total: number
}

export interface InvoiceView {
  id: string
  number: string
  customer_name: string
  issued_at: string
  subtotal: number
  discount: number
  total: number
  paid: number
  sync_status: 'pending' | 'synced' | 'rejected' | 'conflict'
  sync_error: string | null
  server_flags: { code: string; [k: string]: unknown }[]
  lines: InvoiceLineView[]
}

export type CreateResult =
  | { ok: true; invoice: InvoiceView; warnings: Warning[] }
  | { ok: false; needsConfirmation: true; warnings: Warning[] }
  | { ok: false; needsConfirmation: false; error: string }

export interface SessionUser {
  username: string
  full_name: string
  role: string
  permissions: string[]
  online: boolean // false = unlocked offline from cached credentials
}

export interface SyncStatus {
  state: 'idle' | 'syncing' | 'offline' | 'auth_required' | 'error'
  pending: number
  attention: number // rejected / conflict invoices
  last_sync_at: string | null
  message: string | null
}

export interface AppState {
  activated: boolean
  server_url: string | null
  tenant_code: string | null
  terminal_code: string | null
  device_name: string | null
  branding: Branding | null
  user: SessionUser | null
  sync: SyncStatus
}

export interface ReceiptView {
  id: string
  number: string
  customer_id: string
  customer_name: string
  amount: number // paisa
  received_at: string
  note: string | null
  sync_status: 'pending' | 'synced' | 'rejected' | 'conflict'
  sync_error: string | null
}
