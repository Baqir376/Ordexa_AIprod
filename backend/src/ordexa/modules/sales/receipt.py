"""Bill as a PDF, sent to the customer on WhatsApp after an order is approved."""

from __future__ import annotations

from decimal import Decimal

from fpdf import FPDF
from sqlalchemy.orm import Session

from ordexa.modules.customers.models import Customer
from ordexa.modules.sales.models import Invoice
from ordexa.modules.tenants.models import Tenant
from ordexa.modules.tenants.timeutil import local_date


def _t(value: object) -> str:
    """Built-in PDF fonts are Latin-1 only; replace anything else instead of failing."""
    return str(value if value is not None else "").encode("latin-1", "replace").decode("latin-1")


def _money(v: Decimal | None) -> str:
    return f"{Decimal(v or 0):,.2f}"


def _qty(v: Decimal | None) -> str:
    v = Decimal(v or 0)
    return f"{v:,.0f}" if v == v.to_integral_value() else f"{v.normalize():,}"


def _hex(color: str | None) -> tuple[int, int, int]:
    c = (color or "#0F766E").lstrip("#")
    try:
        return int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16)
    except ValueError:
        return 15, 118, 110


def invoice_pdf(session: Session, invoice: Invoice) -> bytes:
    tenant = session.get(Tenant, invoice.tenant_id)
    customer = session.get(Customer, invoice.customer_id) if invoice.customer_id else None
    brand = _hex(getattr(tenant, "primary_color", None))

    pdf = FPDF(format="A4")
    pdf.set_auto_page_break(auto=True, margin=15)
    pdf.set_title(_t(f"Bill {invoice.number}"))
    pdf.set_author(_t(tenant.display_name or tenant.name))
    pdf.add_page()
    w = pdf.epw

    # Header: business
    pdf.set_font("Helvetica", "B", 18)
    pdf.set_text_color(*brand)
    pdf.cell(w, 9, _t(tenant.display_name or tenant.name), new_x="LMARGIN", new_y="NEXT")
    pdf.set_text_color(60, 60, 60)
    pdf.set_font("Helvetica", "", 10)
    contact = "   ".join(x for x in (tenant.address, f"Ph: {tenant.phone}" if tenant.phone else None) if x)
    if contact:
        pdf.cell(w, 5, _t(contact), new_x="LMARGIN", new_y="NEXT")
    pdf.ln(3)
    pdf.set_draw_color(*brand)
    pdf.set_line_width(0.6)
    pdf.line(pdf.l_margin, pdf.get_y(), pdf.l_margin + w, pdf.get_y())
    pdf.ln(4)

    # Bill details
    pdf.set_text_color(20, 20, 20)
    pdf.set_font("Helvetica", "B", 13)
    pdf.cell(w / 2, 7, "SALES INVOICE")
    pdf.set_font("Helvetica", "", 10)
    pdf.cell(w / 2, 7, _t(f"Bill no: {invoice.number}"), align="R", new_x="LMARGIN", new_y="NEXT")
    when = local_date(session, invoice.tenant_id, invoice.issued_at)
    pdf.cell(w / 2, 5, _t(f"Customer: {invoice.customer_name}"))
    pdf.cell(w / 2, 5, _t(f"Date: {when:%d-%m-%Y}"), align="R", new_x="LMARGIN", new_y="NEXT")
    if customer is not None:
        extra = "   ".join(x for x in (f"Code: {customer.code}", customer.address) if x)
        pdf.cell(w, 5, _t(extra), new_x="LMARGIN", new_y="NEXT")
    pdf.ln(4)

    # Lines
    cols = [("#", 8, "C"), ("Item", 62, "L"), ("Batch", 24, "L"), ("Qty", 16, "R"), ("Bonus", 14, "R"),
            ("Rate", 22, "R"), ("Disc", 16, "R"), ("Amount", 28, "R")]  # fmt: skip
    scale = w / sum(c[1] for c in cols)
    cols = [(n, cw * scale, a) for n, cw, a in cols]
    pdf.set_font("Helvetica", "B", 9)
    pdf.set_fill_color(*brand)
    pdf.set_text_color(255, 255, 255)
    for name, cw, align in cols:
        pdf.cell(cw, 7, name, border=0, align=align, fill=True)
    pdf.ln()
    pdf.set_text_color(20, 20, 20)
    pdf.set_font("Helvetica", "", 9)
    for i, ln in enumerate(sorted(invoice.lines, key=lambda x: x.line_no), start=1):
        pdf.set_fill_color(242, 246, 246) if i % 2 == 0 else pdf.set_fill_color(255, 255, 255)
        name = _t(f"{ln.product_name} ({ln.unit})")
        while pdf.get_string_width(name) > cols[1][1] - 2 and len(name) > 4:
            name = name[:-4] + "..."
        values = [str(i), name, _t(ln.batch_no or "-"), _qty(ln.quantity),
                  _qty(ln.bonus_quantity) if ln.bonus_quantity else "-", _money(ln.unit_price),
                  _money(ln.discount) if ln.discount else "-", _money(ln.line_total)]  # fmt: skip
        for (_, cw, align), v in zip(cols, values, strict=True):
            pdf.cell(cw, 6.5, v, align=align, fill=True)
        pdf.ln()
    pdf.set_draw_color(200, 200, 200)
    pdf.set_line_width(0.2)
    pdf.line(pdf.l_margin, pdf.get_y(), pdf.l_margin + w, pdf.get_y())
    pdf.ln(3)

    # Totals
    label_w, val_w = 40, 32
    x = pdf.l_margin + w - label_w - val_w
    rows = [("Subtotal", invoice.subtotal)]
    if invoice.discount:
        rows.append(("Discount", -Decimal(invoice.discount)))
    for label, val in rows:
        pdf.set_x(x)
        pdf.set_font("Helvetica", "", 10)
        pdf.cell(label_w, 6, label)
        pdf.cell(val_w, 6, _money(val), align="R", new_x="LMARGIN", new_y="NEXT")
    pdf.set_x(x)
    pdf.set_font("Helvetica", "B", 12)
    pdf.set_text_color(*brand)
    pdf.cell(label_w, 8, f"Total ({_t(tenant.currency or 'PKR')})")
    pdf.cell(val_w, 8, _money(invoice.total), align="R", new_x="LMARGIN", new_y="NEXT")
    pdf.set_text_color(20, 20, 20)
    if invoice.paid_amount:
        pdf.set_x(x)
        pdf.set_font("Helvetica", "", 10)
        pdf.cell(label_w, 6, "Paid")
        pdf.cell(val_w, 6, _money(invoice.paid_amount), align="R", new_x="LMARGIN", new_y="NEXT")
        pdf.set_x(x)
        pdf.cell(label_w, 6, "Balance due")
        pdf.cell(val_w, 6, _money(Decimal(invoice.total) - Decimal(invoice.paid_amount)), align="R",
                 new_x="LMARGIN", new_y="NEXT")  # fmt: skip

    pdf.ln(10)
    pdf.set_font("Helvetica", "I", 8)
    pdf.set_text_color(120, 120, 120)
    footer = getattr(tenant, "license_text", None) or ""
    pdf.multi_cell(w, 4, _t(f"Computer-generated bill. Thank you for your order. {footer}".strip()), align="C")
    return bytes(pdf.output())
