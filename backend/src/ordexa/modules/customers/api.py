from __future__ import annotations

import uuid
from decimal import Decimal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from ordexa.modules.audit.models import record
from ordexa.modules.customers.models import Customer
from ordexa.modules.identity.service import Principal, requires
from ordexa.shared.db import get_session, lock_master_data
from ordexa.shared.errors import ErrorCode, OrdexaError

router = APIRouter(prefix="/api/v1/customers", tags=["customers"])


class CustomerIn(BaseModel):
    code: str = Field(min_length=1, max_length=40)
    name: str = Field(min_length=1, max_length=200)
    phone: str | None = Field(default=None, max_length=30)
    address: str | None = Field(default=None, max_length=300)
    credit_limit: Decimal | None = Field(default=None, ge=0, max_digits=14, decimal_places=2)


def customer_out(c: Customer) -> dict:
    return {
        "id": str(c.id),
        "code": c.code,
        "name": c.name,
        "phone": c.phone,
        "address": c.address,
        "credit_limit": str(c.credit_limit) if c.credit_limit is not None else None,
        "is_active": c.is_active,
    }


@router.get("")
def list_customers(q: str = "", p: Principal = Depends(requires("customer.view")), session: Session = Depends(get_session)):
    stmt = select(Customer).where(Customer.tenant_id == p.tenant_id).order_by(Customer.name).limit(200)
    if q:
        like = f"%{q.strip()}%"
        stmt = stmt.where(or_(Customer.name.ilike(like), Customer.code.ilike(like)))
    return [customer_out(c) for c in session.scalars(stmt)]


@router.post("", status_code=201)
def create_customer(
    body: CustomerIn, p: Principal = Depends(requires("customer.create")), session: Session = Depends(get_session)
):
    if body.credit_limit is not None:
        p.require("customer.credit_limit.change")
    lock_master_data(session, p.tenant_id)
    if session.scalar(select(Customer).where(Customer.tenant_id == p.tenant_id, Customer.code == body.code)):
        raise OrdexaError(ErrorCode.CONFLICT, f"customer code '{body.code}' already exists")
    c = Customer(tenant_id=p.tenant_id, **body.model_dump())
    session.add(c)
    session.flush()
    record(
        session,
        tenant_id=p.tenant_id,
        actor_user_id=p.user_id,
        action="customer.created",
        entity_type="customer",
        entity_id=c.id,
        data={"code": body.code},
    )
    session.commit()
    return customer_out(c)


class CustomerUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    phone: str | None = Field(default=None, max_length=30)
    address: str | None = Field(default=None, max_length=300)
    credit_limit: Decimal | None = Field(default=None, ge=0, max_digits=14, decimal_places=2)
    is_active: bool | None = None


@router.put("/{customer_id}")
def update_customer(
    customer_id: uuid.UUID,
    body: CustomerUpdate,
    p: Principal = Depends(requires("customer.update")),
    session: Session = Depends(get_session),
):
    changes = body.model_dump(exclude_unset=True)
    if "credit_limit" in changes:
        p.require("customer.credit_limit.change")
    lock_master_data(session, p.tenant_id)
    c = session.get(Customer, customer_id)
    if not c or c.tenant_id != p.tenant_id:
        raise OrdexaError(ErrorCode.NOT_FOUND, "customer not found")
    before = {k: str(getattr(c, k)) for k in changes}
    for k, v in changes.items():
        setattr(c, k, v)
    record(
        session,
        tenant_id=p.tenant_id,
        actor_user_id=p.user_id,
        action="customer.updated",
        entity_type="customer",
        entity_id=c.id,
        data={"before": before, "after": {k: str(v) for k, v in changes.items()}},
    )
    session.commit()
    return customer_out(c)
