# ADR 0001: Cloud backend + Windows desktop app; separate processes per concern

**Status:** Accepted · 2026-11-17

## Context
Pakistani distributors and chemists run desktop ERPs (4M, Soft Team): installed on Windows, branded, fast
keyboard billing, local printing, and they keep working through load-shedding and internet drops. Ordexa also
needs a WhatsApp webhook and LLM processing, which require a public, always-on server. Shop PCs behind home
routers cannot receive webhooks.

## Decision
1. **One central backend** (Python FastAPI + PostgreSQL), multi-tenant, hosted in a cloud region near
   Pakistan.
2. **Windows desktop app**: Electron + React/TypeScript. The same React UI can later run in a browser for
   owners and managers. We chose Electron over Tauri for mature silent printing to named thermal and
   dot-matrix printers, and for an all-TypeScript client.
3. **A modular monolith with process isolation.** It is one codebase with strict module boundaries
   (`backend/src/ordexa/modules/*`), deployed as separate processes:

   | Process | Job | If it crashes |
   |---|---|---|
   | `api` | ERP API, desktop sync | Desktops keep billing offline |
   | `webhook` | Verify WhatsApp signature, enqueue | Meta retries; nothing else affected |
   | `worker --queue ai` | LLM order understanding (Phase 2) | Orders wait in the queue; billing unaffected |
   | `worker --queue channels` | WhatsApp registration bot (Phase 3) | Messages wait in the queue |
   | `worker --queue outbox` | Outbound WhatsApp, retries | Messages wait in the queue |

4. **The job queue is a PostgreSQL table** (`FOR UPDATE SKIP LOCKED`, unique idempotency key, exponential
   backoff, dead-letter after `max_attempts`). This means no Redis or broker to run until volume demands
   it.

## Consequences
- An LLM or WhatsApp outage can never stop counter billing (SRS rule 6.3-12).
- Each worker scales and restarts independently (`docker-compose.yml`, `restart: unless-stopped`).
- Moving a module to its own service later is a deployment change, because modules only talk through
  services and APIs.
