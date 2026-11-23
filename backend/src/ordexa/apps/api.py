"""Main ERP API process. Run: ``ordexa serve`` (migrates, then serves on $PORT) or
``uvicorn ordexa.apps.api:app --port 8000``."""

from __future__ import annotations

import logging
import threading
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text

import ordexa.models  # noqa: F401  (register every table so foreign keys resolve)
from ordexa.apps.webhook import router as webhooks
from ordexa.config import get_settings
from ordexa.modules.ai.api import router as ai
from ordexa.modules.catalogue.api import router as catalogue
from ordexa.modules.channels.api import router as channels
from ordexa.modules.customers.api import router as customers
from ordexa.modules.devices.api import router as devices
from ordexa.modules.finance.api import router as finance
from ordexa.modules.identity.api import router as identity
from ordexa.modules.inventory.api import router as inventory
from ordexa.modules.purchasing.api import router as purchasing
from ordexa.modules.reports.api import router as reports
from ordexa.modules.sales.api import router as sales
from ordexa.modules.sync.api import router as sync
from ordexa.modules.tenants.api import router as tenants
from ordexa.modules.tenants.backup import router as backup
from ordexa.shared.db import get_sessionmaker
from ordexa.shared.errors import OrdexaError, ordexa_error_handler

log = logging.getLogger("ordexa.api")


@asynccontextmanager
async def lifespan(app: FastAPI):
    stop = threading.Event()
    if get_settings().embedded_workers:
        from ordexa.apps.worker import start_embedded

        start_embedded(stop)
        log.info("embedded workers started (ai, channels, outbox)")
    yield
    stop.set()


async def unexpected_error(request: Request, exc: Exception) -> JSONResponse:
    """Never leak stack traces to clients; log them with the correlation id instead."""
    cid = request.headers.get("x-correlation-id")
    log.exception("unhandled error on %s %s (correlation %s)", request.method, request.url.path, cid)
    return JSONResponse(
        status_code=500,
        content={
            "error": {
                "code": "INTERNAL_ERROR",
                "message": "Something went wrong on the server. Please try again.",
                "details": {},
                "correlation_id": cid,
            }
        },
    )


def create_app() -> FastAPI:
    app = FastAPI(title="Ordexa ERP API", version="0.1.0", lifespan=lifespan)
    app.add_exception_handler(OrdexaError, ordexa_error_handler)
    app.add_exception_handler(Exception, unexpected_error)
    for router in (
        identity,
        tenants,
        devices,
        catalogue,
        customers,
        inventory,
        sales,
        sync,
        purchasing,
        finance,
        reports,
        ai,
        channels,
        backup,
        webhooks,
    ):
        app.include_router(router)

    @app.get("/health")
    def health():
        with get_sessionmaker()() as s:
            s.execute(text("SELECT 1"))
        return {"status": "ok", "service": "api"}

    return app


app = create_app()
