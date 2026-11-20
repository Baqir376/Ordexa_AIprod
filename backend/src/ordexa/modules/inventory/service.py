"""Inventory ledger service. Other modules post stock movements only through ``post``."""

from __future__ import annotations

import uuid
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ordexa.modules.inventory.models import InventoryTransaction
from ordexa.shared.money import qty


def post(
    session: Session,
    *,
    tenant_id: uuid.UUID,
    warehouse_id: uuid.UUID,
    product_id: uuid.UUID,
    batch_id: uuid.UUID | None,
    qty_delta: Decimal,
    kind: str,
    source_type: str,
    source_id: uuid.UUID,
    source_line: int = 0,
    created_by: uuid.UUID | None = None,
    reason: str | None = None,
) -> None:
    session.add(
        InventoryTransaction(
            tenant_id=tenant_id,
            warehouse_id=warehouse_id,
            product_id=product_id,
            batch_id=batch_id,
            qty_delta=qty(qty_delta),
            kind=kind,
            source_type=source_type,
            source_id=source_id,
            source_line=source_line,
            created_by=created_by,
            reason=reason,
        )
    )


def on_hand(
    session: Session, *, tenant_id: uuid.UUID, warehouse_id: uuid.UUID, product_id: uuid.UUID, batch_id: uuid.UUID | None
) -> Decimal:
    q = select(func.coalesce(func.sum(InventoryTransaction.qty_delta), 0)).where(
        InventoryTransaction.tenant_id == tenant_id,
        InventoryTransaction.warehouse_id == warehouse_id,
        InventoryTransaction.product_id == product_id,
    )
    q = q.where(InventoryTransaction.batch_id.is_(None) if batch_id is None else InventoryTransaction.batch_id == batch_id)
    return Decimal(session.scalar(q))


def balances(session: Session, *, tenant_id: uuid.UUID, warehouse_id: uuid.UUID | None = None) -> list[dict]:
    t = InventoryTransaction
    q = (
        select(t.warehouse_id, t.product_id, t.batch_id, func.sum(t.qty_delta))
        .where(t.tenant_id == tenant_id)
        .group_by(t.warehouse_id, t.product_id, t.batch_id)
    )
    if warehouse_id is not None:
        q = q.where(t.warehouse_id == warehouse_id)
    return [
        {"warehouse_id": str(w), "product_id": str(p), "batch_id": str(b) if b else None, "on_hand": str(qty(s))}
        for w, p, b, s in session.execute(q)
    ]
