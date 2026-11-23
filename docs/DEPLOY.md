# Deploying Ordexa for free

The free setup has one web service and one database. The API, the WhatsApp webhook and the AI, channels and outbox
workers all run in **one container** (`ORDEXA_EMBEDDED_WORKERS=true`). The Windows app talks to it over HTTPS.

| Part | Free service | Notes |
|---|---|---|
| Database | [Neon](https://neon.tech) free Postgres | 0.5 GB; does not expire; choose region **Singapore** (closest to Pakistan) |
| API (+ webhook + workers) | [Render](https://render.com) free web service | HTTPS included. **Sleeps after 15 minutes idle**, and the first request then takes about 50 seconds |
| Desktop app | GitHub Releases (installer built by CI) | Shops download `OrdexaERP-Setup-x.y.z.exe` |

The desktop app is built for the free tier's sleep:
- Billing never waits for the server.
- Sign-in falls back to offline after 25 seconds.
- Sync simply retries.

**Before real use, upgrade to a paid always-on instance.** WhatsApp replies and the AI desk need the server awake.
Meta retries webhooks, so messages are not lost, but replies are delayed.

## 1. Database (Neon)
1. Create a Neon project in region **AWS Asia Pacific (Singapore)**.
2. Copy the connection string, e.g.
   `postgresql://user:pass@ep-xxx.ap-southeast-1.aws.neon.tech/neondb?sslmode=require`

## 2. API (Render)
1. Push this repository to GitHub. On Render choose **New → Blueprint**, pick the repo, and Render reads `render.yaml`.
2. Fill in the secrets Render asks for:
   - **Required:** `DATABASE_URL` (the Neon string).
   - **Optional:** `ANTHROPIC_API_KEY` and the WhatsApp values.
   - `ORDEXA_JWT_SECRET` is generated automatically.
3. Deploy. On start the container runs the database migrations, then serves on Render's `$PORT`. Check
   `https://<your-service>.onrender.com/health`; it should return `{"status":"ok"}`.

## 3. Create the first business
Render's free plan has no shell, so run the platform command from your PC against the Neon database:
```bash
cd backend
set DATABASE_URL=postgresql://user:pass@ep-xxx.../neondb?sslmode=require     # PowerShell: $env:DATABASE_URL="..."
.venv\Scripts\ordexa migrate
.venv\Scripts\ordexa create-tenant --code ali-chemist --name "Ali Chemist" --plan basic --devices 3
# optional demo data: .venv\Scripts\ordexa seed-demo --code ali-chemist
```
The command asks for the administrator password; use a strong one.

## 4. Desktop app
- **Build an installer with your server pre-filled:**
  `set VITE_ORDEXA_SERVER_URL=https://<your-service>.onrender.com && npm run dist` in `desktop`.
  Or tag a release (`git tag v0.1.0 && git push --tags`) and download the installer from the CI artifacts.
- **On each PC:** install, then **Activate this PC** with the server address, business code and administrator login.
- Non-local server addresses must use `https://`.

## 5. WhatsApp (when you have the Meta account)
Follow [ADR 0006](adr/0006-whatsapp-onboarding.md). For the webhook URL use
`https://<your-service>.onrender.com/webhooks/whatsapp`; the API serves it in the single-process setup.

## Paid / production layout
Use `docker-compose.yml`. It runs separate `api`, `webhook`, `worker-ai`, `worker-channels` and `worker-outbox`
containers, so one crashing never stops the others. Point all of them at a managed Postgres with backups.

## Free AI: Groq
Get a free key at https://console.groq.com (no credit card) and set `GROQ_API_KEY` on the server. With it:

| Order type | What happens | Groq model (override with env) |
|---|---|---|
| Text (English / Roman Urdu / Urdu) | Read into order lines (strict JSON) | `openai/gpt-oss-120b` (`ORDEXA_GROQ_MODEL`) |
| Photo of an order list | Read into order lines | `qwen/qwen3.8-27b` (preview; `ORDEXA_GROQ_VISION_MODEL`) |
| Voice note | Transcribed with Whisper, then read like text; the transcript is shown to staff | `whisper-large-v3` (`ORDEXA_GROQ_STT_MODEL`) |

**Limits and safeguards:**
- The free tier is rate-limited, roughly tens of requests per minute. That's enough for a pilot.
- If a request hits the limit or Groq is down, text orders fall back to the built-in parser, and photos and voice
  notes wait for staff on the order screen. Nothing is lost.
- Groq retires models over time. If one stops working, set the env variable to the replacement listed at
  https://console.groq.com/docs/deprecations.
- **Privacy:** order texts, photos and voice notes are sent to Groq for processing. Mention this in your terms with
  shops.

## WhatsApp costs
- **No Meta subscription:** the WhatsApp Cloud API itself has no monthly fee.
- **Test number:** Meta's test number is free, but it can only message up to 5 phone numbers you verify. That's
  enough to pilot with a few shops.
- **Replies may be charged:** Meta charges per message for templates (e.g. the sign-up code). Guides disagree on
  whether replies inside the 24-hour window are still free after 1 October 2026. Check Meta's pricing page for
  Pakistan before going live.
- **Avoid unofficial WhatsApp tools** that log in as WhatsApp Web. They break WhatsApp's terms, and the number can
  be banned.
