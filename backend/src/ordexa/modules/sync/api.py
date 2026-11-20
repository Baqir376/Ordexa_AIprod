"""Desktop sync protocol (see docs/adr/0003-offline-sync.md).

* pull: master data changed since ``cursor`` (products, batches, customers, branding) plus full snapshots of
  this PC's stock and customer balances. The desktop upserts and stores the returned cursor.
* push: invoices created on the desktop. Each is posted in its own savepoint; replays are ``duplicate``.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from fastapi import APIRouter, Depends, Header
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ordexa.modules.catalogue.api import batch_out, product_out
from ordexa.modules.catalogue.models import Batch, Product
from ordexa.modules.customers.api import customer_out
from ordexa.modules.customers.models import Customer
from ordexa.modules.devices.service import device_for
from ordexa.modules.finance import service as finance
from ordexa.modules.identity.service import Principal, current_principal
from ordexa.modules.inventory import service as inventory
from ordexa.modules.sales import service as sales
from ordexa.modules.tenants.models import Tenant
from ordexa.modules.tenants.service import branding
from ordexa.shared.db import get_session, utcnow
from ordexa.shared.errors import OrdexaError
from ordexa.shared.money import money

router = APIRouter(prefix="/api/v1/sync", tags=["sync"])

MAX_PUSH = 200


class LineBody(BaseModel):
    line_no: int = Field(ge=1)
    product_id: uuid.UUID
    batch_id: uuid.UUID | None = None
    quantity: Decimal = Field(gt=0, max_digits=14, decimal_places=3)
    bonus_quantity: Decimal = Field(default=Decimal(0), ge=0, max_digits=14, decimal_places=3)
    unit_price: Decimal = Field(ge=0, max_digits=14, decimal_places=2)
    discount: Decimal = Field(default=Decimal(0), ge=0, max_digits=14, decimal_places=2)


class InvoiceBody(BaseModel):
    id: uuid.UUID
    number: str = Field(min_length=3, max_length=30)
    customer_id: uuid.UUID | None = None
    walk_in_name: str | None = Field(default=None, max_length=200)
    issued_at: datetime
    discount: Decimal = Field(default=Decimal(0), ge=0, max_digits=14, decimal_places=2)
    paid_amount: Decimal = Field(default=Decimal(0), ge=0, max_digits=14, decimal_places=2)
    total: Decimal | None = Field(default=None, max_digits=14, decimal_places=2)
    lines: list[LineBody] = Field(min_length=1, max_length=500)


class ReceiptBody(BaseModel):
    """Cash collected from a shop at the counter (offline-capable). Number uses this PC's series: T1R-000001."""

    id: uuid.UUID
    number: str = Field(min_length=3, max_length=30)
    customer_id: uuid.UUID
    amount: Decimal = Field(gt=0, max_digits=14, decimal_places=2)
    received_at: datetime
    note: str | None = Field(default=None, max_length=300)


class PushBody(BaseModel):
    invoices: list[InvoiceBody] = Field(default_factory=list, max_length=MAX_PUSH)
    receipts: list[ReceiptBody] = Field(default_factory=list, max_length=MAX_PUSH)


@router.get("/pull")
def pull(
    cursor: int = 0,
    x_device_id: str | None = Header(default=None),
    p: Principal = Depends(current_principal),
    session: Session = Depends(get_session),
):
    device = device_for(session, p, x_device_id)
    tenant = session.get(Tenant, p.tenant_id)

    def changed(model):
        return list(
            session.scalars(
                select(model).where(model.tenant_id == p.tenant_id, model.change_seq > cursor).order_by(model.change_seq)
            )
        )

    products, batches, customers = changed(Product), changed(Batch), changed(Customer)
    seqs = [r.change_seq for r in (*products, *batches, *customers)]
    if tenant.change_seq > cursor:
        seqs.append(tenant.change_seq)
    balances = finance.balances(session, p.tenant_id, "customer")
    return {
        "cursor": max(seqs, default=cursor),
        "server_time": utcnow().isoformat(),
        "device": {"id": str(device.id), "terminal_code": device.terminal_code, "warehouse_id": str(device.warehouse_id)},
        "branding": branding(tenant) if tenant.change_seq > cursor else None,
        "products": [product_out(x) for x in products],
        "batches": [batch_out(x) for x in batches],
        "customers": [customer_out(x) for x in customers],
        # Full snapshots: small for a pilot, and always correct regardless of cursor.
        "stock": inventory.balances(session, tenant_id=p.tenant_id, warehouse_id=device.warehouse_id),
        "customer_balances": {str(k): str(money(v)) for k, v in balances.items()},
    }


@router.post("/push")
def push(
    body: PushBody,
    x_device_id: str | None = Header(default=None),
    p: Principal = Depends(current_principal),
    session: Session = Depends(get_session),
):
    device = device_for(session, p, x_device_id)
    results = []
    for b in body.invoices:
        inv = sales.InvoiceIn(
            id=b.id,
            number=b.number.strip(),
            customer_id=b.customer_id,
            issued_at=b.issued_at,
            discount=b.discount,
            paid_amount=b.paid_amount,
            client_total=b.total,
            walk_in_name=b.walk_in_name,
            lines=[
                sales.LineIn(
                    line_no=ln.line_no,
                    product_id=ln.product_id,
                    batch_id=ln.batch_id,
                    quantity=ln.quantity,
                    bonus_quantity=ln.bonus_quantity,
                    unit_price=ln.unit_price,
                    discount=ln.discount,
                )
                for ln in b.lines
            ],
        )
        with session.begin_nested():  # one bad invoice never blocks the rest of the batch
            r = sales.post_invoice(session, p, device, inv)
        results.append(r.__dict__)
    receipt_results = []
    for r in body.receipts:
        if not r.number.startswith(f"{device.terminal_code}R-"):
            receipt_results.append(
                {
                    "id": str(r.id),
                    "number": r.number,
                    "status": "rejected",
                    "reason": f"receipt number must use series {device.terminal_code}R-",
                }
            )
            continue
        try:
            with session.begin_nested():
                _, created = finance.record_payment(
                    session,
                    p,
                    finance.PaymentIn(
                        id=r.id,
                        direction="in",
                        party_id=r.customer_id,
                        amount=r.amount,
                        method="cash",
                        payment_date=r.received_at.date(),
                        note=r.note,
                    ),
                    device_id=device.id,
                    number=r.number,
                )
            receipt_results.append(
                {"id": str(r.id), "number": r.number, "status": "accepted" if created else "duplicate", "reason": None}
            )
        except OrdexaError as e:
            receipt_results.append({"id": str(r.id), "number": r.number, "status": "rejected", "reason": e.message})
    device.last_sync_at = utcnow()
    session.commit()
    return {"results": results, "receipts": receipt_results}
