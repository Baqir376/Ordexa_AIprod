"""Owner backup: a complete export of one business's data (no password hashes), downloadable any time.
The desktop saves one automatically every day on the owner's PC (4M "Auto Backup")."""

from __future__ import annotations

import datetime as dt
import decimal
import uuid

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

import ordexa.models  # noqa: F401
from ordexa.modules.audit.models import record
from ordexa.modules.identity.service import Principal, requires
from ordexa.modules.tenants.models import Tenant
from ordexa.shared.db import Base, get_session, utcnow

router = APIRouter(prefix="/api/v1/backup", tags=["backup"])

SKIP_COLUMNS = {"users": {"password_hash"}, "whatsapp_contacts": {"otp_hash"}}
# Child tables without tenant_id: (table, foreign key column, parent table)
CHILDREN = [
    ("invoice_lines", "invoice_id", "invoices"),
    ("purchase_lines", "purchase_id", "purchase_invoices"),
    ("credit_note_lines", "credit_note_id", "credit_notes"),
    ("user_permissions", "user_id", "users"),
]


def _jsonable(v):
    if isinstance(v, uuid.UUID):
        return str(v)
    if isinstance(v, decimal.Decimal):
        return str(v)
    if isinstance(v, dt.date | dt.datetime):
        return v.isoformat()
    return v


@router.get("/export")
def export(p: Principal = Depends(requires("settings.configure")), session: Session = Depends(get_session)):
    tables = Base.metadata.tables
    out: dict[str, list[dict]] = {}
    for name, table in tables.items():
        if name in ("tenants", "jobs", "document_sequences") or "tenant_id" not in table.c:
            continue
        rows = session.execute(select(table).where(table.c.tenant_id == p.tenant_id)).mappings()
        out[name] = [{k: _jsonable(v) for k, v in r.items() if k not in SKIP_COLUMNS.get(name, set())} for r in rows]
    for child, fk, parent in CHILDREN:
        parent_ids = [r["id"] for r in out.get(parent, [])]
        t = tables[child]
        rows = session.execute(select(t).where(t.c[fk].in_(parent_ids))).mappings() if parent_ids else []
        out[child] = [{k: _jsonable(v) for k, v in r.items()} for r in rows]
    tenant = session.get(Tenant, p.tenant_id)
    record(
        session,
        tenant_id=p.tenant_id,
        actor_user_id=p.user_id,
        action="backup.exported",
        entity_type="tenant",
        entity_id=p.tenant_id,
        data={"tables": len(out), "rows": sum(len(v) for v in out.values())},
    )
    session.commit()
    return {
        "format": "ordexa-backup/1",
        "exported_at": utcnow().isoformat(),
        "business": {"code": tenant.code, "name": tenant.name},
        "tables": out,
    }
