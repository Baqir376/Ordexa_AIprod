from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from ordexa.modules.audit.models import record
from ordexa.modules.finance import service as finance
from ordexa.modules.identity.service import Principal, requires
from ordexa.modules.purchasing import service
from ordexa.modules.purchasing.models import PurchaseInvoice, Supplier
from ordexa.shared.db import get_session
from ordexa.shared.errors import ErrorCode, OrdexaError

router = APIRouter(prefix="/api/v1", tags=["purchasing"])

D2 = {"max_digits": 14, "decimal_places": 2}
D3 = {"max_digits": 14, "decimal_places": 3}


class SupplierIn(BaseModel):
    code: str = Field(min_length=1, max_length=40)
    name: str = Field(min_length=1, max_length=200)
    phone: str | None = Field(default=None, max_length=30)
    address: str | None = Field(default=None, max_length=300)
    ntn: str | None = Field(default=None, max_length=30)
    is_active: bool = True


class PurchaseLineBody(BaseModel):
    product_id: uuid.UUID
    quantity: Decimal = Field(gt=0, **D3)
    bonus_quantity: Decimal = Field(default=Decimal(0), ge=0, **D3)
    unit_cost: Decimal = Field(ge=0, **D2)
    discount: Decimal = Field(default=Decimal(0), ge=0, **D2)
    batch_no: str | None = Field(default=None, max_length=60)
    expiry_date: date | None = None
    sale_price: Decimal | None = Field(default=None, ge=0, **D2)


class PurchaseBody(BaseModel):
    id: uuid.UUID
    supplier_id: uuid.UUID
    supplier_bill_no: str | None = Field(default=None, max_length=60)
    bill_date: date
    warehouse_id: uuid.UUID | None = None
    discount: Decimal = Field(default=Decimal(0), ge=0, **D2)
    paid_amount: Decimal = Field(default=Decimal(0), ge=0, **D2)
    payment_method: str = Field(default="cash", pattern="^(cash|bank|cheque)$")
    lines: list[PurchaseLineBody] = Field(min_length=1, max_length=500)


def supplier_out(s: Supplier, balance: Decimal | None = None) -> dict:
    return {
        "id": str(s.id),
        "code": s.code,
        "name": s.name,
        "phone": s.phone,
        "address": s.address,
        "ntn": s.ntn,
        "is_active": s.is_active,
        "balance": str(balance) if balance is not None else None,
    }


@router.get("/suppliers")
def list_suppliers(q: str = "", p: Principal = Depends(requires("purchase_order.view")), session: Session = Depends(get_session)):
    stmt = select(Supplier).where(Supplier.tenant_id == p.tenant_id).order_by(Supplier.name).limit(500)
    if q:
        stmt = stmt.where(or_(Supplier.name.ilike(f"%{q}%"), Supplier.code.ilike(f"%{q}%")))
    bal = finance.balances(session, p.tenant_id, "supplier")
    return [supplier_out(s, bal.get(s.id, Decimal("0.00"))) for s in session.scalars(stmt)]


@router.post("/suppliers", status_code=201)
def create_supplier(
    body: SupplierIn, p: Principal = Depends(requires("supplier.manage")), session: Session = Depends(get_session)
):
    if session.scalar(select(Supplier).where(Supplier.tenant_id == p.tenant_id, Supplier.code == body.code)):
        raise OrdexaError(ErrorCode.CONFLICT, f"supplier code '{body.code}' already exists")
    s = Supplier(tenant_id=p.tenant_id, **body.model_dump())
    session.add(s)
    session.flush()
    record(
        session,
        tenant_id=p.tenant_id,
        actor_user_id=p.user_id,
        action="supplier.created",
        entity_type="supplier",
        entity_id=s.id,
        data={"code": s.code},
    )
    session.commit()
    return supplier_out(s)


@router.put("/suppliers/{supplier_id}")
def update_supplier(
    supplier_id: uuid.UUID,
    body: SupplierIn,
    p: Principal = Depends(requires("supplier.manage")),
    session: Session = Depends(get_session),
):
    s = session.get(Supplier, supplier_id)
    if not s or s.tenant_id != p.tenant_id:
        raise OrdexaError(ErrorCode.NOT_FOUND, "supplier not found")
    for k, v in body.model_dump().items():
        setattr(s, k, v)
    record(
        session, tenant_id=p.tenant_id, actor_user_id=p.user_id, action="supplier.updated", entity_type="supplier", entity_id=s.id
    )
    session.commit()
    return supplier_out(s)


@router.get("/purchases")
def list_purchases(
    limit: int = 100, p: Principal = Depends(requires("purchase_order.view")), session: Session = Depends(get_session)
):
    rows = session.execute(
        select(PurchaseInvoice, Supplier.name)
        .join(Supplier, Supplier.id == PurchaseInvoice.supplier_id)
        .where(PurchaseInvoice.tenant_id == p.tenant_id)
        .order_by(PurchaseInvoice.bill_date.desc(), PurchaseInvoice.number.desc())
        .limit(min(max(limit, 1), 500))
    )
    return [service.purchase_out(pi, name) for pi, name in rows]


@router.post("/purchases", status_code=201)
def post_purchase(
    body: PurchaseBody, p: Principal = Depends(requires("goods_receipt.create")), session: Session = Depends(get_session)
):
    pi, created = service.post_purchase(
        session,
        p,
        service.PurchaseIn(
            id=body.id,
            supplier_id=body.supplier_id,
            bill_date=body.bill_date,
            supplier_bill_no=body.supplier_bill_no,
            warehouse_id=body.warehouse_id,
            discount=body.discount,
            paid_amount=body.paid_amount,
            payment_method=body.payment_method,
            lines=[service.PurchaseLineIn(**ln.model_dump()) for ln in body.lines],
        ),
    )
    session.commit()
    return {**service.purchase_out(pi), "duplicate": not created}
