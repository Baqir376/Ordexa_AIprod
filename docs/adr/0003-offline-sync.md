# ADR 0003: Offline billing and the sync protocol

**Status:** Accepted · 2026-11-17 · Verified by `desktop/tests/e2e/offline-sync.e2e.test.ts`

## Rules that keep sync conflict-free
| Rule | Why |
|---|---|
| Billing, cash receipt, printing, stock and customer lookup work offline | The counter must never stop |
| Master data (products, prices, customers, settings) is edited **online only** | No merge conflicts |
| Each PC has its own invoice series `T1-`, `T2-`… assigned at activation | Two PCs never produce the same number |
| The invoice id is a UUID made on the PC | Same identity before and after sync, so replays are harmless |
| Posted invoices are immutable; corrections are credit notes (Phase 1) | Sync only appends |
| Offline oversell, credit breach or stale price is **accepted and flagged** | The sale already happened; the owner reviews flags |

## Protocol
**Push** (`POST /api/v1/sync/push`, header `X-Device-Id`): sends up to 200 pending invoices. Each is posted
in its own savepoint through `sales.service.post_invoice`, which is the same path every channel uses. Each
invoice gets one of these results:
- `accepted`: posted, with optional flags;
- `duplicate`: same id and same content hash, so nothing changes;
- `conflict`: same id but different content;
- `rejected`: for example a wrong series or an unknown product.

Rejected and conflicting invoices stay on the PC, marked for attention. They are never dropped.

**Pull** (`GET /api/v1/sync/pull?cursor=N`) returns:
- master data with `change_seq > N`;
- branding, if it changed;
- a **full snapshot** of this PC's warehouse stock and of customer balances (small for a pilot, and always
  correct).

**Cursor safety:** `change_seq` comes from one PostgreSQL sequence. Master-data writers take a per-tenant
advisory lock (`lock_master_data`), so within a tenant the sequence order equals the commit order, and a
cursor can never skip a row that commits late.

**Local stock** = last snapshot − quantities on local invoices not yet reflected in it. That means invoices
still pending, plus invoices synced after the snapshot was taken (`synced_at > stock_snapshot_at`). A sync
always pushes before it pulls.

## Sign-in while offline
- After each successful online sign-in, the PC stores a scrypt hash of the password, never the password.
- Offline unlock is allowed for up to 7 days since that user's last online sign-in.
- A `401` from the server deletes the cached credential, so a deactivated user is locked out at the next
  online attempt.
- Tokens carry `perm_version`, so permission changes apply on the next request.
