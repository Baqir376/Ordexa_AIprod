"""WhatsApp webhook receiver: a separate, tiny process. It only verifies the signature and durably enqueues
the event, so it stays up even if the API or the AI worker is down, and Meta always gets a fast 200.

Run: ``uvicorn ordexa.apps.webhook:app --port 8001``
"""

from __future__ import annotations

import hashlib
import hmac

from fastapi import FastAPI, Query, Request
from fastapi.responses import JSONResponse, PlainTextResponse

import ordexa.models  # noqa: F401  (register every table so foreign keys resolve)
from ordexa.config import get_settings
from ordexa.modules.jobs.service import enqueue
from ordexa.shared.db import get_sessionmaker

app = FastAPI(title="Ordexa webhook receiver", docs_url=None, redoc_url=None)


def valid_signature(raw: bytes, header: str | None, secret: str) -> bool:
    if not secret or not header or not header.startswith("sha256="):
        return False
    expected = hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header.split("=", 1)[1].strip())


@app.get("/health")
def health():
    return {"status": "ok", "service": "webhook"}


@app.get("/webhooks/whatsapp")
def verify(
    mode: str = Query("", alias="hub.mode"),
    token: str = Query("", alias="hub.verify_token"),
    challenge: str = Query("", alias="hub.challenge"),
):
    expected = get_settings().whatsapp_verify_token
    if expected and mode == "subscribe" and hmac.compare_digest(token, expected):
        return PlainTextResponse(challenge)
    return PlainTextResponse("forbidden", status_code=403)


@app.post("/webhooks/whatsapp")
async def receive(request: Request):
    raw = await request.body()
    if not valid_signature(raw, request.headers.get("x-hub-signature-256"), get_settings().whatsapp_app_secret):
        return JSONResponse({"error": "invalid signature"}, status_code=401)
    payload = await request.json()
    enqueued = 0
    with get_sessionmaker()() as session:
        for entry in payload.get("entry") or []:
            for change in entry.get("changes") or []:
                value = change.get("value") or {}
                number_id = (value.get("metadata") or {}).get("phone_number_id")
                for msg in value.get("messages") or []:
                    wamid = msg.get("id")
                    if wamid and enqueue(
                        session,
                        kind="whatsapp.inbound",
                        queue="channels",
                        idempotency_key=f"wa:{wamid}",
                        payload={"wamid": wamid, "phone_number_id": number_id, "message": msg},
                    ):
                        enqueued += 1
        session.commit()
    return {"received": True, "enqueued": enqueued}
