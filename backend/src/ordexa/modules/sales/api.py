from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ordexa.modules.identity.service import Principal, requires
from ordexa.modules.sales.models import Invoice
from ordexa.shared.db import get_session

router = APIRouter(prefix="/api/v1/invoices", tags=["sales"])


@router.get("")
def list_invoices(
    flagged: bool = False,
    limit: int = 100,
    p: Principal = Depends(requires("invoice.view")),
    session: Session = Depends(get_session),
):
    stmt = (
        select(Invoice).where(Invoice.tenant_id == p.tenant_id).order_by(Invoice.issued_at.desc()).limit(min(max(limit, 1), 500))
    )
    if flagged:
        stmt = stmt.where(func.jsonb_array_length(Invoice.flags) > 0)
    return [
        {
            "id": str(i.id),
            "number": i.number,
            "customer_name": i.customer_name,
            "total": str(i.total),
            "paid_amount": str(i.paid_amount),
            "issued_at": i.issued_at.isoformat(),
            "source": i.source,
            "flags": i.flags,
        }
        for i in session.scalars(stmt)
    ]
