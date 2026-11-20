"""Suppliers and purchase invoices (goods received). Posting a purchase creates or updates batches,
adds stock, records cost price, and posts the supplier payable."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from ordexa.modules.audit.models import record
from ordexa.modules.catalogue.models import Batch, Product
from ordexa.modules.finance import service as finance
from ordexa.modules.identity.service import Principal
from ordexa.modules.inventory import service as inventory
from ordexa.modules.purchasing.models import PurchaseInvoice, PurchaseLine, Supplier
from ordexa.modules.tenants.models import Warehouse
from ordexa.shared.db import lock_master_data
from ordexa.shared.errors import ErrorCode, OrdexaError
from ordexa.shared.money import line_total, money, qty
from ordexa.shared.sequences import next_number


@dataclass
class PurchaseLineIn:
    product_id: uuid.UUID
    quantity: Decimal
    unit_cost: Decimal
    batch_no: str | None = None
    expiry_date: date | None = None
    bonus_quantity: Decimal = Decimal(0)
    discount: Decimal = Decimal(0)
    sale_price: Decimal | None = None  # set/refresh the batch selling price


@dataclass
class PurchaseIn:
    id: uuid.UUID
    supplier_id: uuid.UUID
    bill_date: date
    lines: list[PurchaseLineIn]
    supplier_bill_no: str | None = None
    warehouse_id: uuid.UUID | None = None
    discount: Decimal = Decimal(0)
    paid_amount: Decimal = Decimal(0)
    payment_method: str = "cash"
    extra: dict = field(default_factory=dict)


def post_purchase(session: Session, actor: Principal, data: PurchaseIn) -> tuple[PurchaseInvoice, bool]:
    actor.require("goods_receipt.create")
    existing = session.get(PurchaseInvoice, data.id)
    if existing:
        if existing.tenant_id != actor.tenant_id:
            raise OrdexaError(ErrorCode.CONFLICT, "purchase id is not available")
        return existing, False
    lock_master_data(session, actor.tenant_id)  # batches are synced master data
    supplier = session.get(Supplier, data.supplier_id)
    if not supplier or supplier.tenant_id != actor.tenant_id:
        raise OrdexaError(ErrorCode.NOT_FOUND, "supplier not found")
    if data.warehouse_id:
        wh = session.get(Warehouse, data.warehouse_id)
    else:
        wh = session.scalar(select(Warehouse).where(Warehouse.tenant_id == actor.tenant_id).order_by(Warehouse.created_at))
    if not wh or wh.tenant_id != actor.tenant_id:
        raise OrdexaError(ErrorCode.NOT_FOUND, "warehouse not found")
    if not data.lines:
        raise OrdexaError(ErrorCode.VALIDATION_ERROR, "a purchase needs at least one line")

    pi = PurchaseInvoice(
        id=data.id,
        tenant_id=actor.tenant_id,
        number=next_number(session, actor.tenant_id, "PI"),
        supplier_id=supplier.id,
        supplier_bill_no=data.supplier_bill_no,
        warehouse_id=wh.id,
        bill_date=data.bill_date,
        subtotal=Decimal(0),
        discount=money(data.discount),
        total=Decimal(0),
        paid_amount=money(data.paid_amount),
        created_by=actor.user_id,
    )
    subtotal = Decimal(0)
    for i, ln in enumerate(data.lines, start=1):
        product = session.get(Product, ln.product_id)
        if not product or product.tenant_id != actor.tenant_id:
            raise OrdexaError(ErrorCode.NOT_FOUND, f"line {i}: product not found")
        if qty(ln.quantity) <= 0 or money(ln.unit_cost) < 0 or qty(ln.bonus_quantity) < 0:
            raise OrdexaError(ErrorCode.VALIDATION_ERROR, f"line {i}: invalid quantity or cost")
        batch = None
        if product.track_batches:
            if not ln.batch_no or not ln.expiry_date:
                raise OrdexaError(ErrorCode.VALIDATION_ERROR, f"line {i}: {product.name} needs batch number and expiry")
            batch = session.scalar(select(Batch).where(Batch.product_id == product.id, Batch.batch_no == ln.batch_no.strip()))
            if batch is None:
                batch = Batch(
                    tenant_id=actor.tenant_id, product_id=product.id, batch_no=ln.batch_no.strip(), expiry_date=ln.expiry_date
                )
                session.add(batch)
            elif batch.expiry_date != ln.expiry_date:
                raise OrdexaError(
                    ErrorCode.CONFLICT, f"line {i}: batch {ln.batch_no} already exists with expiry {batch.expiry_date}"
                )
            batch.cost_price = money(ln.unit_cost)
            batch.is_active = True
            if ln.sale_price is not None:
                batch.sale_price = money(ln.sale_price)
            session.flush()
        lt = line_total(ln.quantity, ln.unit_cost, ln.discount)
        subtotal += lt
        pi.lines.append(
            PurchaseLine(
                line_no=i,
                product_id=product.id,
                batch_id=batch.id if batch else None,
                product_name=product.name,
                batch_no=batch.batch_no if batch else None,
                expiry_date=batch.expiry_date if batch else None,
                quantity=qty(ln.quantity),
                bonus_quantity=qty(ln.bonus_quantity),
                unit_cost=money(ln.unit_cost),
                discount=money(ln.discount),
                line_total=lt,
            )
        )
    pi.subtotal = money(subtotal)
    pi.total = money(subtotal - pi.discount)
    if pi.total < 0 or pi.paid_amount < 0 or pi.paid_amount > pi.total:
        raise OrdexaError(ErrorCode.VALIDATION_ERROR, "invalid discount or paid amount")
    session.add(pi)
    session.flush()
    for ln in pi.lines:
        inventory.post(
            session,
            tenant_id=actor.tenant_id,
            warehouse_id=wh.id,
            product_id=ln.product_id,
            batch_id=ln.batch_id,
            qty_delta=ln.quantity + ln.bonus_quantity,
            kind="receipt",
            source_type="purchase",
            source_id=pi.id,
            source_line=ln.line_no,
            created_by=actor.user_id,
        )
    finance.post_ledger(
        session,
        tenant_id=actor.tenant_id,
        party_type="supplier",
        party_id=supplier.id,
        amount=pi.total,
        kind="purchase",
        source_id=pi.id,
        reference=pi.number,
        entry_date=pi.bill_date,
        memo=f"bill {data.supplier_bill_no}" if data.supplier_bill_no else None,
    )
    if pi.paid_amount > 0:
        finance.record_payment(
            session,
            actor,
            finance.PaymentIn(
                id=uuid.uuid5(pi.id, "payment"),
                direction="out",
                party_id=supplier.id,
                amount=pi.paid_amount,
                method=data.payment_method,
                payment_date=pi.bill_date,
                note=f"paid with {pi.number}",
            ),
        )
    record(
        session,
        tenant_id=actor.tenant_id,
        actor_user_id=actor.user_id,
        action="purchase.posted",
        entity_type="purchase",
        entity_id=pi.id,
        data={"number": pi.number, "supplier": supplier.name, "total": str(pi.total), "lines": len(pi.lines)},
    )
    return pi, True


def purchase_out(pi: PurchaseInvoice, supplier_name: str | None = None) -> dict:
    return {
        "id": str(pi.id),
        "number": pi.number,
        "supplier_id": str(pi.supplier_id),
        "supplier_name": supplier_name,
        "supplier_bill_no": pi.supplier_bill_no,
        "bill_date": pi.bill_date.isoformat(),
        "subtotal": str(pi.subtotal),
        "discount": str(pi.discount),
        "total": str(pi.total),
        "paid_amount": str(pi.paid_amount),
        "lines": [
            {
                "line_no": ln.line_no,
                "product_id": str(ln.product_id),
                "product_name": ln.product_name,
                "batch_no": ln.batch_no,
                "expiry_date": ln.expiry_date.isoformat() if ln.expiry_date else None,
                "quantity": str(ln.quantity),
                "bonus_quantity": str(ln.bonus_quantity),
                "unit_cost": str(ln.unit_cost),
                "line_total": str(ln.line_total),
            }
            for ln in pi.lines
        ],
    }
