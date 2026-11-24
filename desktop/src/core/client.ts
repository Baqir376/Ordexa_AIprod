// The desktop application controller: activation, online/offline sign-in, background sync and billing.
// It has no Electron dependency, so the whole offline flow is testable in plain Node.

import { randomBytes, scryptSync, timingSafeEqual } from 'node:crypto'

import { ApiError, NetworkError, apiRequest, checkServerUrl, type ApiOptions } from './api'
import { Billing } from './billing'
import { LocalStore } from './localStore'
import { SyncEngine, type SyncSummary } from './sync'
import type { AppState, Branding, CreateResult, DraftInvoice, SessionUser, SyncStatus } from './types'

const OFFLINE_LOGIN_MAX_DAYS = 7 // a user must sign in online at least weekly on this PC

interface LoginResponse {
  access_token: string
  expires_at: number
  user: { username: string; full_name: string; role: string; permissions: string[] }
  tenant: { code: string; name: string }
}

export class OrdexaClient {
  readonly store: LocalStore
  readonly billing: Billing
  readonly sync: SyncEngine
  private token: string | null = null
  private user: SessionUser | null = null
  private syncing: Promise<SyncSummary | null> | null = null
  private status: SyncStatus

  constructor(dbPath: string, private readonly defaultServerUrl = 'http://127.0.0.1:8000') {
    this.store = new LocalStore(dbPath)
    this.billing = new Billing(this.store)
    this.sync = new SyncEngine(this.store, () => this.apiOptions())
    this.status = { state: 'idle', pending: 0, attention: 0, last_sync_at: null, message: null }
  }

  private apiOptions(): ApiOptions {
    return {
      baseUrl: this.store.getMeta('server_url') ?? this.defaultServerUrl,
      token: this.token,
      deviceId: this.store.getMeta('device_id'),
    }
  }

  state(): AppState {
    return {
      activated: Boolean(this.store.getMeta('device_id')),
      server_url: this.store.getMeta('server_url'),
      tenant_code: this.store.getMeta('tenant_code'),
      terminal_code: this.store.getMeta('terminal_code'),
      device_name: this.store.getMeta('device_name'),
      branding: this.store.getJson<Branding>('branding'),
      user: this.user,
      sync: {
        ...this.status,
        pending: this.sync.pendingCount(),
        attention: this.sync.attentionCount(),
        last_sync_at: this.store.getMeta('last_sync_at'),
      },
    }
  }

  /** First run: an Administrator signs in online and registers this PC (gets its T-series). */
  async activate(input: { serverUrl: string; tenantCode: string; username: string; password: string; deviceName: string }): Promise<AppState> {
    if (this.store.getMeta('device_id')) throw new Error('This PC is already activated.')
    const opts: ApiOptions = { baseUrl: checkServerUrl(input.serverUrl), timeoutMs: 90_000 }
    const login = await apiRequest<LoginResponse>(opts, 'POST', '/api/v1/auth/login', {
      tenant_code: input.tenantCode,
      username: input.username,
      password: input.password,
    })
    const device = await apiRequest<{ id: string; terminal_code: string; warehouse_id: string }>(
      { ...opts, token: login.access_token },
      'POST',
      '/api/v1/devices/activate',
      { name: input.deviceName },
    )
    this.store.tx(() => {
      this.store.setMeta('server_url', opts.baseUrl)
      this.store.setMeta('tenant_code', login.tenant.code)
      this.store.setMeta('device_id', device.id)
      this.store.setMeta('terminal_code', device.terminal_code)
      this.store.setMeta('warehouse_id', device.warehouse_id)
      this.store.setMeta('device_name', input.deviceName)
      this.store.setMeta('next_invoice_seq', '1')
    })
    this.acceptLogin(login, input.password)
    await this.sync.pull()
    return this.state()
  }

  private acceptLogin(login: LoginResponse, password: string): void {
    this.token = login.access_token
    this.user = { ...login.user, online: true }
    const salt = randomBytes(16)
    const hash = scryptSync(password, salt, 32)
    this.store.db
      .prepare(
        `INSERT INTO offline_users(username, full_name, role, permissions, salt, hash, last_online_login) VALUES (?, ?, ?, ?, ?, ?, ?)
         ON CONFLICT(username) DO UPDATE SET full_name = excluded.full_name, role = excluded.role, permissions = excluded.permissions,
           salt = excluded.salt, hash = excluded.hash, last_online_login = excluded.last_online_login`,
      )
      .run(login.user.username, login.user.full_name, login.user.role, JSON.stringify(login.user.permissions), salt.toString('hex'), hash.toString('hex'), new Date().toISOString())
  }

  /** Sign in online if the server is reachable; otherwise unlock from cached credentials. */
  async login(username: string, password: string): Promise<SessionUser> {
    const tenantCode = this.store.getMeta('tenant_code')
    if (!tenantCode) throw new Error('This PC is not activated.')
    const uname = username.trim().toLowerCase()
    try {
      // Short wait at the counter: if the server is asleep or unreachable, unlock offline instead.
      const login = await apiRequest<LoginResponse>({ ...this.apiOptions(), timeoutMs: 25_000 }, 'POST', '/api/v1/auth/login', {
        tenant_code: tenantCode,
        username: uname,
        password,
      })
      this.acceptLogin(login, password)
      return this.user!
    } catch (e) {
      if (e instanceof ApiError) {
        if (e.status === 401) this.store.db.prepare('DELETE FROM offline_users WHERE username = ?').run(uname) // revoked/changed
        throw new Error(e.message)
      }
      if (!(e instanceof NetworkError)) throw e
    }
    return this.offlineLogin(uname, password)
  }

  private offlineLogin(username: string, password: string): SessionUser {
    const row = this.store.db.prepare('SELECT * FROM offline_users WHERE username = ?').get(username) as Record<string, string> | undefined
    const fail = new Error('Server is offline and these credentials are not saved on this PC. Sign in once while online.')
    if (!row) throw fail
    const age = Date.now() - Date.parse(row.last_online_login)
    if (age > OFFLINE_LOGIN_MAX_DAYS * 86_400_000) {
      throw new Error(`Offline sign-in expired: please connect to the internet and sign in (required every ${OFFLINE_LOGIN_MAX_DAYS} days).`)
    }
    const expected = Buffer.from(row.hash, 'hex')
    const actual = scryptSync(password, Buffer.from(row.salt, 'hex'), 32)
    if (!timingSafeEqual(expected, actual)) throw new Error('Incorrect password.')
    this.token = null
    this.user = { username: row.username, full_name: row.full_name, role: row.role, permissions: JSON.parse(row.permissions), online: false }
    this.status = { ...this.status, state: 'offline', message: 'Working offline. Bills will sync when the internet is back.' }
    return this.user
  }

  logout(): void {
    this.token = null
    this.user = null
  }

  currentUser(): SessionUser | null {
    return this.user
  }

  createInvoice(draft: DraftInvoice, confirmed: boolean): CreateResult {
    if (!this.user) return { ok: false, needsConfirmation: false, error: 'Please sign in.' }
    return this.billing.createInvoice(draft, this.user, confirmed)
  }

  recordReceipt(input: { customerId: string; amount: string; note?: string }) {
    if (!this.user) throw new Error('Please sign in.')
    return this.billing.recordReceipt(input, this.user)
  }

  /**
   * Online-only call for admin and back-office screens (master data, purchases, reports...). The server
   * enforces permissions; this only adds the session token and refuses non-API paths.
   */
  async request<T>(method: 'GET' | 'POST' | 'PUT' | 'DELETE', path: string, body?: unknown): Promise<T> {
    if (!this.user) throw new Error('Please sign in.')
    if (!/^\/api\/v1\/[\w\-/?=&.%:]*$/.test(path)) throw new Error('invalid path')
    if (!this.token) throw new Error('This screen needs the server. Connect to the internet and sign in again.')
    try {
      return await apiRequest<T>(this.apiOptions(), method, path, body)
    } catch (e) {
      if (e instanceof NetworkError) throw new Error('Cannot reach the server. This screen works only online; billing still works offline.')
      if (e instanceof ApiError && e.status === 401) {
        this.token = null
        this.status = { ...this.status, state: 'auth_required', message: 'Session expired. Sign in again.' }
      }
      throw e
    }
  }

  /** Download a binary file (order photo / voice note) from the API as a data: URL for <img> or <audio>. */
  async requestDataUrl(path: string): Promise<string> {
    if (!this.user) throw new Error('Please sign in.')
    if (!/^\/api\/v1\/[\w\-/]*$/.test(path)) throw new Error('invalid path')
    if (!this.token) throw new Error('This needs the server. Connect to the internet and sign in again.')
    const opts = this.apiOptions()
    let res: Response
    try {
      res = await fetch(`${opts.baseUrl.replace(/\/+$/, '')}${path}`, {
        headers: { Authorization: `Bearer ${this.token}` },
        signal: AbortSignal.timeout(60_000),
      })
    } catch {
      throw new Error('Cannot reach the server.')
    }
    if (!res.ok) throw new Error(`Could not load the file (HTTP ${res.status}).`)
    const type = (res.headers.get('content-type') ?? 'application/octet-stream').split(';')[0]
    return `data:${type};base64,${Buffer.from(await res.arrayBuffer()).toString('base64')}`
  }

  /** Push + pull. Safe to call often; concurrent calls share one run. Never throws. */
  syncNow(): Promise<SyncSummary | null> {
    if (this.syncing) return this.syncing
    this.syncing = this.runSync().finally(() => {
      this.syncing = null
    })
    return this.syncing
  }

  private async runSync(): Promise<SyncSummary | null> {
    if (!this.store.getMeta('device_id') || !this.user) return null
    if (!this.token) {
      this.status = { ...this.status, state: 'auth_required', message: 'Sign in again while online to sync.' }
      return null
    }
    this.status = { ...this.status, state: 'syncing', message: null }
    try {
      const summary = await this.sync.syncOnce()
      this.status = { ...this.status, state: 'idle', message: null }
      return summary
    } catch (e) {
      if (e instanceof NetworkError) {
        this.status = { ...this.status, state: 'offline', message: 'No connection to the server. Billing continues offline.' }
      } else if (e instanceof ApiError && e.status === 401) {
        this.token = null
        this.status = { ...this.status, state: 'auth_required', message: 'Session expired. Sign in again to sync.' }
      } else {
        this.status = { ...this.status, state: 'error', message: (e as Error).message }
      }
      return null
    }
  }

  close(): void {
    this.store.close()
  }
}
