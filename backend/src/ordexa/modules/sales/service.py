"""Invoice posting. Desktop invoices (offline or online), and later AI/WhatsApp ones, all post through
``post_invoice``, so every channel gets the same validation (SRS AC-010)."""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ordexa.modules.audit.models import record
from ordexa.modules.catalogue.models import Batch, Product
from ordexa.modules.customers.models import Customer
from ordexa.modules.devices.models import Device
from ordexa.modules.finance.service import balance, post_ledger
from ordexa.modules.identity.service import Principal
from ordexa.modules.inventory import service as inventory
from ordexa.modules.sales.models import Invoice, InvoiceLine
from ordexa.modules.tenants.timeutil import local_date
from ordexa.shared.money import line_total, money, qty


@dataclass
class LineIn:
    line_no: int
    product_id: uuid.UUID
    batch_id: uuid.UUID | None
    quantity: Decimal
    unit_price: Decimal
    bonus_quantity: Decimal = Decimal(0)
    discount: Decimal = Decimal(0)


@dataclass
class InvoiceIn:
    id: uuid.UUID
    number: str
    customer_id: uuid.UUID | None
    issued_at: datetime
    lines: list[LineIn]
    discount: Decimal = Decimal(0)
    paid_amount: Decimal = Decimal(0)
    client_total: Decimal | None = None
    walk_in_name: str | None = None


@dataclass
class PostResult:
    id: str
    number: str
    status: str  # accepted | duplicate | conflict | rejected
    flags: list[dict] = field(default_factory=list)
    reason: str | None = None
    total: str | None = None


def payload_hash(inv: InvoiceIn) -> str:
    canon = {
        "id": str(inv.id),
        "number": inv.number,
        "customer_id": str(inv.customer_id),
        "issued_at": inv.issued_at.isoformat(),
        "discount": str(money(inv.discount)),
        "paid": str(money(inv.paid_amount)),
        "lines": [
            [
                ln.line_no,
                str(ln.product_id),
                str(ln.batch_id),
                str(qty(ln.quantity)),
                str(qty(ln.bonus_quantity)),
                str(money(ln.unit_price)),
                str(money(ln.discount)),
            ]
            for ln in sorted(inv.lines, key=lambda x: x.line_no)
        ],
    }
    return hashlib.sha256(json.dumps(canon, sort_keys=True).encode()).hexdigest()


def customer_outstanding(session: Session, tenant_id: uuid.UUID, customer_id: uuid.UUID) -> Decimal:
    """Receivable from the party ledger (invoices - receipts - returns + bounced cheques)."""
    return balance(session, tenant_id, "customer", customer_id)


def _replay(existing: Invoice, h: str, principal: Principal) -> PostResult:
    if existing.tenant_id != principal.tenant_id:
        return PostResult(str(existing.id), existing.number, "rejected", reason="invoice id is not available")
    if existing.payload_hash == h:
        return PostResult(str(existing.id), existing.number, "duplicate", existing.flags, total=str(existing.total))
    return PostResult(
        str(existing.id), existing.number, "conflict", reason="an invoice with this id was already synced with different content"
    )


def post_invoice(
    session: Session,
    principal: Principal,
    device: Device | None,
    inv: InvoiceIn,
    source: str = "desktop",
    warehouse_id: uuid.UUID | None = None,
) -> PostResult:
    """Idempotent: replaying the same invoice returns ``duplicate`` and changes nothing.
    The sale already happened at the counter, so business-rule breaches (oversold, credit, price drift)
    are recorded as flags for the owner to review rather than rejecting the invoice."""
    principal.require("invoice.create")
    h = payload_hash(inv)
    existing = session.get(Invoice, inv.id)
    if existing:
        return _replay(existing, h, principal)

    def reject(reason: str) -> PostResult:
        return PostResult(str(inv.id), inv.number, "rejected", reason=reason)

    wh_id = device.warehouse_id if device else warehouse_id
    if wh_id is None:
        return reject("no warehouse for this invoice")
    if device and not inv.number.startswith(f"{device.terminal_code}-"):
        return reject(f"invoice number must use this PC's series '{device.terminal_code}-'")
    if not inv.lines:
        return reject("invoice has no lines")

    flags: list[dict] = []
    customer = None
    if inv.customer_id is not None:
        customer = session.get(Customer, inv.customer_id)
        if not customer or customer.tenant_id != principal.tenant_id:
            return reject("customer not found")
        if not customer.is_active:
            flags.append({"code": "customer_inactive"})

    lines: list[InvoiceLine] = []
    subtotal = Decimal(0)
    for ln in sorted(inv.lines, key=lambda x: x.line_no):
        product = session.get(Product, ln.product_id)
        if not product or product.tenant_id != principal.tenant_id:
            return reject(f"line {ln.line_no}: product not found")
        batch = session.get(Batch, ln.batch_id) if ln.batch_id else None
        if ln.batch_id and (not batch or batch.product_id != product.id):
            return reject(f"line {ln.line_no}: batch does not belong to product")
        if qty(ln.quantity) <= 0 or qty(ln.bonus_quantity) < 0 or money(ln.unit_price) < 0:
            return reject(f"line {ln.line_no}: invalid quantity or price")
        current = batch.sale_price if batch and batch.sale_price is not None else product.sale_price
        if money(ln.unit_price) != money(current):
            flags.append(
                {
                    "code": "price_changed",
                    "line_no": ln.line_no,
                    "billed": str(money(ln.unit_price)),
                    "current": str(money(current)),
                }
            )
        total = line_total(ln.quantity, ln.unit_price, ln.discount)
        subtotal += total
        lines.append(
            InvoiceLine(
                line_no=ln.line_no,
                product_id=product.id,
                batch_id=batch.id if batch else None,
                product_name=product.name,
                batch_no=batch.batch_no if batch else None,
                quantity=qty(ln.quantity),
                bonus_quantity=qty(ln.bonus_quantity),
                unit=product.sale_unit,
                unit_price=money(ln.unit_price),
                discount=money(ln.discount),
                line_total=total,
            )
        )

    total = money(subtotal - money(inv.discount))
    if inv.client_total is not None and money(inv.client_total) != total:
        flags.append({"code": "total_mismatch", "device_total": str(money(inv.client_total)), "server_total": str(total)})

    invoice = Invoice(
        id=inv.id,
        tenant_id=principal.tenant_id,
        device_id=device.id if device else None,
        warehouse_id=wh_id,
        number=inv.number,
        customer_id=inv.customer_id,
        customer_name=customer.name if customer else (inv.walk_in_name or "Walk-in customer"),
        source=source,
        issued_at=inv.issued_at,
        subtotal=money(subtotal),
        discount=money(inv.discount),
        total=total,
        paid_amount=money(inv.paid_amount),
        flags=[],
        payload_hash=h,
        created_by=principal.user_id,
        lines=lines,
    )
    try:
        with session.begin_nested():  # a concurrent push of the same invoice loses the race cleanly
            session.add(invoice)
            session.flush()
    except IntegrityError:
        existing = session.get(Invoice, inv.id)
        if existing:
            return _replay(existing, h, principal)
        return reject(f"invoice number {inv.number} is already used")

    for ln in lines:
        inventory.post(
            session,
            tenant_id=principal.tenant_id,
            warehouse_id=wh_id,
            product_id=ln.product_id,
            batch_id=ln.batch_id,
            qty_delta=-(ln.quantity + ln.bonus_quantity),
            kind="sale",
            source_type="invoice",
            source_id=invoice.id,
            source_line=ln.line_no,
            created_by=principal.user_id,
        )
    session.flush()
    for ln in lines:
        left = inventory.on_hand(
            session,
            tenant_id=principal.tenant_id,
            warehouse_id=wh_id,
            product_id=ln.product_id,
            batch_id=ln.batch_id,
        )
        if left < 0:
            flags.append(
                {
                    "code": "oversold",
                    "line_no": ln.line_no,
                    "product": ln.product_name,
                    "batch_no": ln.batch_no,
                    "on_hand_after": str(left),
                }
            )
    if customer:
        day = local_date(session, principal.tenant_id, inv.issued_at)  # business date, not UTC
        post_ledger(
            session,
            tenant_id=principal.tenant_id,
            party_type="customer",
            party_id=customer.id,
            amount=total,
            kind="invoice",
            source_id=invoice.id,
            reference=inv.number,
            entry_date=day,
        )
        post_ledger(
            session,
            tenant_id=principal.tenant_id,
            party_type="customer",
            party_id=customer.id,
            amount=-money(inv.paid_amount),
            kind="receipt",
            source_id=invoice.id,
            reference=inv.number,
            entry_date=day,
            memo="paid at counter",
        )
    if customer and customer.credit_limit is not None:
        outstanding = customer_outstanding(session, principal.tenant_id, customer.id)
        if outstanding > customer.credit_limit:
            flags.append({"code": "credit_exceeded", "outstanding": str(outstanding), "credit_limit": str(customer.credit_limit)})
    invoice.flags = flags
    record(
        session,
        tenant_id=principal.tenant_id,
        actor_user_id=principal.user_id,
        actor_device_id=device.id if device else None if device else None,
        action="invoice.posted",
        entity_type="invoice",
        entity_id=invoice.id,
        data={"number": inv.number, "total": str(total), "source": source, "flags": [f["code"] for f in flags]},
    )
    return PostResult(str(invoice.id), invoice.number, "accepted", flags, total=str(total))
