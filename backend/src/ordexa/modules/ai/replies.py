"""What the bot says back to a shop. Fixed templates filled from the database (never written by the AI): order
confirmations that repeat the items read, rates, balance, the last bill, order status, repeat orders, and hand-offs
to staff for complaints and questions."""

from __future__ import annotations

import uuid
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from ordexa.modules.ai.extract import ExtractedLine
from ordexa.modules.ai.models import AiOrderRequest
from ordexa.modules.catalogue.models import Product
from ordexa.modules.customers.models import Customer
from ordexa.modules.finance.service import balance
from ordexa.modules.jobs.service import enqueue
from ordexa.modules.sales.models import Invoice
from ordexa.modules.tenants.timeutil import local_date

STATUS_TEXT = {
    "received": "hamare paas aa gaya hai, abhi parha ja raha hai",
    "processing": "hamare paas aa gaya hai, abhi parha ja raha hai",
    "needs_review": "check ho raha hai",
    "draft_ready": "team confirm kar rahi hai, bill jald ban jayega",
    "failed": "team check kar rahi hai",
}

HELP = (
    "Order ke liye items aur quantity likhein, jaise '10 box Panadol Extra, 5 botal Calpol'.\n"
    "Balance ke liye 'balance', aakhri bill ke liye 'bill' likhein."
)


def qty_text(v) -> str:
    if v is None:
        return "?"
    d = Decimal(str(v))
    return f"{d:,.0f}" if d == d.to_integral_value() else f"{d.normalize()}"


def rs(v) -> str:
    d = Decimal(str(v or 0))
    return f"Rs {d:,.0f}" if d == d.to_integral_value() else f"Rs {d:,.2f}"


def items_text(lines: list[dict], *, rates: bool = False, stock: bool = True) -> str:
    rows = []
    for ln in lines:
        name = ln.get("product_name") or ln.get("phrase") or "?"
        unit = ln.get("unit") or ln.get("sale_unit") or ""
        row = f"- {name}: {qty_text(ln.get('quantity'))} {unit}".rstrip()
        if rates and ln.get("unit_price"):
            row += f" @ {rs(ln['unit_price'])}"
        if stock and "insufficient_stock" in (ln.get("issues") or []):
            row += " (stock kam hai, team batayegi)"
        rows.append(row)
    return "\n".join(rows)


def estimated_total(lines: list[dict]) -> Decimal | None:
    if not lines or any(not ln.get("line_total") for ln in lines):
        return None
    return sum((Decimal(ln["line_total"]) for ln in lines), Decimal(0))


def order_ack(req: AiOrderRequest, *, asks_price: bool, prefix: str = "") -> str:
    q = req.questions[0] if req.questions else None
    if req.status == "needs_review" and q:
        more = f" ({len(req.questions) - 1} aur sawal baqi)" if len(req.questions) > 1 else ""
        return f"{prefix}Shukriya! Order {req.number} mil gaya. Aik cheez confirm karein{more}:\n{q}"
    body = f"{prefix}Shukriya! Order {req.number} mil gaya:\n{items_text(req.lines, rates=asks_price)}"
    total = estimated_total(req.lines)
    if total is not None:
        body += f"\nAndaazan total: {rs(total)} (final bill team confirm karegi)"
    body += "\nKuch tabdeeli ho to abhi likh dein. Hamari team confirm karke bill bhej degi."
    return body


def balance_text(session: Session, tenant_id: uuid.UUID, customer_id: uuid.UUID) -> str:
    c = session.get(Customer, customer_id)
    owed = balance(session, tenant_id, "customer", customer_id)
    # Only ever the sender's own shop, said by name (a shop asking about another shop gets its own balance).
    return f"Aap ki shop ({c.name}) ka baqaya (balance) {rs(owed)} hai."


def latest_invoice(session: Session, tenant_id: uuid.UUID, customer_id: uuid.UUID | None) -> Invoice | None:
    if customer_id is None:
        return None
    return session.scalar(
        select(Invoice)
        .where(Invoice.tenant_id == tenant_id, Invoice.customer_id == customer_id, Invoice.status == "posted")
        .order_by(Invoice.issued_at.desc())
        .limit(1)
    )


def send_bill(session: Session, req: AiOrderRequest) -> str:
    """Re-send the shop's latest bill as a PDF; returns the text to say if there is none."""
    inv = latest_invoice(session, req.tenant_id, req.customer_id)
    if inv is None:
        return "Aap ka abhi tak koi bill nahi bana. " + HELP
    when = local_date(session, req.tenant_id, inv.issued_at)
    enqueue(
        session,
        kind="whatsapp.send",
        queue="outbox",
        tenant_id=req.tenant_id,
        idempotency_key=f"wa-bill-again:{req.id}",
        payload={
            "to": req.sender,
            "body": f"Aap ka aakhri bill {inv.number} ({when:%d-%m-%Y}): {rs(inv.total)}.",
            "document": {"kind": "invoice", "invoice_id": str(inv.id), "filename": f"Bill-{inv.number}.pdf"},
        },
    )
    return ""


def status_text(session: Session, req: AiOrderRequest) -> str:
    """Status of the shop's previous order (the one before this message)."""
    prev = session.scalar(
        select(AiOrderRequest)
        .where(
            AiOrderRequest.tenant_id == req.tenant_id,
            AiOrderRequest.sender == req.sender,
            AiOrderRequest.id != req.id,
            AiOrderRequest.status.notin_(("closed", "message")),
        )
        .order_by(AiOrderRequest.received_at.desc())
        .limit(1)
    )
    if prev is None:
        return "Aap ka koi order hamare paas nahi hai. " + HELP
    if prev.status == "approved":
        inv = session.get(Invoice, prev.invoice_id) if prev.invoice_id else None
        bill = f" Bill {inv.number} ({rs(inv.total)}) ban chuka hai." if inv else ""
        return (
            f"Order {prev.number} confirm ho chuka hai.{bill} Delivery ke waqt ke liye hamari team ko aap ka sawal bhej diya hai."
        )
    if prev.status == "rejected":
        why = prev.review_note or ""
        if why.startswith("Cancelled by the shop"):
            return f"Order {prev.number} aap ne cancel kar diya tha. Naya order kabhi bhi bhej dein."
        return f"Maazrat, order {prev.number} process nahi ho saka. Wajah: {why}. Mazeed maloomat ke liye team se raabta karein."
    if prev.status == "needs_review" and prev.questions:
        return f"Order {prev.number} aap ke jawab ka intezar kar raha hai:\n{prev.questions[0]}"
    return f"Order {prev.number} {STATUS_TEXT.get(prev.status, 'check ho raha hai')}."


def price_text(session: Session, req: AiOrderRequest, *, footer: bool = True) -> str:
    """Rates of the products the shop named (resolved lines, or the options for a vague name)."""
    rows = []
    for ln in req.lines:
        if ln.get("product_name") and ln.get("unit_price"):
            rows.append(f"- {ln['product_name']}: {rs(ln['unit_price'])} per {ln.get('sale_unit') or 'unit'}")
        elif ln.get("candidates"):
            for c in ln["candidates"][:4]:
                p = session.get(Product, uuid.UUID(c["product_id"]))
                price = _price(session, req, p)
                if price is not None:
                    rows.append(f"- {p.name}: {rs(price)} per {p.sale_unit}")
        else:
            rows.append(f"- '{ln.get('phrase')}': yeh hamari list mein nahi mila")
    if not rows:
        return "Kis dawai ka rate chahiye? Naam likh dein."
    text = "Aaj ke rates:\n" + "\n".join(dict.fromkeys(rows))
    return text + "\nOrder ke liye quantity ke saath likh dein." if footer else text


def _price(session: Session, req: AiOrderRequest, p: Product) -> Decimal | None:
    from ordexa.modules.ai import service as ai

    wh = ai._main_warehouse(session, req.tenant_id)
    try:
        return ai._price(session, req.tenant_id, wh.id, p)
    except Exception:  # noqa: BLE001 - no price set
        return None


def repeat_lines(session: Session, req: AiOrderRequest) -> tuple[Invoice | None, list[ExtractedLine], dict[int, dict]]:
    """Items of the shop's latest bill as order lines (the products are known, so they are kept as chosen)."""
    inv = latest_invoice(session, req.tenant_id, req.customer_id)
    if inv is None:
        return None, [], {}
    lines, keep = [], {}
    for i, il in enumerate(sorted(inv.lines, key=lambda x: x.line_no), start=1):
        lines.append(ExtractedLine(phrase=il.product_name[:200], quantity=float(il.quantity), unit=il.unit))
        keep[i] = {"product_id": str(il.product_id), "resolution": "customer", "quantity": float(il.quantity),
                   "unit": il.unit, "quantity_confirmed": True}  # fmt: skip
    return inv, lines, keep
