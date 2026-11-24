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


# ------------------------------------------------------------------ adding items to today's bill

AMENDABLE_SOURCES = ("whatsapp_ai", "ai_manual")  # server-numbered W- bills; PC bills are printed at the counter


def amend_blocker(session: Session, invoice: Invoice) -> str | None:
    """Why this bill cannot take more items (None = it can): same business day, W-series, no returns against it."""
    from ordexa.modules.finance.models import CreditNote
    from ordexa.modules.tenants.timeutil import business_today

    if invoice.status != "posted":
        return f"bill {invoice.number} is {invoice.status}"
    if invoice.source not in AMENDABLE_SOURCES:
        return f"bill {invoice.number} was made on a PC; only WhatsApp/AI bills can be added to"
    if local_date(session, invoice.tenant_id, invoice.issued_at) != business_today(session, invoice.tenant_id):
        return f"bill {invoice.number} is from an earlier day"
    if session.query(CreditNote.id).filter(CreditNote.invoice_id == invoice.id).first():
        return f"bill {invoice.number} already has a return / credit note"
    return None


def add_lines(session: Session, principal: Principal, invoice: Invoice, new_lines: list[LineIn], *, note: str) -> PostResult:
    """Add items to a bill made earlier today: the same bill number, total and ledger grow, stock goes out for the new
    lines only, and the change is recorded on the bill and in the audit log."""
    principal.require("invoice.create")
    why = amend_blocker(session, invoice)
    if why:
        return PostResult(str(invoice.id), invoice.number, "rejected", reason=why)
    if not new_lines:
        return PostResult(str(invoice.id), invoice.number, "rejected", reason="nothing to add")
    start = max((ln.line_no for ln in invoice.lines), default=0)
    added: list[InvoiceLine] = []
    flags: list[dict] = []
    add_total = Decimal(0)
    for k, ln in enumerate(new_lines, start=1):
        product = session.get(Product, ln.product_id)
        if not product or product.tenant_id != principal.tenant_id:
            return PostResult(str(invoice.id), invoice.number, "rejected", reason="product not found")
        batch = session.get(Batch, ln.batch_id) if ln.batch_id else None
        if qty(ln.quantity) <= 0 or money(ln.unit_price) < 0:
            return PostResult(str(invoice.id), invoice.number, "rejected", reason="invalid quantity or price")
        total = line_total(ln.quantity, ln.unit_price, ln.discount)
        add_total += total
        added.append(
            InvoiceLine(
                line_no=start + k,
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
    invoice.lines.extend(added)
    invoice.subtotal = money(invoice.subtotal + add_total)
    invoice.total = money(invoice.total + add_total)
    session.flush()
    for ln in added:
        inventory.post(
            session,
            tenant_id=principal.tenant_id,
            warehouse_id=invoice.warehouse_id,
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
    for ln in added:
        left = inventory.on_hand(
            session,
            tenant_id=principal.tenant_id,
            warehouse_id=invoice.warehouse_id,
            product_id=ln.product_id,
            batch_id=ln.batch_id,
        )
        if left < 0:
            flags.append({"code": "oversold", "line_no": ln.line_no, "product": ln.product_name, "on_hand_after": str(left)})
    amendment_id = uuid.uuid4()
    if invoice.customer_id:
        post_ledger(
            session,
            tenant_id=principal.tenant_id,
            party_type="customer",
            party_id=invoice.customer_id,
            amount=add_total,
            kind="invoice_addition",
            source_id=amendment_id,
            reference=f"{invoice.number} +",
            entry_date=local_date(session, principal.tenant_id, invoice.issued_at),
            memo=note[:200],
        )
        customer = session.get(Customer, invoice.customer_id)
        if customer and customer.credit_limit is not None:
            outstanding = customer_outstanding(session, principal.tenant_id, customer.id)
            if outstanding > customer.credit_limit:
                flags.append(
                    {"code": "credit_exceeded", "outstanding": str(outstanding), "credit_limit": str(customer.credit_limit)}
                )
    invoice.flags = (
        (invoice.flags or [])
        + flags
        + [
            {
                "code": "items_added",
                "amendment_id": str(amendment_id),
                "lines": [ln.line_no for ln in added],
                "amount": str(money(add_total)),
                "note": note[:200],
                "by": principal.username,
                "at": datetime.now().astimezone().isoformat(),
            }
        ]
    )
    record(
        session,
        tenant_id=principal.tenant_id,
        actor_user_id=principal.user_id,
        action="invoice.items_added",
        entity_type="invoice",
        entity_id=invoice.id,
        data={"number": invoice.number, "added": str(money(add_total)), "total": str(invoice.total), "note": note[:200]},
    )
    return PostResult(str(invoice.id), invoice.number, "accepted", flags, total=str(invoice.total))


def revise_bill(
    session: Session,
    principal: Principal,
    invoice: Invoice,
    set_qty: dict[uuid.UUID, Decimal],
    add: list[LineIn],
    *,
    note: str,
) -> tuple[PostResult, list[str]]:
    """Correct today's bill in place ("10 ki jagah 50"): change the quantity of products already on it (0 removes the
    line) and add new products. Stock and the shop's ledger move by the difference only; recorded on the bill."""
    principal.require("invoice.create")
    why = amend_blocker(session, invoice)
    if why:
        return PostResult(str(invoice.id), invoice.number, "rejected", reason=why), []
    revision_id = uuid.uuid4()
    changes: list[str] = []
    flags: list[dict] = []
    old_total = invoice.total
    for product_id, new_qty in set_qty.items():
        line = next((ln for ln in sorted(invoice.lines, key=lambda x: x.line_no) if ln.product_id == product_id), None)
        if line is None:
            continue
        new_qty = qty(new_qty)
        delta = new_qty - line.quantity
        if delta == 0:
            continue
        inventory.post(
            session,
            tenant_id=principal.tenant_id,
            warehouse_id=invoice.warehouse_id,
            product_id=line.product_id,
            batch_id=line.batch_id,
            qty_delta=-delta,
            kind="sale" if delta > 0 else "sale_correction",
            source_type="invoice_revision",
            source_id=revision_id,
            source_line=line.line_no,
            created_by=principal.user_id,
        )
        changes.append(f"{line.product_name}: {line.quantity.normalize():f} -> {new_qty.normalize():f} {line.unit}")
        if new_qty <= 0:
            invoice.lines.remove(line)
        else:
            line.quantity = new_qty
            line.line_total = line_total(new_qty, line.unit_price, line.discount)
    session.flush()
    if add:
        start = max((ln.line_no for ln in invoice.lines), default=0)
        for k, ln in enumerate(add, start=1):
            product = session.get(Product, ln.product_id)
            batch = session.get(Batch, ln.batch_id) if ln.batch_id else None
            if not product or product.tenant_id != principal.tenant_id:
                continue
            total = line_total(ln.quantity, ln.unit_price, ln.discount)
            new_line = InvoiceLine(
                line_no=start + k,
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
            invoice.lines.append(new_line)
            session.flush()
            inventory.post(
                session,
                tenant_id=principal.tenant_id,
                warehouse_id=invoice.warehouse_id,
                product_id=product.id,
                batch_id=new_line.batch_id,
                qty_delta=-new_line.quantity,
                kind="sale",
                source_type="invoice_revision",
                source_id=revision_id,
                source_line=new_line.line_no,
                created_by=principal.user_id,
            )
            changes.append(f"{product.name}: +{new_line.quantity.normalize():f} {product.sale_unit}")
    if not changes:
        return PostResult(str(invoice.id), invoice.number, "rejected", reason="nothing changes on the bill"), []
    session.flush()
    invoice.subtotal = money(sum((ln.line_total for ln in invoice.lines), Decimal(0)))
    invoice.total = money(invoice.subtotal - invoice.discount)
    diff = money(invoice.total - old_total)
    if invoice.customer_id and diff != 0:
        post_ledger(
            session,
            tenant_id=principal.tenant_id,
            party_type="customer",
            party_id=invoice.customer_id,
            amount=diff,
            kind="invoice_revision",
            source_id=revision_id,
            reference=f"{invoice.number} ~",
            entry_date=local_date(session, principal.tenant_id, invoice.issued_at),
            memo=note[:200],
        )
    for ln in invoice.lines:
        left = inventory.on_hand(
            session,
            tenant_id=principal.tenant_id,
            warehouse_id=invoice.warehouse_id,
            product_id=ln.product_id,
            batch_id=ln.batch_id,
        )
        if left < 0:
            flags.append({"code": "oversold", "line_no": ln.line_no, "product": ln.product_name, "on_hand_after": str(left)})
    invoice.flags = (
        (invoice.flags or [])
        + flags
        + [
            {
                "code": "items_added",  # shown as "updated" everywhere
                "kind": "revised",
                "revision_id": str(revision_id),
                "changes": changes,
                "amount": str(diff),
                "note": note[:200],
                "by": principal.username,
                "at": datetime.now().astimezone().isoformat(),
                "lines": [],
            }
        ]
    )
    record(
        session,
        tenant_id=principal.tenant_id,
        actor_user_id=principal.user_id,
        action="invoice.revised",
        entity_type="invoice",
        entity_id=invoice.id,
        data={"number": invoice.number, "changes": changes, "difference": str(diff), "total": str(invoice.total)},
    )
    return PostResult(str(invoice.id), invoice.number, "accepted", flags, total=str(invoice.total)), changes
