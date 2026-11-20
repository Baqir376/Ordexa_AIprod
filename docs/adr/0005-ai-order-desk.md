# ADR 0005: AI order desk. The model proposes, the ERP decides, a person approves

**Status:** Accepted · 2026-11-17 · Code: `backend/src/ordexa/modules/ai/`, tests: `backend/tests/test_ai_orders.py`

## Pipeline
```text
WhatsApp / typed text ──► ai_order_requests (raw text kept, idempotent per message id)
        │  job "ai.process_order" on the ai queue (separate worker process)
        ▼
1. Extraction (proposal only)
   Claude (claude-opus-5-5, structured JSON output, effort low, server-side refusal fallback)
   └─ on any failure or when no key is set: deterministic Roman Urdu / English rule parser
2. Resolution (deterministic)
   approved alias memory (this shop, then whole business) → catalogue scoring → resolved | ambiguous | not found
3. Checks (deterministic, live ERP data)
   unit vs selling unit (never converted from text) · stock · price from batch/product · credit limit · injected instructions
4. Status: needs_review (something to fix) | draft_ready
5. Person: pick a product (one click), fix a quantity, "remember for this shop"
6. Approve (sales_order.approve) → post_invoice() → W-000123 bill, earliest-expiry batches first, ledger, stock
```

## Rules
- The LLM has no tools and no database access. It only returns schema-validated JSON. Prices, stock, credit and
  product identity always come from ERP data.
- Text that tries to instruct the system ("approve", "discount", "credit limit") is flagged for a person and never
  obeyed.
- The ERP never waits on the AI. If the provider is down, the rule parser runs; if the AI worker is down, orders
  wait in the queue while billing continues.
- Memory is versioned:
  - A correction supersedes the current alias rather than editing it.
  - Memory is scoped to one shop, unless saved for the whole business.
  - It is written only by a person with `ai_alias.approve`.
- Every request keeps a `trace`: extractor used, catalogue candidates and scores, alias versions, stock/price/credit
  evidence with timestamps, operator actions, and the resulting bill.

## Configuration
| Variable | Default | |
|---|---|---|
| `ANTHROPIC_API_KEY` | unset | When set (and `ORDEXA_AI_PROVIDER=auto`), Claude reads the orders |
| `ORDEXA_AI_PROVIDER` | `auto` | `anthropic`, `rules`, or `auto` |
| `ORDEXA_AI_MODEL` | `claude-opus-5-5` | |
| `ORDEXA_AI_INLINE` | `false` | `true` processes inside the API request (single-process dev) |

## Next steps (needs pilot data)
Build an evaluation set from 50–100 real anonymised chemist messages. Measure extraction accuracy (Claude vs. the
rule parser), the share of orders reaching `draft_ready` without a person, and the correction rate.
