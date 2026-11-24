"""Receivables, payables, receipts/payments, cheques, credit notes and expenses. Every balance comes from
``ledger_entries`` (one place, auditable, idempotent per source document)."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from ordexa.modules.audit.models import record
from ordexa.modules.catalogue.models import Batch, Product
from ordexa.modules.customers.models import Customer
from ordexa.modules.finance.models import CreditNote, CreditNoteLine, Expense, LedgerEntry, Payment
from ordexa.modules.identity.service import Principal
from ordexa.modules.inventory import service as inventory
from ordexa.modules.purchasing.models import Supplier
from ordexa.modules.sales.models import Invoice
from ordexa.modules.tenants.models import Warehouse
from ordexa.modules.tenants.timeutil import business_today
from ordexa.shared.errors import ErrorCode, OrdexaError
from ordexa.shared.money import line_total, money, qty
from ordexa.shared.sequences import next_number

# ------------------------------------------------------------------ ledger


def post_ledger(
    session: Session,
    *,
    tenant_id: uuid.UUID,
    party_type: str,
    party_id: uuid.UUID,
    amount: Decimal,
    kind: str,
    source_id: uuid.UUID,
    reference: str,
    entry_date: date,
    memo: str | None = None,
) -> bool:
    """Idempotent: a (kind, source) pair posts at most once. Returns False if it already existed."""
    if money(amount) == 0:
        return False
    stmt = (
        insert(LedgerEntry)
        .values(
            id=uuid.uuid4(),
            tenant_id=tenant_id,
            party_type=party_type,
            party_id=party_id,
            amount=money(amount),
            kind=kind,
            source_id=source_id,
            reference=reference,
            entry_date=entry_date,
            memo=memo,
        )
        .on_conflict_do_nothing(constraint="uq_ledger_source")
        .returning(LedgerEntry.id)
    )
    return session.execute(stmt).scalar() is not None


def balance(session: Session, tenant_id: uuid.UUID, party_type: str, party_id: uuid.UUID) -> Decimal:
    v = session.scalar(
        select(func.coalesce(func.sum(LedgerEntry.amount), 0)).where(
            LedgerEntry.tenant_id == tenant_id, LedgerEntry.party_type == party_type, LedgerEntry.party_id == party_id
        )
    )
    return money(v)


def balances(session: Session, tenant_id: uuid.UUID, party_type: str) -> dict[uuid.UUID, Decimal]:
    rows = session.execute(
        select(LedgerEntry.party_id, func.sum(LedgerEntry.amount))
        .where(LedgerEntry.tenant_id == tenant_id, LedgerEntry.party_type == party_type)
        .group_by(LedgerEntry.party_id)
    )
    return {pid: money(v) for pid, v in rows}


def statement(session: Session, tenant_id: uuid.UUID, party_type: str, party_id: uuid.UUID) -> list[dict]:
    rows = session.scalars(
        select(LedgerEntry)
        .where(LedgerEntry.tenant_id == tenant_id, LedgerEntry.party_type == party_type, LedgerEntry.party_id == party_id)
        .order_by(LedgerEntry.entry_date, LedgerEntry.created_at)
    )
    running = Decimal(0)
    out = []
    for e in rows:
        running += e.amount
        out.append(
            {
                "date": e.entry_date.isoformat(),
                "kind": e.kind,
                "reference": e.reference,
                "memo": e.memo,
                "debit": str(e.amount) if e.amount > 0 else None,
                "credit": str(-e.amount) if e.amount < 0 else None,
                "balance": str(money(running)),
            }
        )
    return out


def aging(session: Session, tenant_id: uuid.UUID, party_type: str, as_of: date) -> list[dict]:
    """Ageing buckets: credits (receipts, returns) settle the oldest debits first (FIFO)."""
    entries = session.scalars(
        select(LedgerEntry)
        .where(LedgerEntry.tenant_id == tenant_id, LedgerEntry.party_type == party_type)
        .order_by(LedgerEntry.party_id, LedgerEntry.entry_date, LedgerEntry.created_at)
    )
    by_party: dict[uuid.UUID, list[LedgerEntry]] = {}
    for e in entries:
        by_party.setdefault(e.party_id, []).append(e)
    model = Customer if party_type == "customer" else Supplier
    names = dict(session.execute(select(model.id, model.name).where(model.tenant_id == tenant_id)).all())
    out = []
    for pid, es in by_party.items():
        credit = -sum((e.amount for e in es if e.amount < 0), Decimal(0))
        buckets = {"0_30": Decimal(0), "31_60": Decimal(0), "61_90": Decimal(0), "over_90": Decimal(0)}
        for d in (e for e in es if e.amount > 0):
            open_amt = d.amount
            used = min(open_amt, credit)
            open_amt -= used
            credit -= used
            if open_amt <= 0:
                continue
            age = (as_of - d.entry_date).days
            key = "0_30" if age <= 30 else "31_60" if age <= 60 else "61_90" if age <= 90 else "over_90"
            buckets[key] += open_amt
        total = sum(buckets.values(), Decimal(0)) - credit  # leftover credit = advance
        if total == 0 and credit == 0:
            continue
        out.append(
            {
                "party_id": str(pid),
                "name": names.get(pid, "?"),
                "balance": str(money(total)),
                **{k: str(money(v)) for k, v in buckets.items()},
                "advance": str(money(credit)),
            }
        )
    return sorted(out, key=lambda r: Decimal(r["balance"]), reverse=True)


# ------------------------------------------------------------------ payments


@dataclass
class PaymentIn:
    id: uuid.UUID
    direction: str  # in (customer receipt) | out (supplier payment)
    party_id: uuid.UUID
    amount: Decimal
    method: str = "cash"
    payment_date: date | None = None
    cheque_no: str | None = None
    cheque_bank: str | None = None
    cheque_date: date | None = None
    note: str | None = None
    source_invoice_id: uuid.UUID | None = None


def _party(session: Session, tenant_id: uuid.UUID, party_type: str, party_id: uuid.UUID):
    model = Customer if party_type == "customer" else Supplier
    p = session.get(model, party_id)
    if not p or p.tenant_id != tenant_id:
        raise OrdexaError(ErrorCode.NOT_FOUND, f"{party_type} not found")
    return p


def record_payment(
    session: Session, actor: Principal, data: PaymentIn, device_id: uuid.UUID | None = None, number: str | None = None
) -> tuple[Payment, bool]:
    """Idempotent by ``data.id``. Returns (payment, created)."""
    actor.require("payment.record")
    existing = session.get(Payment, data.id)
    if existing:
        if existing.tenant_id != actor.tenant_id:
            raise OrdexaError(ErrorCode.CONFLICT, "payment id is not available")
        return existing, False
    if data.direction not in ("in", "out") or data.method not in ("cash", "bank", "cheque"):
        raise OrdexaError(ErrorCode.VALIDATION_ERROR, "invalid direction or method")
    if money(data.amount) <= 0:
        raise OrdexaError(ErrorCode.VALIDATION_ERROR, "amount must be positive")
    if data.method == "cheque" and not data.cheque_no:
        raise OrdexaError(ErrorCode.VALIDATION_ERROR, "cheque number is required")
    party_type = "customer" if data.direction == "in" else "supplier"
    party = _party(session, actor.tenant_id, party_type, data.party_id)
    when = data.payment_date or business_today(session, actor.tenant_id)
    p = Payment(
        id=data.id,
        tenant_id=actor.tenant_id,
        number=number or next_number(session, actor.tenant_id, "RV" if data.direction == "in" else "PV"),
        direction=data.direction,
        party_type=party_type,
        party_id=party.id,
        party_name=party.name,
        amount=money(data.amount),
        method=data.method,
        payment_date=when,
        cheque_no=data.cheque_no,
        cheque_bank=data.cheque_bank,
        cheque_date=data.cheque_date,
        cheque_status="pending" if data.method == "cheque" else None,
        source_invoice_id=data.source_invoice_id,
        device_id=device_id,
        note=data.note,
        created_by=actor.user_id,
    )
    session.add(p)
    session.flush()
    post_ledger(
        session,
        tenant_id=actor.tenant_id,
        party_type=party_type,
        party_id=party.id,
        amount=-p.amount,
        kind="receipt" if data.direction == "in" else "payment",
        source_id=p.id,
        reference=p.number,
        entry_date=when,
        memo=f"{data.method}{' #' + data.cheque_no if data.cheque_no else ''}",
    )
    record(
        session,
        tenant_id=actor.tenant_id,
        actor_user_id=actor.user_id,
        actor_device_id=device_id,
        action="payment.recorded",
        entity_type="payment",
        entity_id=p.id,
        data={"number": p.number, "direction": p.direction, "amount": str(p.amount), "method": p.method},
    )
    return p, True


def set_cheque_status(session: Session, actor: Principal, payment_id: uuid.UUID, status: str) -> Payment:
    """Cleared: nothing changes in the ledger. Bounced: the receipt is reversed (party owes it again)."""
    actor.require("payment.record")
    p = session.get(Payment, payment_id)
    if not p or p.tenant_id != actor.tenant_id or p.method != "cheque":
        raise OrdexaError(ErrorCode.NOT_FOUND, "cheque not found")
    if status not in ("cleared", "bounced"):
        raise OrdexaError(ErrorCode.VALIDATION_ERROR, "status must be cleared or bounced")
    if p.cheque_status != "pending":
        raise OrdexaError(ErrorCode.CONFLICT, f"cheque is already {p.cheque_status}")
    p.cheque_status = status
    if status == "bounced":
        post_ledger(
            session,
            tenant_id=p.tenant_id,
            party_type=p.party_type,
            party_id=p.party_id,
            amount=p.amount,
            kind="cheque_bounced",
            source_id=p.id,
            reference=p.number,
            entry_date=business_today(session, p.tenant_id),
            memo=f"cheque #{p.cheque_no} bounced",
        )
    record(
        session,
        tenant_id=p.tenant_id,
        actor_user_id=actor.user_id,
        action=f"cheque.{status}",
        entity_type="payment",
        entity_id=p.id,
        data={"cheque_no": p.cheque_no, "amount": str(p.amount)},
    )
    return p


# ------------------------------------------------------------------ credit notes (sales returns)


@dataclass
class ReturnLineIn:
    product_id: uuid.UUID
    batch_id: uuid.UUID | None
    quantity: Decimal
    unit_price: Decimal
    restock: bool = True


def create_credit_note(
    session: Session,
    actor: Principal,
    *,
    id: uuid.UUID,
    customer_id: uuid.UUID,
    warehouse_id: uuid.UUID | None,
    invoice_id: uuid.UUID | None,
    reason: str,
    lines: list[ReturnLineIn],
) -> tuple[CreditNote, bool]:
    actor.require("sales_order.cancel")
    existing = session.get(CreditNote, id)
    if existing:
        return existing, False
    customer = _party(session, actor.tenant_id, "customer", customer_id)
    if invoice_id:
        inv = session.get(Invoice, invoice_id)
        if not inv or inv.tenant_id != actor.tenant_id or inv.customer_id != customer.id:
            raise OrdexaError(ErrorCode.NOT_FOUND, "original invoice not found for this customer")
    if warehouse_id is None:
        wh = session.scalar(select(Warehouse).where(Warehouse.tenant_id == actor.tenant_id).order_by(Warehouse.created_at))
    else:
        wh = session.get(Warehouse, warehouse_id)
    if not wh or wh.tenant_id != actor.tenant_id:
        raise OrdexaError(ErrorCode.NOT_FOUND, "warehouse not found")
    if not lines or len(reason.strip()) < 3:
        raise OrdexaError(ErrorCode.VALIDATION_ERROR, "a return needs lines and a reason")
    cn = CreditNote(
        id=id,
        tenant_id=actor.tenant_id,
        number=next_number(session, actor.tenant_id, "CN"),
        customer_id=customer.id,
        invoice_id=invoice_id,
        warehouse_id=wh.id,
        note_date=business_today(session, actor.tenant_id),
        reason=reason.strip(),
        total=Decimal(0),
        created_by=actor.user_id,
    )
    total = Decimal(0)
    for i, ln in enumerate(lines, start=1):
        product = session.get(Product, ln.product_id)
        batch = session.get(Batch, ln.batch_id) if ln.batch_id else None
        if not product or product.tenant_id != actor.tenant_id or (ln.batch_id and (not batch or batch.product_id != product.id)):
            raise OrdexaError(ErrorCode.NOT_FOUND, f"line {i}: product or batch not found")
        if qty(ln.quantity) <= 0:
            raise OrdexaError(ErrorCode.VALIDATION_ERROR, f"line {i}: quantity must be positive")
        lt = line_total(ln.quantity, ln.unit_price)
        total += lt
        cn.lines.append(
            CreditNoteLine(
                line_no=i,
                product_id=product.id,
                batch_id=batch.id if batch else None,
                product_name=product.name,
                quantity=qty(ln.quantity),
                unit_price=money(ln.unit_price),
                line_total=lt,
                restock=ln.restock,
            )
        )
    cn.total = money(total)
    session.add(cn)
    session.flush()
    for ln in cn.lines:
        if ln.restock:
            inventory.post(
                session,
                tenant_id=actor.tenant_id,
                warehouse_id=wh.id,
                product_id=ln.product_id,
                batch_id=ln.batch_id,
                qty_delta=ln.quantity,
                kind="return",
                source_type="credit_note",
                source_id=cn.id,
                source_line=ln.line_no,
                created_by=actor.user_id,
                reason=reason,
            )
    post_ledger(
        session,
        tenant_id=actor.tenant_id,
        party_type="customer",
        party_id=customer.id,
        amount=-cn.total,
        kind="credit_note",
        source_id=cn.id,
        reference=cn.number,
        entry_date=cn.note_date,
        memo=reason,
    )
    record(
        session,
        tenant_id=actor.tenant_id,
        actor_user_id=actor.user_id,
        action="credit_note.created",
        entity_type="credit_note",
        entity_id=cn.id,
        data={"number": cn.number, "total": str(cn.total)},
    )
    return cn, True


# ------------------------------------------------------------------ expenses


def record_expense(
    session: Session,
    actor: Principal,
    *,
    id: uuid.UUID,
    category: str,
    amount: Decimal,
    expense_date: date | None,
    method: str,
    note: str | None,
) -> tuple[Expense, bool]:
    actor.require("expense.record")
    existing = session.get(Expense, id)
    if existing:
        return existing, False
    if money(amount) <= 0 or not category.strip():
        raise OrdexaError(ErrorCode.VALIDATION_ERROR, "expense needs a category and a positive amount")
    e = Expense(
        id=id,
        tenant_id=actor.tenant_id,
        category=category.strip()[:60],
        amount=money(amount),
        expense_date=expense_date or business_today(session, actor.tenant_id),
        method=method,
        note=note,
        created_by=actor.user_id,
    )
    session.add(e)
    record(
        session,
        tenant_id=actor.tenant_id,
        actor_user_id=actor.user_id,
        action="expense.recorded",
        entity_type="expense",
        entity_id=id,
        data={"category": e.category, "amount": str(e.amount)},
    )
    return e, True
