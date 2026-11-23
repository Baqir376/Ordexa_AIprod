"""WhatsApp webhook receiver. It only verifies the signature and durably enqueues the event, so Meta always
gets a fast 200 and processing happens in the channels worker.

Deploy it either as its own tiny process (``uvicorn ordexa.apps.webhook:app --port 8001``, see docker-compose) so it
stays up even if the API is down, or mounted inside the API (the API always includes ``router``) for single-process
hosting.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging

from fastapi import APIRouter, FastAPI, Query, Request
from fastapi.responses import JSONResponse, PlainTextResponse

import ordexa.models  # noqa: F401  (register every table so foreign keys resolve)
from ordexa.config import get_settings
from ordexa.modules.jobs.service import enqueue
from ordexa.shared.db import get_sessionmaker

log = logging.getLogger("ordexa.webhook")
router = APIRouter(tags=["webhooks"])


def valid_signature(raw: bytes, header: str | None, secret: str) -> bool:
    if not secret or not header or not header.startswith("sha256="):
        return False
    expected = hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header.split("=", 1)[1].strip())


@router.get("/webhooks/whatsapp")
def verify(
    mode: str = Query("", alias="hub.mode"),
    token: str = Query("", alias="hub.verify_token"),
    challenge: str = Query("", alias="hub.challenge"),
):
    expected = get_settings().whatsapp_verify_token
    if expected and mode == "subscribe" and hmac.compare_digest(token, expected):
        return PlainTextResponse(challenge)
    return PlainTextResponse("forbidden", status_code=403)


@router.post("/webhooks/whatsapp")
async def receive(request: Request):
    raw = await request.body()
    if not valid_signature(raw, request.headers.get("x-hub-signature-256"), get_settings().whatsapp_app_secret):
        return JSONResponse({"error": "invalid signature"}, status_code=401)
    try:
        payload = json.loads(raw)
    except ValueError:
        return JSONResponse({"error": "invalid JSON"}, status_code=400)
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


app = FastAPI(title="Ordexa webhook receiver", docs_url=None, redoc_url=None)
app.include_router(router)


@app.get("/health")
def health():
    return {"status": "ok", "service": "webhook"}
