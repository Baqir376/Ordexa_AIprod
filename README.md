# Ordexa ERP

A white-label ERP for Pakistani medicine distributors and chemists:
- a Windows desktop app that bills offline;
- a central cloud backend;
- an AI order desk that turns WhatsApp orders (English, Urdu, Roman Urdu) into draft bills that a person approves.

| | |
|---|---|
| Requirements | `../Ordexa_ERP_SRS_and_System_Architecture_v1.1.md` |
| Decisions | [`docs/adr/`](docs/adr): platform and processes, data location, offline sync, pharma pilot, AI order desk, WhatsApp sign-up |
| AI guardrails | [`docs/adr/0007-ai-guardrails.md`](docs/adr/0007-ai-guardrails.md): injection and hallucination guards, red-team results |
| Test WhatsApp | [`docs/WHATSAPP_TESTING.md`](docs/WHATSAPP_TESTING.md): connect Meta's free test number in Settings and test from your phone |
| Test the bot like a shop | [`docs/QA_CHAT_TESTING.md`](docs/QA_CHAT_TESTING.md): 50 tricky shop conversations against the live AI |
| Import products from Excel | `docker compose exec api ordexa import-products --code <business> --file /tmp/list.xlsx --stock 500` (name column required; unit, price, stock, generic, company optional) |
| Deploy (free) | [`docs/DEPLOY.md`](docs/DEPLOY.md): Neon Postgres + Render, one container, `render.yaml` blueprint |
| AI reference design | `../orderwise` (OrderWise) |

## What's in it
| Area | Features |
|---|---|
| Counter (works offline) | Keyboard-first billing (F2/F4/F9). Earliest-expiry batches first; expired batches never offered. Bonus quantities. Credit and stock warnings. Per-PC bill series. Cash receipts. Silent receipt printing / auto-print. Reprint. |
| WhatsApp + AI | Sign-up bot (shop code + one-time code, or staff approval). Shops answer the bot's questions in the chat. Photo orders and voice notes, read by AI: free with Groq (`GROQ_API_KEY`), or Claude. AI order reading (LLM, with a rule parser fallback). One-click fixes. Per-shop product memory, versioned. Approve → W-series bill. Replies to the shop. Balance query. Simulator. |
| Stock & purchasing | Products, batches, expiry, reorder levels. Purchases with batch, expiry, cost and new sale price. Stock adjustments with reasons. Warehouse transfers. Movement history. |
| Money | Customer and supplier ledgers. Receipts and payments (cash, bank, cheque). Cheque clearing and bounce. Sales returns (credit notes, restock or not). Expenses. Receivable and payable ageing. |
| Reports | Dashboard. Sales by day, product and customer. Profit. Stock valuation. Expiry. Reorder suggestions. Flagged bills. CSV export. |
| Admin | White-label branding and logo. Staff accounts with permission grid and presets. PC licensing and deactivation. Warehouses. Printer per PC. Daily automatic backup to the owner's PC. |

## Layout
```text
backend/   Python 3.12 · FastAPI · SQLAlchemy 2 · Alembic · PostgreSQL · Anthropic SDK
  src/ordexa/apps/      api.py · webhook.py · worker.py (queues: ai, channels, outbox)
  src/ordexa/modules/   tenants identity devices catalogue customers inventory sales sync
                        purchasing finance reports ai channels jobs audit
desktop/   Electron 45 · React 19 · TypeScript · node:sqlite (no native modules)
  src/core/             offline engine: local store, billing, sync, client (no Electron)
  src/renderer/         screens: billing, bills, WhatsApp orders, dashboard, customers, products,
                        purchases, suppliers, returns, cheques, expenses, reports, staff, settings
.github/workflows/  ci.yml · release-desktop.yml (Windows installer)
docker-compose.yml  db + migrate + api + webhook + worker-ai + worker-channels + worker-outbox
```

## Run locally (Windows)
```bash
docker compose up -d db
cd backend
python -m venv .venv && .venv/Scripts/pip install -e ".[dev]"
.venv/Scripts/ordexa migrate
.venv/Scripts/ordexa create-tenant --code ali-chemist --name "Ali Chemist" --plan plus --devices 10
.venv/Scripts/ordexa seed-demo --code ali-chemist
# simplest: one process, AI runs inside the API
ORDEXA_AI_INLINE=true .venv/Scripts/uvicorn ordexa.apps.api:app --port 8000
# production-like: separate processes (each can crash/restart alone)
.venv/Scripts/python -m ordexa.apps.worker --queue ai
.venv/Scripts/python -m ordexa.apps.worker --queue channels
.venv/Scripts/python -m ordexa.apps.worker --queue outbox

cd ../desktop && npm install && npm run dev   # "Activate this PC" with business code ali-chemist
```
Notes:
- **AI order reading:** set `ANTHROPIC_API_KEY` to have the AI model read the orders. Without it, the built-in rule parser
  is used.
- **WhatsApp go-live:** see [ADR 0006](docs/adr/0006-whatsapp-onboarding.md).
- **Installer:** `npm run dist` builds `desktop/dist/OrdexaERP-Setup-<version>.exe`.

## Tests
```bash
cd backend && .venv/Scripts/pytest            # 114 tests on real PostgreSQL
cd desktop && npm test                        # offline billing engine
ORDEXA_E2E_URL=http://127.0.0.1:8000 ORDEXA_E2E_TENANT=<code> ORDEXA_E2E_PASSWORD=<pw> npm run test:e2e
npm run build && node tests/smoke/full-tour.mjs <code> <pw> <screenshot-dir>   # drives the real app
```

| Suite | What it covers |
|---|---|
| Backend (114 tests) | Permissions, tenant isolation, sync idempotency, purchases, ledger/cheques/returns, reports, AI resolution/memory/approval, WhatsApp sign-up and lockout, webhook → worker. |
| Two-PC end-to-end test | Two PCs bill and take receipts offline, reconnect, replay; no duplicates and exact stock. |
| UI tour | Bills a shop, signs a shop up over WhatsApp, orders in Roman Urdu, fixes and approves the AI draft into a bill, then opens every back-office screen. |

## Not built (outside the SRS MVP or needs third parties)
- **Needs a third-party account or approval:**
  - live WhatsApp needs your Meta account;
  - an FBR/tax module needs rules confirmed by a tax adviser.
- **Separate projects:**
  - payroll/HR;
  - salesman Android apps (the API is ready for them);
  - SMS gateways.
