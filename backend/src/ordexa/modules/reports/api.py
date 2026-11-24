"""Read-only reports. Every figure is computed from posted records (SRS REP-009: never invented)."""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ordexa.modules.catalogue.models import Batch, Product
from ordexa.modules.finance import service as finance
from ordexa.modules.finance.models import Expense, Payment
from ordexa.modules.identity.service import Principal, requires
from ordexa.modules.inventory.models import InventoryTransaction
from ordexa.modules.purchasing.models import PurchaseInvoice
from ordexa.modules.sales.models import Invoice, InvoiceLine
from ordexa.modules.tenants.models import Tenant
from ordexa.modules.tenants.timeutil import business_today
from ordexa.shared.db import get_session
from ordexa.shared.money import money, qty

router = APIRouter(prefix="/api/v1/reports", tags=["reports"])


def _day_range(session: Session, tenant_id: uuid.UUID, start: date, end: date) -> tuple[datetime, datetime]:
    """Local business days [start, end] -> UTC instants (tenant timezone, e.g. Asia/Karachi)."""
    tz = ZoneInfo(session.get(Tenant, tenant_id).timezone)
    lo = datetime.combine(start, time.min, tz).astimezone(UTC)
    hi = datetime.combine(end + timedelta(days=1), time.min, tz).astimezone(UTC)
    return lo, hi


def _stock_rows(session: Session, tenant_id: uuid.UUID, warehouse_id: uuid.UUID | None = None):
    t = InventoryTransaction
    q = (
        select(t.product_id, t.batch_id, func.sum(t.qty_delta).label("on_hand"))
        .where(t.tenant_id == tenant_id)
        .group_by(t.product_id, t.batch_id)
    )
    if warehouse_id:
        q = q.where(t.warehouse_id == warehouse_id)
    return session.execute(q).all()


@router.get("/dashboard")
def dashboard(p: Principal = Depends(requires("reports.view")), session: Session = Depends(get_session)):
    tz = ZoneInfo(session.get(Tenant, p.tenant_id).timezone)
    today = datetime.now(tz).date()
    lo, hi = _day_range(session, p.tenant_id, today, today)
    mlo, _ = _day_range(session, p.tenant_id, today.replace(day=1), today)

    def sales_between(a, b):
        r = session.execute(
            select(func.count(), func.coalesce(func.sum(Invoice.total), 0)).where(
                Invoice.tenant_id == p.tenant_id, Invoice.status == "posted", Invoice.issued_at >= a, Invoice.issued_at < b
            )
        ).one()
        return int(r[0]), money(r[1])

    today_n, today_total = sales_between(lo, hi)
    month_n, month_total = sales_between(mlo, hi)
    receivable = sum((v for v in finance.balances(session, p.tenant_id, "customer").values() if v > 0), Decimal(0))
    payable = sum((v for v in finance.balances(session, p.tenant_id, "supplier").values() if v > 0), Decimal(0))
    cash_in = session.scalar(
        select(func.coalesce(func.sum(Payment.amount), 0)).where(
            Payment.tenant_id == p.tenant_id, Payment.direction == "in", Payment.payment_date == today
        )
    )
    counter_cash = session.scalar(
        select(func.coalesce(func.sum(Invoice.paid_amount), 0)).where(
            Invoice.tenant_id == p.tenant_id, Invoice.issued_at >= lo, Invoice.issued_at < hi
        )
    )
    pending_cheques = session.scalar(
        select(func.count()).select_from(Payment).where(Payment.tenant_id == p.tenant_id, Payment.cheque_status == "pending")
    )
    flagged = session.scalar(
        select(func.count())
        .select_from(Invoice)
        .where(Invoice.tenant_id == p.tenant_id, func.jsonb_array_length(Invoice.flags) > 0, Invoice.issued_at >= mlo)
    )
    exp = expiry(days=90, p=p, session=session)
    low = low_stock(p=p, session=session)
    top = session.execute(
        select(InvoiceLine.product_name, func.sum(InvoiceLine.quantity), func.sum(InvoiceLine.line_total))
        .join(Invoice, Invoice.id == InvoiceLine.invoice_id)
        .where(Invoice.tenant_id == p.tenant_id, Invoice.issued_at >= mlo)
        .group_by(InvoiceLine.product_name)
        .order_by(func.sum(InvoiceLine.line_total).desc())
        .limit(5)
    ).all()
    trend = sales_by_day(start=today - timedelta(days=13), end=today, p=p, session=session)
    return {
        "today": {
            "date": today.isoformat(),
            "invoices": today_n,
            "sales": str(today_total),
            "cash_collected": str(money(Decimal(cash_in) + Decimal(counter_cash))),
        },
        "month": {"invoices": month_n, "sales": str(month_total)},
        "receivable": str(money(receivable)),
        "payable": str(money(payable)),
        "pending_cheques": pending_cheques,
        "flagged_invoices": flagged,
        "near_expiry_batches": len(exp),
        "low_stock_products": len(low),
        "top_products": [{"name": n, "quantity": str(qty(q)), "amount": str(money(a))} for n, q, a in top],
        "trend": trend,
    }


@router.get("/sales-by-day")
def sales_by_day(
    start: date, end: date, p: Principal = Depends(requires("reports.view")), session: Session = Depends(get_session)
):
    tz_name = session.get(Tenant, p.tenant_id).timezone
    lo, hi = _day_range(session, p.tenant_id, start, end)
    day = func.date(func.timezone(tz_name, Invoice.issued_at))
    rows = dict(
        (d, (n, t))
        for d, n, t in session.execute(
            select(day, func.count(), func.sum(Invoice.total))
            .where(Invoice.tenant_id == p.tenant_id, Invoice.status == "posted", Invoice.issued_at >= lo, Invoice.issued_at < hi)
            .group_by(day)
        ).all()
    )
    out, d = [], start
    while d <= end:
        n, t = rows.get(d, (0, 0))
        out.append({"date": d.isoformat(), "invoices": int(n), "sales": str(money(t or 0))})
        d += timedelta(days=1)
    return out


@router.get("/sales-by-product")
def sales_by_product(
    start: date, end: date, p: Principal = Depends(requires("reports.view")), session: Session = Depends(get_session)
):
    lo, hi = _day_range(session, p.tenant_id, start, end)
    rows = session.execute(
        select(
            InvoiceLine.product_id,
            InvoiceLine.product_name,
            func.sum(InvoiceLine.quantity),
            func.sum(InvoiceLine.bonus_quantity),
            func.sum(InvoiceLine.line_total),
        )
        .join(Invoice, Invoice.id == InvoiceLine.invoice_id)
        .where(Invoice.tenant_id == p.tenant_id, Invoice.issued_at >= lo, Invoice.issued_at < hi)
        .group_by(InvoiceLine.product_id, InvoiceLine.product_name)
        .order_by(func.sum(InvoiceLine.line_total).desc())
    )
    return [
        {"product_id": str(pid), "name": n, "quantity": str(qty(q)), "bonus": str(qty(b)), "amount": str(money(a))}
        for pid, n, q, b, a in rows
    ]


@router.get("/sales-by-customer")
def sales_by_customer(
    start: date, end: date, p: Principal = Depends(requires("reports.view")), session: Session = Depends(get_session)
):
    lo, hi = _day_range(session, p.tenant_id, start, end)
    rows = session.execute(
        select(Invoice.customer_name, func.count(), func.sum(Invoice.total))
        .where(Invoice.tenant_id == p.tenant_id, Invoice.issued_at >= lo, Invoice.issued_at < hi)
        .group_by(Invoice.customer_name)
        .order_by(func.sum(Invoice.total).desc())
    )
    return [{"customer": n, "invoices": int(c), "amount": str(money(a))} for n, c, a in rows]


@router.get("/stock")
def stock(
    warehouse_id: uuid.UUID | None = None,
    p: Principal = Depends(requires("inventory.view")),
    session: Session = Depends(get_session),
):
    products = {x.id: x for x in session.scalars(select(Product).where(Product.tenant_id == p.tenant_id))}
    batches = {x.id: x for x in session.scalars(select(Batch).where(Batch.tenant_id == p.tenant_id))}
    out = []
    for pid, bid, on_hand in _stock_rows(session, p.tenant_id, warehouse_id):
        pr, b = products.get(pid), batches.get(bid) if bid else None
        if pr is None:
            continue
        cost = b.cost_price if b and b.cost_price is not None else None
        out.append(
            {
                "product_id": str(pid),
                "code": pr.code,
                "name": pr.name,
                "batch_id": str(bid) if bid else None,
                "batch_no": b.batch_no if b else None,
                "expiry_date": b.expiry_date.isoformat() if b and b.expiry_date else None,
                "on_hand": str(qty(on_hand)),
                "cost_price": str(cost) if cost is not None else None,
                "value": str(money(qty(on_hand) * cost)) if cost is not None else None,
            }
        )
    return sorted(out, key=lambda r: (r["name"], r["expiry_date"] or ""))


@router.get("/expiry")
def expiry(days: int = 90, p: Principal = Depends(requires("inventory.view")), session: Session = Depends(get_session)):
    """Batches with stock that are expired or expire within ``days`` (expiry intimation)."""
    today = business_today(session, p.tenant_id)
    limit = today + timedelta(days=days)
    out = []
    for r in stock(p=p, session=session):
        if r["expiry_date"] and Decimal(r["on_hand"]) > 0 and date.fromisoformat(r["expiry_date"]) <= limit:
            d = (date.fromisoformat(r["expiry_date"]) - today).days
            out.append({**r, "days_left": d, "expired": d <= 0})
    return sorted(out, key=lambda r: r["days_left"])


@router.get("/low-stock")
def low_stock(p: Principal = Depends(requires("inventory.view")), session: Session = Depends(get_session)):
    """Products at or under their reorder level, with a suggested order quantity (auto purchase reorder):
    enough for 30 days at the last 30 days' sales rate, at least back up to twice the reorder level."""
    on_hand: dict[uuid.UUID, Decimal] = {}
    for pid, _bid, q in _stock_rows(session, p.tenant_id):
        on_hand[pid] = on_hand.get(pid, Decimal(0)) + Decimal(q)
    since = datetime.now(UTC) - timedelta(days=30)
    sold = dict(
        session.execute(
            select(InvoiceLine.product_id, func.sum(InvoiceLine.quantity + InvoiceLine.bonus_quantity))
            .join(Invoice, Invoice.id == InvoiceLine.invoice_id)
            .where(Invoice.tenant_id == p.tenant_id, Invoice.issued_at >= since)
            .group_by(InvoiceLine.product_id)
        ).all()
    )
    out = []
    for pr in session.scalars(
        select(Product).where(Product.tenant_id == p.tenant_id, Product.is_active.is_(True), Product.reorder_level.is_not(None))
    ):
        have = on_hand.get(pr.id, Decimal(0))
        if have <= pr.reorder_level:
            monthly = Decimal(sold.get(pr.id, 0) or 0)
            suggest = max(monthly - have, pr.reorder_level * 2 - have, Decimal(0))
            out.append(
                {
                    "product_id": str(pr.id),
                    "code": pr.code,
                    "name": pr.name,
                    "on_hand": str(qty(have)),
                    "reorder_level": str(qty(pr.reorder_level)),
                    "sold_last_30_days": str(qty(monthly)),
                    "suggested_order": str(qty(suggest.to_integral_value(rounding="ROUND_CEILING"))),
                }
            )
    return out


@router.get("/profit")
def profit(start: date, end: date, p: Principal = Depends(requires("finance.view")), session: Session = Depends(get_session)):
    """Gross profit = sales - cost of goods sold (batch cost price) - expenses, for the period."""
    lo, hi = _day_range(session, p.tenant_id, start, end)
    rows = session.execute(
        select(InvoiceLine.line_total, InvoiceLine.quantity + InvoiceLine.bonus_quantity, Batch.cost_price)
        .join(Invoice, Invoice.id == InvoiceLine.invoice_id)
        .outerjoin(Batch, Batch.id == InvoiceLine.batch_id)
        .where(Invoice.tenant_id == p.tenant_id, Invoice.issued_at >= lo, Invoice.issued_at < hi)
    ).all()
    sales = sum((Decimal(r[0]) for r in rows), Decimal(0))
    cogs = sum((Decimal(r[1]) * Decimal(r[2]) for r in rows if r[2] is not None), Decimal(0))
    unknown_cost_lines = sum(1 for r in rows if r[2] is None)
    expenses = session.scalar(
        select(func.coalesce(func.sum(Expense.amount), 0)).where(
            Expense.tenant_id == p.tenant_id, Expense.expense_date >= start, Expense.expense_date <= end
        )
    )
    purchases = session.scalar(
        select(func.coalesce(func.sum(PurchaseInvoice.total), 0)).where(
            PurchaseInvoice.tenant_id == p.tenant_id, PurchaseInvoice.bill_date >= start, PurchaseInvoice.bill_date <= end
        )
    )
    return {
        "sales": str(money(sales)),
        "cost_of_goods": str(money(cogs)),
        "gross_profit": str(money(sales - cogs)),
        "expenses": str(money(expenses)),
        "net_profit": str(money(sales - cogs - Decimal(expenses))),
        "purchases": str(money(purchases)),
        "lines_without_cost": unknown_cost_lines,
    }
