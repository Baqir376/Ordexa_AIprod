from __future__ import annotations

import uuid
from decimal import Decimal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ordexa.modules.audit.models import record
from ordexa.modules.catalogue.models import Batch, Product
from ordexa.modules.identity.service import Principal, requires
from ordexa.modules.inventory import service
from ordexa.modules.inventory.models import InventoryTransaction
from ordexa.modules.tenants.models import Branch, Warehouse
from ordexa.shared.db import get_session, lock_master_data
from ordexa.shared.errors import ErrorCode, OrdexaError


class WarehouseIn(BaseModel):
    code: str = Field(min_length=1, max_length=20)
    name: str = Field(min_length=1, max_length=200)


router = APIRouter(prefix="/api/v1/inventory", tags=["inventory"])


class AdjustmentIn(BaseModel):
    id: uuid.UUID  # client-generated; a retried request posts once
    warehouse_id: uuid.UUID
    product_id: uuid.UUID
    batch_id: uuid.UUID | None = None
    qty_delta: Decimal
    kind: str = Field(default="adjustment", pattern="^(opening|adjustment)$")
    reason: str = Field(min_length=3, max_length=300)


@router.get("/balances")
def balances(
    warehouse_id: uuid.UUID | None = None,
    p: Principal = Depends(requires("inventory.view")),
    session: Session = Depends(get_session),
):
    return service.balances(session, tenant_id=p.tenant_id, warehouse_id=warehouse_id)


@router.post("/adjustments", status_code=201)
def adjust(body: AdjustmentIn, p: Principal = Depends(requires("inventory.adjust")), session: Session = Depends(get_session)):
    if session.scalar(
        select(InventoryTransaction).where(
            InventoryTransaction.tenant_id == p.tenant_id,
            InventoryTransaction.source_type == "adjustment",
            InventoryTransaction.source_id == body.id,
        )
    ):
        return {"id": str(body.id), "duplicate": True}
    wh, product = session.get(Warehouse, body.warehouse_id), session.get(Product, body.product_id)
    batch = session.get(Batch, body.batch_id) if body.batch_id else None
    if (
        not wh
        or wh.tenant_id != p.tenant_id
        or not product
        or product.tenant_id != p.tenant_id
        or (body.batch_id and (not batch or batch.product_id != product.id))
    ):
        raise OrdexaError(ErrorCode.NOT_FOUND, "warehouse, product or batch not found")
    if product.track_batches and not batch:
        raise OrdexaError(ErrorCode.VALIDATION_ERROR, "this product is batch-tracked; choose a batch")
    service.post(
        session,
        tenant_id=p.tenant_id,
        warehouse_id=wh.id,
        product_id=product.id,
        batch_id=batch.id if batch else None,
        qty_delta=body.qty_delta,
        kind=body.kind,
        source_type="adjustment",
        source_id=body.id,
        created_by=p.user_id,
        reason=body.reason,
    )
    record(
        session,
        tenant_id=p.tenant_id,
        actor_user_id=p.user_id,
        action="inventory.adjusted",
        entity_type="product",
        entity_id=product.id,
        data={"qty_delta": str(body.qty_delta), "batch_id": str(body.batch_id), "reason": body.reason},
    )
    session.commit()
    return {"id": str(body.id), "duplicate": False}


class TransferIn(BaseModel):
    id: uuid.UUID
    from_warehouse_id: uuid.UUID
    to_warehouse_id: uuid.UUID
    product_id: uuid.UUID
    batch_id: uuid.UUID | None = None
    quantity: Decimal = Field(gt=0, max_digits=14, decimal_places=3)
    note: str | None = Field(default=None, max_length=300)


@router.post("/transfers", status_code=201)
def transfer(body: TransferIn, p: Principal = Depends(requires("inventory.transfer")), session: Session = Depends(get_session)):
    """Store transfer note: stock leaves one warehouse and enters another in one transaction."""
    if session.scalar(
        select(InventoryTransaction).where(
            InventoryTransaction.tenant_id == p.tenant_id,
            InventoryTransaction.source_type == "transfer",
            InventoryTransaction.source_id == body.id,
        )
    ):
        return {"id": str(body.id), "duplicate": True}
    src, dst = session.get(Warehouse, body.from_warehouse_id), session.get(Warehouse, body.to_warehouse_id)
    product = session.get(Product, body.product_id)
    if (
        not src
        or not dst
        or src.tenant_id != p.tenant_id
        or dst.tenant_id != p.tenant_id
        or src.id == dst.id
        or not product
        or product.tenant_id != p.tenant_id
    ):
        raise OrdexaError(ErrorCode.NOT_FOUND, "warehouses or product not found")
    have = service.on_hand(session, tenant_id=p.tenant_id, warehouse_id=src.id, product_id=product.id, batch_id=body.batch_id)
    if have < body.quantity:
        raise OrdexaError(ErrorCode.CONFLICT, f"only {have} available in {src.name}")
    for line, wh, delta in ((1, src, -body.quantity), (2, dst, body.quantity)):
        service.post(
            session,
            tenant_id=p.tenant_id,
            warehouse_id=wh.id,
            product_id=product.id,
            batch_id=body.batch_id,
            qty_delta=delta,
            kind="transfer",
            source_type="transfer",
            source_id=body.id,
            source_line=line,
            created_by=p.user_id,
            reason=body.note,
        )
    record(
        session,
        tenant_id=p.tenant_id,
        actor_user_id=p.user_id,
        action="inventory.transferred",
        entity_type="product",
        entity_id=product.id,
        data={"from": src.code, "to": dst.code, "quantity": str(body.quantity), "batch_id": str(body.batch_id)},
    )
    session.commit()
    return {"id": str(body.id), "duplicate": False}


@router.get("/movements")
def movements(product_id: uuid.UUID, p: Principal = Depends(requires("inventory.view")), session: Session = Depends(get_session)):
    t = InventoryTransaction
    rows = session.scalars(
        select(t).where(t.tenant_id == p.tenant_id, t.product_id == product_id).order_by(t.created_at.desc()).limit(500)
    )
    return [
        {
            "date": r.created_at.isoformat(),
            "kind": r.kind,
            "qty": str(r.qty_delta),
            "batch_id": str(r.batch_id),
            "warehouse_id": str(r.warehouse_id),
            "source": r.source_type,
            "reason": r.reason,
        }
        for r in rows
    ]


@router.post("/warehouses", status_code=201)
def create_warehouse(
    body: WarehouseIn, p: Principal = Depends(requires("settings.configure")), session: Session = Depends(get_session)
):
    lock_master_data(session, p.tenant_id)
    branch = session.scalar(select(Branch).where(Branch.tenant_id == p.tenant_id).order_by(Branch.created_at))
    if session.scalar(select(Warehouse).where(Warehouse.tenant_id == p.tenant_id, Warehouse.code == body.code)):
        raise OrdexaError(ErrorCode.CONFLICT, f"warehouse code '{body.code}' already exists")
    w = Warehouse(tenant_id=p.tenant_id, branch_id=branch.id, code=body.code, name=body.name)
    session.add(w)
    session.flush()
    record(
        session,
        tenant_id=p.tenant_id,
        actor_user_id=p.user_id,
        action="warehouse.created",
        entity_type="warehouse",
        entity_id=w.id,
        data={"code": w.code},
    )
    session.commit()
    return {"id": str(w.id), "code": w.code, "name": w.name}
