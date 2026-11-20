# ADR 0002: Cloud database is the source of truth; each PC keeps a local replica

**Status:** Accepted · 2026-11-17

## Decision
- **PostgreSQL in the cloud** holds every business record.
- **Each desktop holds a local SQLite file**, using Node's built-in `node:sqlite`, so there are no native
  modules to compile. It contains:
  - a read-only replica of products, batches, prices, customers, branding, this PC's stock and customer
    balances;
  - the **outbox** of invoices billed on this PC.
- **No server inside the shop.** A shop server means hardware visits and SQL Server maintenance. An
  on-premise option can be offered later for large distributors.
- **Owner trust (Phase 4):** a scheduled encrypted backup to the owner's PC and Google Drive, matching
  4M's "Auto Backup" feature.

## Consequences
- WhatsApp, AI, multi-branch reporting and owner dashboards have one place to read from.
- The desktop needs the internet for master-data edits, AI and reports, but not for billing (ADR 0003).
