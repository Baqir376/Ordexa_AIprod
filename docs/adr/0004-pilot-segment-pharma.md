# ADR 0004: First pilot is a pharmaceutical distributor or chemist

**Status:** Accepted · 2026-11-17

## Decision
The first pilot targets pharmacy and medicine distribution. That makes these core from Phase 0 and 1:

- **Batches with expiry.** Selling is **earliest-expiry-first (FEFO)**: a line without a chosen batch is
  split across batches automatically.
- **Expired batches are never offered.** Batches within 90 days of expiry need cashier confirmation.
- **Bonus quantity** (e.g. 10+1 schemes) leaves stock but is not charged.
- **Units:** box, strip, pack and bottle per product. Conversions between units come in Phase 1.
- **Batch price override:** a batch can carry its own price, for trade-price or MRP changes.

## Consequences
Demo data (`ordexa seed-demo`) is a synthetic chemist catalogue. FMCG and general wholesale can switch
batch tracking off per product (`track_batches = false`).
