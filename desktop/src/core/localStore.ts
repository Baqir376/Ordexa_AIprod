// The desktop's local database (SQLite via Node's built-in node:sqlite: no native modules to compile).
// It holds a read-only replica of master data pulled from the server, plus invoices created on this PC
// that wait in the outbox until they are pushed.

import { DatabaseSync } from 'node:sqlite'

const SCHEMA_VERSION = 1

const SCHEMA = `
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS products (
  id TEXT PRIMARY KEY, code TEXT NOT NULL, name TEXT NOT NULL, generic_name TEXT, manufacturer TEXT,
  pack_size TEXT, sale_unit TEXT NOT NULL, sale_price INTEGER NOT NULL, barcode TEXT,
  track_batches INTEGER NOT NULL, is_active INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS ix_products_name ON products(name);
CREATE TABLE IF NOT EXISTS batches (
  id TEXT PRIMARY KEY, product_id TEXT NOT NULL, batch_no TEXT NOT NULL, expiry_date TEXT,
  sale_price INTEGER, is_active INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS ix_batches_product ON batches(product_id);
CREATE TABLE IF NOT EXISTS customers (
  id TEXT PRIMARY KEY, code TEXT NOT NULL, name TEXT NOT NULL, phone TEXT, address TEXT,
  credit_limit INTEGER, is_active INTEGER NOT NULL);
-- Server snapshots (replaced on every pull). batch_id '' means "no batch".
CREATE TABLE IF NOT EXISTS stock (product_id TEXT NOT NULL, batch_id TEXT NOT NULL, on_hand INTEGER NOT NULL,
  PRIMARY KEY (product_id, batch_id));
CREATE TABLE IF NOT EXISTS customer_balances (customer_id TEXT PRIMARY KEY, outstanding INTEGER NOT NULL);
-- Outbox: invoices made on this PC. Never deleted; sync_status tracks delivery.
CREATE TABLE IF NOT EXISTS invoices (
  id TEXT PRIMARY KEY, number TEXT NOT NULL UNIQUE, customer_id TEXT, customer_name TEXT NOT NULL,
  walk_in_name TEXT, issued_at TEXT NOT NULL, subtotal INTEGER NOT NULL, discount INTEGER NOT NULL,
  total INTEGER NOT NULL, paid INTEGER NOT NULL, created_by TEXT NOT NULL,
  sync_status TEXT NOT NULL DEFAULT 'pending', sync_error TEXT, server_flags TEXT NOT NULL DEFAULT '[]',
  synced_at TEXT);
CREATE INDEX IF NOT EXISTS ix_invoices_sync ON invoices(sync_status);
CREATE TABLE IF NOT EXISTS invoice_lines (
  invoice_id TEXT NOT NULL REFERENCES invoices(id), line_no INTEGER NOT NULL, product_id TEXT NOT NULL,
  batch_id TEXT, product_name TEXT NOT NULL, batch_no TEXT, expiry_date TEXT, unit TEXT NOT NULL,
  qty INTEGER NOT NULL, bonus INTEGER NOT NULL, unit_price INTEGER NOT NULL, discount INTEGER NOT NULL,
  line_total INTEGER NOT NULL, PRIMARY KEY (invoice_id, line_no));
-- Cash collected from shops at the counter (offline-capable), pushed like invoices.
CREATE TABLE IF NOT EXISTS receipts (
  id TEXT PRIMARY KEY, number TEXT NOT NULL UNIQUE, customer_id TEXT NOT NULL, customer_name TEXT NOT NULL,
  amount INTEGER NOT NULL, received_at TEXT NOT NULL, note TEXT, created_by TEXT NOT NULL,
  sync_status TEXT NOT NULL DEFAULT 'pending', sync_error TEXT, synced_at TEXT);
-- Cached credentials for offline unlock (scrypt hash; never the password).
CREATE TABLE IF NOT EXISTS offline_users (
  username TEXT PRIMARY KEY, full_name TEXT NOT NULL, role TEXT NOT NULL, permissions TEXT NOT NULL,
  salt TEXT NOT NULL, hash TEXT NOT NULL, last_online_login TEXT NOT NULL);
`

export class LocalStore {
  readonly db: DatabaseSync

  constructor(path: string) {
    this.db = new DatabaseSync(path)
    this.db.exec('PRAGMA journal_mode = WAL; PRAGMA foreign_keys = ON; PRAGMA busy_timeout = 5000;')
    this.db.exec(SCHEMA)
    if (!this.getMeta('schema_version')) this.setMeta('schema_version', String(SCHEMA_VERSION))
  }

  close(): void {
    this.db.close()
  }

  getMeta(key: string): string | null {
    const row = this.db.prepare('SELECT value FROM meta WHERE key = ?').get(key) as { value: string } | undefined
    return row ? row.value : null
  }

  setMeta(key: string, value: string | null): void {
    if (value === null) this.db.prepare('DELETE FROM meta WHERE key = ?').run(key)
    else this.db.prepare('INSERT INTO meta(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value').run(key, value)
  }

  getJson<T>(key: string): T | null {
    const v = this.getMeta(key)
    return v ? (JSON.parse(v) as T) : null
  }

  setJson(key: string, value: unknown): void {
    this.setMeta(key, value === null ? null : JSON.stringify(value))
  }

  /** Run ``fn`` in one SQLite transaction (all-or-nothing). */
  tx<T>(fn: () => T): T {
    this.db.exec('BEGIN IMMEDIATE')
    try {
      const out = fn()
      this.db.exec('COMMIT')
      return out
    } catch (e) {
      this.db.exec('ROLLBACK')
      throw e
    }
  }
}
