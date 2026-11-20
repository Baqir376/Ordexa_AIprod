"""Products and batches. Master data is edited online only (keeps offline sync conflict-free)."""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from ordexa.modules.audit.models import record
from ordexa.modules.catalogue.models import Batch, Product
from ordexa.modules.identity.service import Principal, requires
from ordexa.shared.db import get_session, lock_master_data
from ordexa.shared.errors import ErrorCode, OrdexaError

router = APIRouter(prefix="/api/v1/products", tags=["catalogue"])


class ProductIn(BaseModel):
    code: str = Field(min_length=1, max_length=40)
    name: str = Field(min_length=1, max_length=200)
    generic_name: str | None = None
    manufacturer: str | None = None
    pack_size: str | None = None
    sale_unit: str = Field(default="box", max_length=20)
    sale_price: Decimal = Field(ge=0, max_digits=14, decimal_places=2)
    barcode: str | None = None
    track_batches: bool = True
    reorder_level: Decimal | None = Field(default=None, ge=0, max_digits=14, decimal_places=3)


class BatchIn(BaseModel):
    batch_no: str = Field(min_length=1, max_length=60)
    expiry_date: date | None = None
    sale_price: Decimal | None = Field(default=None, ge=0, max_digits=14, decimal_places=2)


def product_out(p: Product) -> dict:
    return {
        "id": str(p.id),
        "code": p.code,
        "name": p.name,
        "generic_name": p.generic_name,
        "manufacturer": p.manufacturer,
        "pack_size": p.pack_size,
        "sale_unit": p.sale_unit,
        "sale_price": str(p.sale_price),
        "barcode": p.barcode,
        "track_batches": p.track_batches,
        "reorder_level": str(p.reorder_level) if p.reorder_level is not None else None,
        "is_active": p.is_active,
    }


def batch_out(b: Batch) -> dict:
    return {
        "id": str(b.id),
        "product_id": str(b.product_id),
        "batch_no": b.batch_no,
        "expiry_date": b.expiry_date.isoformat() if b.expiry_date else None,
        "sale_price": str(b.sale_price) if b.sale_price is not None else None,
        "is_active": b.is_active,
    }


@router.get("")
def list_products(q: str = "", p: Principal = Depends(requires("product.view")), session: Session = Depends(get_session)):
    stmt = select(Product).where(Product.tenant_id == p.tenant_id).order_by(Product.name).limit(200)
    if q:
        like = f"%{q.strip()}%"
        stmt = stmt.where(or_(Product.name.ilike(like), Product.code.ilike(like), Product.barcode == q.strip()))
    return [product_out(x) for x in session.scalars(stmt)]


@router.post("", status_code=201)
def create_product(body: ProductIn, p: Principal = Depends(requires("product.manage")), session: Session = Depends(get_session)):
    lock_master_data(session, p.tenant_id)
    if session.scalar(select(Product).where(Product.tenant_id == p.tenant_id, Product.code == body.code)):
        raise OrdexaError(ErrorCode.CONFLICT, f"product code '{body.code}' already exists")
    product = Product(tenant_id=p.tenant_id, **body.model_dump())
    session.add(product)
    session.flush()
    record(
        session,
        tenant_id=p.tenant_id,
        actor_user_id=p.user_id,
        action="product.created",
        entity_type="product",
        entity_id=product.id,
        data={"code": body.code},
    )
    session.commit()
    return product_out(product)


@router.post("/{product_id}/batches", status_code=201)
def create_batch(
    product_id: uuid.UUID,
    body: BatchIn,
    p: Principal = Depends(requires("product.manage")),
    session: Session = Depends(get_session),
):
    lock_master_data(session, p.tenant_id)
    product = session.get(Product, product_id)
    if not product or product.tenant_id != p.tenant_id:
        raise OrdexaError(ErrorCode.NOT_FOUND, "product not found")
    if session.scalar(select(Batch).where(Batch.product_id == product.id, Batch.batch_no == body.batch_no)):
        raise OrdexaError(ErrorCode.CONFLICT, f"batch '{body.batch_no}' already exists for this product")
    batch = Batch(tenant_id=p.tenant_id, product_id=product.id, **body.model_dump())
    session.add(batch)
    session.flush()
    record(
        session,
        tenant_id=p.tenant_id,
        actor_user_id=p.user_id,
        action="batch.created",
        entity_type="batch",
        entity_id=batch.id,
        data={"product_id": str(product.id), "batch_no": body.batch_no},
    )
    session.commit()
    return batch_out(batch)


class ProductUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    generic_name: str | None = None
    manufacturer: str | None = None
    pack_size: str | None = None
    sale_unit: str | None = Field(default=None, max_length=20)
    sale_price: Decimal | None = Field(default=None, ge=0, max_digits=14, decimal_places=2)
    barcode: str | None = None
    reorder_level: Decimal | None = Field(default=None, ge=0, max_digits=14, decimal_places=3)
    is_active: bool | None = None


@router.put("/{product_id}")
def update_product(
    product_id: uuid.UUID,
    body: ProductUpdate,
    p: Principal = Depends(requires("product.manage")),
    session: Session = Depends(get_session),
):
    lock_master_data(session, p.tenant_id)
    product = session.get(Product, product_id)
    if not product or product.tenant_id != p.tenant_id:
        raise OrdexaError(ErrorCode.NOT_FOUND, "product not found")
    changes = body.model_dump(exclude_unset=True)
    before = {k: str(getattr(product, k)) for k in changes}
    for k, v in changes.items():
        setattr(product, k, v)
    record(
        session,
        tenant_id=p.tenant_id,
        actor_user_id=p.user_id,
        action="product.updated",
        entity_type="product",
        entity_id=product.id,
        data={"before": before, "after": {k: str(v) for k, v in changes.items()}},
    )
    session.commit()
    return product_out(product)


@router.get("/{product_id}/batches")
def list_batches(
    product_id: uuid.UUID, p: Principal = Depends(requires("product.view")), session: Session = Depends(get_session)
):
    rows = session.scalars(
        select(Batch).where(Batch.tenant_id == p.tenant_id, Batch.product_id == product_id).order_by(Batch.expiry_date)
    )
    return [batch_out(b) for b in rows]
