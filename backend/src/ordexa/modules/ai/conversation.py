"""The shop's follow-up messages about an order that is not billed yet.

Shops change their mind in the chat: "balkay 20 box kar dein" (make it 20 instead), "is ko choro, paracetamol dedo"
(drop this one, send paracetamol), "arinac bhi" (add), "order cancel". This module decides what a message means for
the open order and applies it deterministically:

* The AI (or rule parser) only reads *products and quantities* from the message, as for any order; it never decides
  what happens to the order. Intent comes from fixed marker words plus overlap with the order's items.
* Every change is re-resolved against the catalogue and re-checked by the same guardrails and price/stock checks,
  and a person still approves the bill.
"""

from __future__ import annotations

import re
from datetime import timedelta
from decimal import Decimal
from difflib import SequenceMatcher

from sqlalchemy import select
from sqlalchemy.orm import Session

from ordexa.modules.ai import service as ai
from ordexa.modules.ai.extract import ExtractedLine, Extraction, extract_order, normalise_phrase
from ordexa.modules.ai.models import AiOrderRequest
from ordexa.modules.audit.models import record
from ordexa.modules.catalogue.models import Product
from ordexa.shared.db import utcnow

CHANGE_WINDOW = timedelta(hours=24)

_W = r"(?<![\w])(?:{})(?![\w])"
CORRECTION = re.compile(
    _W.format(
        r"balkay|balke|balki|bal ke|instead|rather|ki jagah|ki jaga|ke bajaye|ki bajaye|is ki bajaye|change|badal|"
        r"badal do|tabdeel|correction|correct|galat|ghalat|update"
    ),
    re.I,
)
REMOVE = re.compile(
    _W.format(
        r"chor|choro|chhoro|chordo|chor do|chhor do|chhod do|chod do|chodo|rehne do|rehnay do|rehne dein|hata|hatao|"
        r"hata do|hatado|remove|nikal do|nikaal do|nahi chahiye|nai chahiye|nahin chahiye|mat bhejo|mat bhejna|"
        r"mat bhejain|cancel|cncl"
    ),
    re.I,
)
ADD = re.compile(_W.format(r"bhi|add|also|is ke sath|iske sath|saath mein|sath mein"), re.I)
WHOLE_ORDER = re.compile(_W.format(r"order|sab|sara|saara|poora|pura|all|everything|sab kuch"), re.I)
POINTER = re.compile(_W.format(r"is ko|isko|isay|isse|is ka|iska|ye wala|yeh wala|ye|yeh|this|that|woh|wo"), re.I)

MATCH_MIN = 0.6


def open_order(session: Session, tenant_id, sender: str) -> AiOrderRequest | None:
    """The shop's latest WhatsApp order, if it is not billed, rejected or cancelled yet (an older unfinished order is
    never reopened once the shop has sent a newer one)."""
    latest = session.scalar(
        select(AiOrderRequest)
        .where(
            AiOrderRequest.tenant_id == tenant_id,
            AiOrderRequest.source == "whatsapp",
            AiOrderRequest.sender == sender,
            AiOrderRequest.received_at >= utcnow() - CHANGE_WINDOW,
        )
        .order_by(AiOrderRequest.received_at.desc())
        .limit(1)
    )
    return latest if latest is not None and latest.status in ("needs_review", "draft_ready") else None


def _strip_markers(text: str) -> str:
    for rx in (CORRECTION, REMOVE, ADD, POINTER):
        text = rx.sub(" ", text)
    return re.sub(r"\s+", " ", text).strip(" ,.!?")


def _fmt_qty(v) -> str:
    if v is None:
        return "?"
    d = Decimal(str(v))
    return f"{d:,.0f}" if d == d.to_integral_value() else f"{d.normalize()}"


def summary(req: AiOrderRequest) -> str:
    items = []
    for ln in req.lines:
        name = ln.get("product_name") or ln.get("phrase")
        unit = ln.get("unit") or ln.get("sale_unit") or ""
        items.append(f"- {name}: {_fmt_qty(ln.get('quantity'))} {unit}".rstrip())
    return "\n".join(items)


def after_change_reply(req: AiOrderRequest, what: str) -> str:
    q = ai.first_question(req)
    if q:
        return f"{what}\nAb yeh bata dein:\n{q}"
    return f"{what} Order {req.number} ab yeh hai:\n{summary(req)}\nHamari team confirm karke bill bhej degi."


def _line_similarity(phrase: str, line: dict, products: dict[str, Product]) -> float:
    p = normalise_phrase(phrase)
    if not p:
        return 0.0
    best = SequenceMatcher(None, p, normalise_phrase(line.get("phrase") or "")).ratio()
    if line.get("read_as"):
        best = max(best, SequenceMatcher(None, p, line["read_as"]).ratio())
    prod = products.get(str(line.get("product_id")))
    if prod is not None:
        best = max(best, ai.score_product(p, prod))
    return best


def _target(lines: list[dict]) -> dict | None:
    """The line the bot is currently asking about."""
    return next((x for x in lines if set(x["issues"]) & ai.BLOCKING), None)


def handle_reply(session: Session, req: AiOrderRequest, text: str) -> str | None:
    """Apply the shop's message to its open order. Returns the bot's reply, or None when the message is a new order."""
    has_markers = bool(CORRECTION.search(text) or REMOVE.search(text) or ADD.search(text))
    asking = req.status == "needs_review" and bool(req.questions)
    if asking and not has_markers and ai.looks_like_answer(req, text):
        return ai.apply_customer_answer(session, req, text)
    if not has_markers and not asking:
        return None  # order is complete and waiting for staff; a new message without change words is a new order

    cleaned = _strip_markers(text)
    lines = [dict(x) for x in req.lines]
    ex = Extraction.model_validate(req.extraction)
    products = {
        str(p.id): p for p in session.scalars(select(Product).where(Product.tenant_id == req.tenant_id, Product.is_active))
    }
    new = Extraction()
    if cleaned and re.search(r"[^\W\d_]", cleaned):
        result = extract_order(cleaned)
        new = result.extraction
        ai._apply_guardrails(new, cleaned, from_photo=False)
        if new.contains_instructions:
            req.warnings = (req.warnings or []) + [
                {"code": "contains_instructions", "message": f"Shop's follow-up tried to instruct the system: {text[:80]!r}"}
            ]
            req.status = "needs_review"
            return "Shukriya! Hamari team aap ka message dekh kar raabta karegi."

    target = _target(lines)
    removing = bool(REMOVE.search(text))
    changed: list[str] = []

    # Map each item in the message to an existing line (same product) or treat it as new.
    matches: list[tuple[ExtractedLine, dict | None]] = []
    for nl in new.lines:
        scored = sorted(((_line_similarity(nl.phrase, ln, products), ln) for ln in lines), key=lambda x: x[0], reverse=True)
        matches.append((nl, scored[0][1] if scored and scored[0][0] >= MATCH_MIN else None))

    if not has_markers and asking and not any(m for _, m in matches):
        # A plain new message while a question is open: a different product without a quantity replaces the item
        # being asked about ("paracetamol" in reply to "which arinac?"); anything else is a new order.
        if not (
            target and len(new.lines) == 1 and not new.lines[0].quantity and set(target["issues"]) & {"ambiguous", "not_found"}
        ):
            return None

    if removing and not new.lines:
        if WHOLE_ORDER.search(text) or target is None or len(lines) <= 1:
            return cancel(session, req, text)
        lines.remove(target)
        changed.append(f"'{target.get('product_name') or target['phrase']}' order se nikal diya.")
    else:
        replaced = False
        for nl, line in matches:
            if line is not None and removing and not nl.quantity:
                lines.remove(line)
                changed.append(f"'{line.get('product_name') or line['phrase']}' order se nikal diya.")
            elif line is not None:
                if nl.quantity:
                    line["quantity"] = nl.quantity
                    line["unit"] = nl.unit
                    line["quantity_confirmed"] = True
                    if line.get("product_id"):
                        line["resolution"] = "customer"
                    changed.append(
                        f"'{line.get('product_name') or line['phrase']}': {_fmt_qty(nl.quantity)} {nl.unit or ''}".rstrip()
                    )
            elif target is not None and not replaced and set(target["issues"]) & {"ambiguous", "not_found"}:
                # "is ko choro, paracetamol dedo" / "balkay paracetamol": swap the item being asked about.
                old = target["phrase"]
                keep_qty = {"quantity": nl.quantity or target.get("quantity"), "unit": nl.unit or target.get("unit")}
                target.update(
                    phrase=nl.phrase, product_id=None, resolution="replaced", candidates=[], read_as=None,
                    _new=nl.model_copy(update=keep_qty),
                )  # fmt: skip
                replaced = True
                changed.append(f"'{old}' ki jagah '{nl.phrase}'.")
            else:
                lines.append({"line_no": None, "phrase": nl.phrase, "issues": [], "_new": nl})
                changed.append(f"'{nl.phrase}' add kar diya.")
        if not changed:
            return None if not has_markers else ai.apply_customer_answer(session, req, text)

    if not lines:
        return cancel(session, req, text)

    # Rebuild the extraction and the lines kept as decided, then re-run every check.
    ex_lines: list[ExtractedLine] = []
    keep: dict[int, dict] = {}
    for i, ln in enumerate(lines, start=1):
        if "_new" in ln:
            ex_lines.append(ln["_new"])
            continue
        old_ex = ex.lines[ln["line_no"] - 1]
        el = old_ex.model_copy(update={"quantity": ln.get("quantity"), "unit": ln.get("unit")})
        ex_lines.append(el)
        if ln.get("resolution") in ("operator", "customer"):
            keep[i] = ln
    ex.lines = ex_lines
    req.extraction = ex.model_dump()
    req.trace = (req.trace or []) + [
        {"step": "shop_changed_order", "message": text[:200], "changes": changed, "at": utcnow().isoformat()}
    ]
    ai.evaluate(session, req, ex, keep=keep)
    record(
        session,
        tenant_id=req.tenant_id,
        action="ai_order.changed_by_shop",
        entity_type="ai_order",
        entity_id=req.id,
        data={"number": req.number, "changes": changed, "status": req.status},
    )
    return after_change_reply(req, "Theek hai: " + " ".join(changed))


def cancel(session: Session, req: AiOrderRequest, text: str) -> str:
    req.status = "rejected"
    req.review_note = "Cancelled by the shop on WhatsApp"
    req.reviewed_at = utcnow()
    req.trace = (req.trace or []) + [{"step": "cancelled_by_shop", "message": text[:200], "at": utcnow().isoformat()}]
    record(
        session,
        tenant_id=req.tenant_id,
        action="ai_order.cancelled_by_shop",
        entity_type="ai_order",
        entity_id=req.id,
        data={"number": req.number},
    )
    return f"Theek hai, order {req.number} cancel kar diya gaya. Naya order kabhi bhi bhej dein."
