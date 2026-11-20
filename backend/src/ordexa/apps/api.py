"""Main ERP API process. Run: ``uvicorn ordexa.apps.api:app --port 8000``"""

from __future__ import annotations

from fastapi import FastAPI
from sqlalchemy import text

import ordexa.models  # noqa: F401  (register every table so foreign keys resolve)
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


def create_app() -> FastAPI:
    app = FastAPI(title="Ordexa ERP API", version="0.1.0")
    app.add_exception_handler(OrdexaError, ordexa_error_handler)
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
    ):
        app.include_router(router)

    @app.get("/health")
    def health():
        with get_sessionmaker()() as s:
            s.execute(text("SELECT 1"))
        return {"status": "ok", "service": "api"}

    return app


app = create_app()
