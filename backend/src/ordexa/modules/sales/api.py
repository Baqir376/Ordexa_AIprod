from __future__ import annotations

import uuid
from datetime import datetime, time, timedelta

from fastapi import APIRouter, Depends
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from ordexa.modules.identity.service import Principal, requires
from ordexa.modules.sales.models import Invoice
from ordexa.modules.tenants.timeutil import business_today, local_date
from ordexa.shared.db import get_session
from ordexa.shared.errors import ErrorCode, OrdexaError

router = APIRouter(prefix="/api/v1/invoices", tags=["sales"])

SOURCE_LABEL = {"desktop": "Counter", "whatsapp_ai": "WhatsApp", "ai_manual": "Typed order", "api": "API"}


def _row(session: Session, i: Invoice) -> dict:
    return {
        "id": str(i.id),
        "number": i.number,
        "customer_name": i.customer_name,
        "total": str(i.total),
        "paid_amount": str(i.paid_amount),
        "issued_at": i.issued_at.isoformat(),
        "business_date": local_date(session, i.tenant_id, i.issued_at).isoformat(),
        "source": i.source,
        "source_label": SOURCE_LABEL.get(i.source, i.source),
        "status": i.status,
        "line_count": len(i.lines),
        "updated": any(f.get("code") == "items_added" for f in (i.flags or [])),
        "flags": i.flags,
    }


@router.get("")
def list_invoices(
    flagged: bool = False,
    limit: int = 100,
    q: str | None = None,
    today: bool = False,
    p: Principal = Depends(requires("invoice.view")),
    session: Session = Depends(get_session),
):
    """Every bill of the business (all PCs and WhatsApp), newest first; search by number or customer."""
    stmt = (
        select(Invoice).where(Invoice.tenant_id == p.tenant_id).order_by(Invoice.issued_at.desc()).limit(min(max(limit, 1), 500))
    )
    if flagged:
        stmt = stmt.where(func.jsonb_array_length(Invoice.flags) > 0)
    if q:
        like = f"%{q.strip()}%"
        stmt = stmt.where(or_(Invoice.number.ilike(like), Invoice.customer_name.ilike(like)))
    if today:
        d = business_today(session, p.tenant_id)
        start = datetime.combine(d, time.min).astimezone()  # wide window, then exact business-date filter
        stmt = stmt.where(Invoice.issued_at >= start - timedelta(days=1))
        return [r for r in (_row(session, i) for i in session.scalars(stmt)) if r["business_date"] == d.isoformat()]
    return [_row(session, i) for i in session.scalars(stmt)]


@router.get("/{invoice_id}")
def get_invoice(invoice_id: uuid.UUID, p: Principal = Depends(requires("invoice.view")), session: Session = Depends(get_session)):
    inv = session.get(Invoice, invoice_id)
    if inv is None or inv.tenant_id != p.tenant_id:
        raise OrdexaError(ErrorCode.NOT_FOUND, "bill not found")
    return {
        **_row(session, inv),
        "subtotal": str(inv.subtotal),
        "discount": str(inv.discount),
        "lines": [
            {
                "line_no": ln.line_no,
                "product_name": ln.product_name,
                "batch_no": ln.batch_no,
                "quantity": str(ln.quantity),
                "bonus_quantity": str(ln.bonus_quantity),
                "unit": ln.unit,
                "unit_price": str(ln.unit_price),
                "discount": str(ln.discount),
                "line_total": str(ln.line_total),
            }
            for ln in sorted(inv.lines, key=lambda x: x.line_no)
        ],
    }
