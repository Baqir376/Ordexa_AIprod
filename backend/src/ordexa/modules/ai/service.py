"""AI order desk: intake -> extraction -> deterministic resolution and checks -> human approval -> invoice.

The model never touches the database or makes business decisions. It proposes lines; this module resolves them
against the catalogue and approved memory, checks units, stock, price and credit with ERP data, and a person with
``sales_order.approve`` turns the draft into an invoice through the same ``post_invoice`` path every bill uses.
"""

from __future__ import annotations

import hashlib
import logging
import re
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from difflib import SequenceMatcher

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ordexa.config import get_settings
from ordexa.modules.ai import groq, guardrails
from ordexa.modules.ai.extract import (
    ExtractedLine,
    Extraction,
    ExtractResult,
    RuleExtractor,
    extract_order,
    has_urdu_script,
    normalise_phrase,
    sounds_alike,
    urdu_to_latin,
)
from ordexa.modules.ai.models import AiAttachment, AiOrderRequest, ProductAlias
from ordexa.modules.audit.models import record
from ordexa.modules.catalogue.models import Batch, Product
from ordexa.modules.customers.models import Customer
from ordexa.modules.finance.service import balance
from ordexa.modules.identity.service import Principal
from ordexa.modules.inventory.models import InventoryTransaction
from ordexa.modules.jobs.service import enqueue
from ordexa.modules.sales import service as sales
from ordexa.modules.sales.models import Invoice
from ordexa.modules.tenants.models import Warehouse
from ordexa.modules.tenants.timeutil import business_today
from ordexa.shared.errors import ErrorCode, OrdexaError
from ordexa.shared.money import line_total, money, qty
from ordexa.shared.sequences import next_number

log = logging.getLogger("ordexa.ai")

RESOLVE_MIN = 0.75  # top candidate score needed to resolve without a person
RESOLVE_GAP = 0.12  # and its lead over the runner-up
CANDIDATE_MIN = 0.45
BLOCKING = {"ambiguous", "not_found", "missing_quantity", "unit_mismatch", "inactive_product"} | guardrails.BLOCKING_FLAGS
QTY_FLAGS = {"quantity_not_in_message", "unusual_quantity", "fractional_quantity"}


def utcnow() -> datetime:
    return datetime.now(UTC)


# ------------------------------------------------------------------ matching


# Pharmacy-list abbreviations and how shops write them: both sides are mapped to one form before comparing.
FORM_WORDS = {
    "syp": "syrup", "syrp": "syrup", "sirup": "syrup", "shrbat": "syrup",
    "susp": "suspension", "sus": "suspension",
    "tab": "tablet", "tabs": "tablet", "tablets": "tablet", "goli": "tablet", "goliyan": "tablet",
    "cap": "capsule", "caps": "capsule", "capsules": "capsule", "capsul": "capsule",
    "inj": "injection", "injections": "injection", "teeka": "injection", "tika": "injection",
    "inf": "infusion", "drip": "infusion",
    "drop": "drops", "e/d": "drops", "ed": "drops",
    "oint": "ointment", "crm": "cream",
}  # fmt: skip


def _tokens(text: str) -> list[str]:
    return [FORM_WORDS.get(t, t) for t in re.split(r"[\s\-/]+", normalise_phrase(text)) if t]


def _token_hit(tok: str, name_tokens: list[str]) -> bool:
    return _token_weight(tok, name_tokens) > 0


def _token_weight(tok: str, name_tokens: list[str]) -> float:
    """1 for the same word (or "400" vs "400mg"), less for a word that only looks alike ("cefix" vs "cefim")."""
    best = 0.0
    for nt in name_tokens:
        if tok == nt or (tok[:1].isdigit() and nt.startswith(tok)):
            return 1.0
        if len(tok) >= 3 and nt.startswith(tok):
            best = max(best, 0.9)
        elif SequenceMatcher(None, tok, nt).ratio() >= 0.8:
            best = max(best, 0.7)
    return best


def score_product(phrase: str, product: Product) -> float:
    p = normalise_phrase(phrase)
    if not p:
        return 0.0
    if p in (product.code.lower(), (product.barcode or "").lower()):
        return 1.0
    name = normalise_phrase(product.name)
    if p == name:
        return 1.0
    ptoks = _tokens(p)
    ntoks = _tokens(f"{product.name} {product.generic_name or ''}")
    coverage = sum(_token_weight(t, ntoks) for t in ptoks) / len(ptoks)
    if coverage == 0:
        return 0.0
    similar = SequenceMatcher(None, " ".join(ptoks), " ".join(_tokens(product.name))).ratio()
    return round(0.6 * coverage + 0.4 * similar, 3)


def _all_words_in(phrase: str, product: Product) -> bool:
    ntoks = _tokens(product.name)
    ptoks = _tokens(phrase)
    return bool(ptoks) and all(_token_weight(t, ntoks) == 1.0 for t in ptoks)


def clear_winner(searches: list[str], cands: list[tuple[float, Product]]) -> bool:
    """Resolve without asking when the top product is clearly what the shop wrote:
    its exact listed name; or the only product containing every word the shop wrote ("cefix 400" -> CEFIX 400MG CAP,
    not CEFIM); or a clear lead in score."""
    if not cands:
        return False
    if cands[0][0] >= 1.0 and (len(cands) == 1 or cands[1][0] < 1.0):
        return True
    for s in searches:
        full = [p for _, p in cands if _all_words_in(s, p)]
        if len(full) == 1 and full[0] is cands[0][1] and cands[0][0] >= RESOLVE_MIN:
            return True
    return cands[0][0] >= RESOLVE_MIN and (len(cands) == 1 or cands[0][0] - cands[1][0] >= RESOLVE_GAP)


def _find_alias(session: Session, tenant_id: uuid.UUID, customer_id: uuid.UUID | None, phrase: str) -> ProductAlias | None:
    for cid in ([customer_id] if customer_id else []) + [None]:
        q = select(ProductAlias).where(
            ProductAlias.tenant_id == tenant_id, ProductAlias.phrase == phrase, ProductAlias.status == "current"
        )
        q = q.where(ProductAlias.customer_id == cid) if cid else q.where(ProductAlias.customer_id.is_(None))
        alias = session.scalar(q)
        if alias:
            return alias
    return None


def _available(session: Session, tenant_id: uuid.UUID, warehouse_id: uuid.UUID, product_id: uuid.UUID) -> Decimal:
    v = session.scalar(
        select(func.coalesce(func.sum(InventoryTransaction.qty_delta), 0)).where(
            InventoryTransaction.tenant_id == tenant_id,
            InventoryTransaction.warehouse_id == warehouse_id,
            InventoryTransaction.product_id == product_id,
        )
    )
    return Decimal(v)


def _fefo_batches(
    session: Session, tenant_id: uuid.UUID, warehouse_id: uuid.UUID, product_id: uuid.UUID
) -> list[tuple[Batch, Decimal]]:
    """Unexpired batches with their on-hand quantity, earliest expiry first."""
    t = InventoryTransaction
    rows = session.execute(
        select(Batch, func.coalesce(func.sum(t.qty_delta), 0))
        .outerjoin(t, (t.batch_id == Batch.id) & (t.warehouse_id == warehouse_id))
        .where(
            Batch.tenant_id == tenant_id,
            Batch.product_id == product_id,
            Batch.is_active.is_(True),
            (Batch.expiry_date.is_(None)) | (Batch.expiry_date > business_today(session, tenant_id)),
        )
        .group_by(Batch.id)
        .order_by(Batch.expiry_date.nulls_last(), Batch.batch_no)
    ).all()
    return [(b, Decimal(q)) for b, q in rows]


def _price(session: Session, tenant_id: uuid.UUID, warehouse_id: uuid.UUID, product: Product) -> Decimal:
    for b, on_hand in _fefo_batches(session, tenant_id, warehouse_id, product.id):
        if on_hand > 0 and b.sale_price is not None:
            return b.sale_price
        if on_hand > 0:
            break
    return product.sale_price


def _main_warehouse(session: Session, tenant_id: uuid.UUID) -> Warehouse:
    return session.scalar(select(Warehouse).where(Warehouse.tenant_id == tenant_id).order_by(Warehouse.created_at))


# ------------------------------------------------------------------ evaluation


def evaluate(session: Session, req: AiOrderRequest, extraction: Extraction, keep: dict[int, dict] | None = None) -> None:
    """Resolve and check every line; set status, warnings, questions. ``keep`` holds operator-resolved lines
    (by line_no) that must not be re-resolved."""
    keep = keep or {}
    wh = _main_warehouse(session, req.tenant_id)
    products = list(session.scalars(select(Product).where(Product.tenant_id == req.tenant_id)))
    by_id = {p.id: p for p in products}
    trace: list[dict] = []
    lines: list[dict] = []
    for i, ex in enumerate(extraction.lines, start=1):
        phrase = normalise_phrase(ex.phrase)
        searches = [phrase]
        read_as = None
        if has_urdu_script(ex.phrase):
            # Voice notes often come back in Urdu letters; the catalogue is in English letters.
            if ex.search_name and not has_urdu_script(ex.search_name) and sounds_alike(ex.phrase, ex.search_name):
                read_as = normalise_phrase(ex.search_name)
            elif ex.search_name:
                trace.append({"step": "search_name_rejected", "line": i, "search_name": ex.search_name[:60]})
            searches = [s for s in (read_as, normalise_phrase(urdu_to_latin(ex.phrase))) if s]
        line = {
            "line_no": i,
            "phrase": ex.phrase,
            "read_as": read_as,
            "quantity": ex.quantity,
            "unit": ex.unit,
            "note": ex.note,
            "product_id": None,
            "product_name": None,
            "sale_unit": None,
            "resolution": None,
            "alias_id": None,
            "alias_version": None,
            "score": None,
            "candidates": [],
            "issues": [],
            "unit_price": None,
            "line_total": None,
            "available": None,
        }
        if i in keep:
            line.update(
                {k: keep[i][k] for k in ("product_id", "quantity", "resolution", "unit", "quantity_confirmed") if k in keep[i]}
            )
        # Guardrail flags from extraction; a person (operator) or the shop's own answer clears what it confirms.
        flags = list(ex.flags)
        if line["resolution"] == "operator":
            flags = []
        elif line["resolution"] == "customer" and keep.get(i, {}).get("quantity_confirmed"):
            flags = [f for f in flags if f not in QTY_FLAGS]
        line["issues"].extend(flags)
        product = None
        if line["product_id"]:
            product = by_id.get(uuid.UUID(str(line["product_id"])))
        if product is None:
            alias = next(
                (a for a in (_find_alias(session, req.tenant_id, req.customer_id, s) for s in [phrase, *searches]) if a), None
            )
            if alias and by_id.get(alias.product_id):
                product = by_id[alias.product_id]
                line.update(resolution="alias", alias_id=str(alias.id), alias_version=alias.version, score=1.0)
                trace.append(
                    {
                        "step": "alias",
                        "line": i,
                        "phrase": phrase,
                        "alias_id": str(alias.id),
                        "version": alias.version,
                        "scope": "customer" if alias.customer_id else "business",
                    }
                )
            else:
                scored = sorted(
                    ((max(score_product(s, p) for s in searches), p) for p in products if p.is_active),
                    key=lambda x: x[0],
                    reverse=True,
                )
                cands = [(s, p) for s, p in scored if s >= CANDIDATE_MIN][:5]
                line["candidates"] = [
                    {"product_id": str(p.id), "name": p.name, "code": p.code, "generic": p.generic_name, "score": s}
                    for s, p in cands
                ]
                trace.append(
                    {
                        "step": "catalogue_search",
                        "line": i,
                        "phrase": phrase,
                        "searched": searches,
                        "top": [{"code": p.code, "score": s} for s, p in cands[:3]],
                    }
                )
                if clear_winner(searches, cands):
                    product = cands[0][1]
                    line.update(resolution="catalogue", score=cands[0][0])
                elif cands:
                    line["resolution"] = "ambiguous"
                    line["issues"].append("ambiguous")
                else:
                    line["resolution"] = "not_found"
                    line["issues"].append("not_found")
        if product is not None:
            line.update(product_id=str(product.id), product_name=product.name, sale_unit=product.sale_unit)
            if not product.is_active:
                line["issues"].append("inactive_product")
            if line["unit"] and line["unit"] != product.sale_unit and line.get("resolution") not in ("operator", "customer"):
                # Never convert units from text (SRS 7.5): a person confirms the quantity in the selling unit.
                line["issues"].append("unit_mismatch")
            price = _price(session, req.tenant_id, wh.id, product)
            line["unit_price"] = str(money(price))
            avail = _available(session, req.tenant_id, wh.id, product.id)
            line["available"] = str(qty(avail))
            trace.append(
                {
                    "step": "stock_price",
                    "line": i,
                    "product": product.code,
                    "available": str(qty(avail)),
                    "price": str(money(price)),
                    "checked_at": utcnow().isoformat(),
                }
            )
            if line["quantity"]:
                line["line_total"] = str(line_total(Decimal(str(line["quantity"])), price))
                if avail < Decimal(str(line["quantity"])):
                    line["issues"].append("insufficient_stock")  # warning only: never silently reduce quantity
        if not line["quantity"] or float(line["quantity"]) <= 0:
            line["issues"].append("missing_quantity")
        lines.append(line)

    warnings: list[dict] = []
    if not lines and req.kind == "audio":
        warnings.append(
            {
                "code": "voice_note",
                "message": "Voice note: play it and add the items with '+ Add item'"
                + (" (set a free GROQ_API_KEY to transcribe voice notes automatically)." if not groq.configured() else "."),
            }
        )
    elif not lines and req.kind == "image":
        how = (
            "set a free GROQ_API_KEY to read photos automatically"
            if not (req.extractor or "").startswith(("anthropic", "groq"))
            else "the photo could not be read"
        )
        warnings.append({"code": "photo_unread", "message": f"Photo order: look at the picture and add the items ({how})."})
    elif not lines:
        warnings.append({"code": "no_lines", "message": "No order lines could be read from this message."})
    if req.customer_id is None:
        warnings.append({"code": "unknown_customer", "message": "Sender is not linked to a customer."})
    if extraction.contains_instructions:
        kinds = ", ".join(extraction.injection) if extraction.injection else "discount, approval, credit..."
        warnings.append(
            {
                "code": "contains_instructions",
                "message": f"Message tries to instruct the system ({kinds}). It was not acted on; review it yourself.",
            }
        )
    warnings.extend(extraction.guard_warnings)
    total = sum((Decimal(ln["line_total"]) for ln in lines if ln["line_total"]), Decimal(0))
    if req.customer_id:
        c = session.get(Customer, req.customer_id)
        if c and c.credit_limit is not None:
            owed = balance(session, req.tenant_id, "customer", c.id)
            trace.append({"step": "credit", "balance": str(owed), "limit": str(c.credit_limit), "order_total": str(total)})
            if owed + total > c.credit_limit:
                warnings.append(
                    {
                        "code": "credit_review",
                        "message": f"Balance {owed} + this order {money(total)} exceeds credit limit {c.credit_limit}.",
                    }
                )
        if c and not c.is_active:
            warnings.append({"code": "customer_inactive", "message": f"{c.name} is inactive."})

    questions = []
    for ln in lines:
        ln_phrase = guardrails.safe_echo(ln["phrase"])
        if ln.get("read_as"):
            ln_phrase = f"{ln_phrase} ({guardrails.safe_echo(ln['read_as'])})"
        if "not_in_message" in ln["issues"]:
            continue  # a reading error for staff to fix, not a question for the shop
        if "unusual_quantity" in ln["issues"]:
            questions.append(
                f"'{ln_phrase}': {_qty_text(ln['quantity'])} {ln.get('unit') or ''} bohat zyada hai. "
                f"Sahi quantity sirf number mein likh dein."
            )
        elif "fractional_quantity" in ln["issues"]:
            u = ln.get("unit") or ln.get("sale_unit") or ""
            questions.append(f"'{ln_phrase}': {_qty_text(ln['quantity'])} {u} — hum poore {u} mein bechte hain. Kitne {u}?")
        elif "quantity_not_in_message" in ln["issues"]:
            questions.append(f"'{ln_phrase}': kitni quantity chahiye? Please confirm the quantity.")
        elif "ambiguous" in ln["issues"]:
            cands = ln["candidates"][:4]
            words = set(normalise_phrase(ln.get("read_as") or ln["phrase"]).split())
            by_generic = all(
                words & set(normalise_phrase(c.get("generic") or "").split())
                and not words & set(normalise_phrase(c["name"]).split())
                for c in cands
            )
            if by_generic:
                # The shop named an ingredient (e.g. paracetamol), not a product: say these are brands containing it.
                opts = "\n".join(f"{k + 1}) {c['name']} ({c['generic']})" for k, c in enumerate(cands))
                questions.append(
                    f"'{ln_phrase}' naam ki item hamari list mein nahi hai, lekin yeh brands is mein aate hain. "
                    f"Konsa chahiye? Number likh dein:\n{opts}"
                )
            elif len(cands) == 1:
                questions.append(f"'{ln_phrase}' se murad '{cands[0]['name']}' hai? Haan ya nahi likhein.")
            else:
                opts = "\n".join(f"{k + 1}) {c['name']}" for k, c in enumerate(cands))
                questions.append(f"'{ln_phrase}' mein se konsa chahiye? Number likh dein:\n{opts}")
        elif "not_found" in ln["issues"]:
            questions.append(
                f"Maazrat, '{ln_phrase}' hamari list mein match nahi hua. Dawai ka naam English mein likh dein, "
                f"jaise 'Panadol Extra' ya 'Calpol Syrup'."
            )
        elif "missing_quantity" in ln["issues"]:
            questions.append(f"'{ln_phrase}': kitni quantity chahiye? How many?")
        elif "unit_mismatch" in ln["issues"]:
            questions.append(
                f"'{ln_phrase}': {_qty_text(ln['quantity'])} {ln['unit']} — hum {ln['sale_unit']} mein bechte hain. "
                f"Kitne {ln['sale_unit']}?"
            )

    blocked = any(set(ln["issues"]) & BLOCKING for ln in lines) or not lines or req.customer_id is None
    req.lines = lines
    req.warnings = warnings
    req.questions = questions
    req.trace = (req.trace or []) + trace
    req.status = "needs_review" if blocked or extraction.contains_instructions else "draft_ready"
    req.processed_at = utcnow()


def _apply_guardrails(extraction: Extraction, source: str, *, from_photo: bool) -> None:
    """Deterministic checks on whatever the model (or rule parser) returned. The model cannot set flags itself."""
    for line in extraction.lines:
        line.flags = []
    extraction.injection = guardrails.detect_injection(source)
    if extraction.injection:
        extraction.contains_instructions = True
    extraction.guard_warnings = guardrails.check(extraction, source, from_photo=from_photo)


def process_request(session: Session, request_id: uuid.UUID, *, defer_on_rate_limit: bool = False) -> AiOrderRequest:
    # Row lock: a second processor (worker retry, simulator) waits here and then sees the order already processed.
    req = session.get(AiOrderRequest, request_id, with_for_update=True)
    if req is None:
        raise LookupError(f"AI request {request_id} not found")
    if req.status not in ("received", "processing", "failed"):
        return req  # already processed (job retried after success)
    req.status = "processing"
    voice_trace = None
    image = None
    text = req.raw_text
    if req.attachment_id:
        att = session.get(AiAttachment, req.attachment_id)
        if req.kind == "image":
            image = (att.data, att.media_type)
            text = "" if req.raw_text.startswith("[") else req.raw_text
        elif req.kind == "audio":
            if att.transcript is None and groq.configured():
                try:
                    att.transcript = groq.transcribe(att.data, att.media_type)
                    voice_trace = {"step": "speech_to_text", "provider": "groq", "chars": len(att.transcript)}
                except Exception as exc:  # noqa: BLE001 - staff can still listen to the voice note
                    voice_trace = {"step": "speech_to_text", "provider": "groq", "error": str(exc)[:300]}
            text = att.transcript or ""  # transcript read like a text order; without one, staff listen
    source = text
    clean = guardrails.sanitize_input(text)
    result = (
        extract_order(clean, image, defer_on_rate_limit=defer_on_rate_limit)
        if (clean or image)
        else ExtractResult(Extraction(), "none", [])
    )
    _apply_guardrails(result.extraction, source, from_photo=image is not None)
    _classify(result.extraction, source, result.extractor)
    _merge_duplicates(result.extraction)
    req.extractor = result.extractor
    req.extraction = result.extraction.model_dump()
    carried = [t for t in (req.trace or []) if t.get("step") in ("after_billed_order", "add_to_bill")]  # set on arrival
    req.trace = carried + ([voice_trace] if voice_trace else []) + result.trace
    req.error = None
    evaluate(session, req, result.extraction)
    record(
        session,
        tenant_id=req.tenant_id,
        action="ai_order.processed",
        entity_type="ai_order",
        entity_id=req.id,
        data={"number": req.number, "status": req.status, "extractor": req.extractor, "lines": len(req.lines)},
    )
    if req.source == "whatsapp":
        respond(session, req, result.extraction)
    return req


GREETING_WORDS = r"shukriya|shukria|thanks?|thank you|jazak ?allah|salam|assalam ?o ?alaikum|aoa|regards|sir|bhai|ji|please|order"

NON_ORDER_KINDS = {"balance", "bill_request", "order_status", "repeat_last_order"}


def _classify(ex: Extraction, source: str, extractor: str) -> None:
    """Rule-based message type when no AI read the message, or when the AI found no items in an "order"."""
    from ordexa.modules.ai.extract import rule_message_type

    if not extractor.startswith(("groq", "anthropic")):
        keyword_kind, _ = rule_message_type(source, False)
        has_qty = any(ln.quantity for ln in ex.lines)
        # "shop 101 ka balance": a number without a unit next to balance/bill/status words is not an order.
        if has_qty and keyword_kind in NON_ORDER_KINDS and not any(ln.unit for ln in ex.lines if ln.quantity):
            has_qty = False
            ex.lines = []
        ex.message_type, ex.asks_price = rule_message_type(source, has_qty)
        if ex.message_type == "other" and ex.lines:
            ex.message_type = "order"  # "panadol extra bhej do": an order without a quantity (the bot asks)
    elif not ex.lines and ex.message_type == "order":
        ex.message_type = rule_message_type(source, False)[0]
    if not ex.lines and not any(ch.isalpha() for ch in source or ""):
        ex.message_type = "other"  # "?", "..." - send the help text, not a staff ticket


def _merge_duplicates(ex: Extraction) -> None:
    """'5 box panadol extra, aur 5 box panadol extra bhi' -> one line of 10 (the reply shows the total to the shop)."""
    seen: dict[tuple, ExtractedLine] = {}
    merged = False
    out = []
    for ln in ex.lines:
        key = (normalise_phrase(ln.phrase), ln.unit)
        first = seen.get(key)
        if first is not None and first.quantity is not None and ln.quantity is not None and not ln.flags and not first.flags:
            first.quantity = first.quantity + ln.quantity
            merged = True
            continue
        seen.setdefault(key, ln)
        out.append(ln)
    if merged:
        ex.lines = out
        ex.guard_warnings.append(
            {"code": "merged_duplicates", "message": "The same item was written twice; quantities were added up."}
        )


def respond(session: Session, req: AiOrderRequest, ex: Extraction) -> None:
    """Reply to the shop according to what the message was, and close requests that are not orders."""
    from ordexa.modules.ai import replies

    if not req.sender:
        return
    kind = ex.message_type
    real = [ln for ln in req.lines if ln.get("quantity")]
    is_order = kind == "order" or bool(real)  # words the rule parser took for items in "mera hisaab batao" are not an order
    body: str | None
    prefix = ""
    for t in req.trace or []:
        if t.get("step") == "after_billed_order":
            prefix = f"Order {t['number']} ka bill ban chuka tha, is liye yeh naya order hai.\n"
        if t.get("step") == "add_to_bill":
            prefix = f"Yeh items aap ke aaj ke bill {t['number']} mein add kar diye jayenge.\n"
    if req.customer_id is None:
        body = replies.order_ack(req, asks_price=False) if req.lines else None
    elif kind == "price_question" and not real:
        if not req.lines:  # some models leave the products out of a pure rate question: read their names by rule
            named = [ln for ln in RuleExtractor().extract(req.raw_text).extraction.lines if ln.phrase]
            if named:
                evaluate(session, req, Extraction(lines=named))
        body = replies.price_text(session, req)
        _close(req, "Rates sent to the shop")
    elif kind == "repeat_last_order" and not real:
        inv, lines, keep = replies.repeat_lines(session, req)
        if inv is None:
            body = "Aap ka koi pichla bill nahi mila. " + replies.HELP
            _close(req, "No previous bill to repeat")
        else:
            ex.lines = lines
            req.extraction = ex.model_dump()
            req.trace = (req.trace or []) + [{"step": "repeat_of_bill", "invoice": inv.number}]
            evaluate(session, req, ex, keep=keep)
            body = replies.order_ack(req, asks_price=ex.asks_price, prefix=f"Pichla order (bill {inv.number}) dobara:\n")
    elif req.lines and is_order:
        body = replies.order_ack(req, asks_price=ex.asks_price, prefix=prefix)
        if ex.asks_price and req.status == "needs_review":
            body = replies.price_text(session, req, footer=False) + "\n\n" + body
        note = (ex.non_order_text or "").strip()
        greeting_only = bool(re.fullmatch(r"[\W_]*(?:" + GREETING_WORDS + r")?[\W_]*", note.lower()))
        if kind in ("complaint", "question") or (len(note) >= 15 and not greeting_only):
            # e.g. "pichli dafa 2 botal tooti hui thi" next to the order: staff must see it, the shop is told so.
            req.warnings = (req.warnings or []) + [
                {"code": "shop_question", "message": f"Shop also wrote: {note or req.raw_text[:200]}"}
            ]
            body += "\nAap ki baat bhi hamari team tak pohancha di hai."
    elif req.kind in ("image", "audio") and not req.lines:
        what = "photo" if req.kind == "image" else "voice message"
        body = f"Shukriya! Aap ka {what} mil gaya (order {req.number}). Hamari team check karke confirm karegi."
    elif ex.contains_instructions or kind in ("complaint", "question"):
        req.status = "message"
        req.warnings = [{"code": "shop_question", "message": f"Shop wrote: {req.raw_text[:300]}"}]
        body = (
            "Takleef ke liye maazrat. Aap ki shikayat hamari team ko bhej di hai, woh jald raabta karegi."
            if kind == "complaint"
            else "Aap ka message hamari team ko bhej diya hai, woh jald jawab degi."
        )
    elif kind == "balance":
        body = replies.balance_text(session, req.tenant_id, req.customer_id)
        _close(req, "Balance sent to the shop")
    elif kind == "bill_request":
        body = replies.send_bill(session, req) or None
        _close(req, "Last bill sent to the shop")
    elif kind == "order_status":
        body = replies.status_text(session, req)
        if "Delivery ke waqt" in body:
            req.status = "message"
            req.warnings = [{"code": "shop_question", "message": f"Shop asks: {req.raw_text[:300]}"}]
        else:
            _close(req, "Order status sent to the shop")
    elif kind == "greeting_thanks":
        body = "Shukriya! " + replies.HELP
        _close(req, "Greeting")
    else:
        body = "Maazrat, samajh nahi aaya. " + replies.HELP
        _close(req, "Not understood; help sent")
    if body:
        _queue_reply(session, req, body)


def _close(req: AiOrderRequest, note: str) -> None:
    """Not an order: answered by the bot, nothing for staff to approve."""
    req.status = "closed"
    req.review_note = note
    req.lines = []
    req.questions = []


def _queue_reply(session: Session, req: AiOrderRequest, body: str) -> None:
    key = hashlib.sha256(body.encode()).hexdigest()[:16]
    enqueue(
        session,
        kind="whatsapp.send",
        queue="outbox",
        tenant_id=req.tenant_id,
        idempotency_key=f"wa-reply:{req.id}:{key}",
        payload={"to": req.sender, "body": body, "ref": req.number},
    )


def _qty_text(v) -> str:
    d = Decimal(str(v))
    return f"{d:,.0f}" if d == d.to_integral_value() else f"{d.normalize()}"


def first_question(req: AiOrderRequest) -> str | None:
    return req.questions[0] if req.questions else None


def _notify_customer(session: Session, req: AiOrderRequest) -> None:
    """Queue a WhatsApp reply (sent by the outbox worker). Questions go one at a time so a reply like "2" is
    unambiguous; the shop's answer is applied by ``apply_customer_answer``."""
    if not req.sender:
        return
    q = first_question(req)
    if req.status == "needs_review" and q:
        more = f" ({len(req.questions) - 1} aur sawal baqi)" if len(req.questions) > 1 else ""
        body = f"Shukriya! Order {req.number} mil gaya. Aik cheez confirm karein{more}:\n{q}"
    elif req.kind in ("image", "audio") and not req.lines:
        what = "photo" if req.kind == "image" else "voice message"
        body = f"Shukriya! Aap ka {what} mil gaya (order {req.number}). Hamari team check karke confirm karegi."
    else:
        body = (
            f"Shukriya! Order {req.number} mil gaya ({len(req.lines)} items). Hamari team confirm karke bill "
            f"bhej degi. / Order received, we will confirm shortly."
        )
    key = hashlib.sha256(body.encode()).hexdigest()[:16]
    enqueue(
        session,
        kind="whatsapp.send",
        queue="outbox",
        tenant_id=req.tenant_id,
        idempotency_key=f"wa-reply:{req.id}:{key}",
        payload={"to": req.sender, "body": body, "ref": req.number},
    )


# ------------------------------------------------------------------ the shop answers a question on WhatsApp

ANSWER_WINDOW = timedelta(hours=24)


def open_clarification(session: Session, tenant_id: uuid.UUID, sender: str) -> AiOrderRequest | None:
    """The shop's latest WhatsApp order that is still waiting for an answer from them."""
    return session.scalar(
        select(AiOrderRequest)
        .where(
            AiOrderRequest.tenant_id == tenant_id,
            AiOrderRequest.source == "whatsapp",
            AiOrderRequest.sender == sender,
            AiOrderRequest.status == "needs_review",
            AiOrderRequest.received_at >= utcnow() - ANSWER_WINDOW,
        )
        .order_by(AiOrderRequest.received_at.desc())
        .limit(1)
    )


def _number(text: str) -> float | None:
    from ordexa.modules.ai.extract import NUMBER_WORDS, URDU_DIGITS

    t = text.translate(URDU_DIGITS).strip().lower().rstrip(".")
    m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*[a-z]*", t)
    if m:
        return float(m.group(1))
    return float(NUMBER_WORDS[t]) if t in NUMBER_WORDS else None


def looks_like_answer(req: AiOrderRequest, text: str) -> bool:
    """A short reply to the open question ("2", "extra wali", "5 box") rather than a new order."""
    if not req.questions or not text.strip() or len(text) > 60:
        return False
    if _number(text) is not None:
        return True
    lines = RuleExtractor().extract(text).extraction.lines
    # A new order normally names a product with a quantity; an answer usually does not.
    return len(lines) <= 1 and not any(ln.quantity for ln in lines) and len(text.split()) <= 4


def apply_customer_answer(session: Session, req: AiOrderRequest, text: str) -> str:
    """Apply the shop's answer to the first open question; returns the reply to send back.
    A shop's choice is not saved as approved memory (only staff approve aliases)."""
    lines = [dict(x) for x in req.lines]
    target = next((x for x in lines if set(x["issues"]) & BLOCKING), None)
    if target is None:
        return "Shukriya! Order update ho gaya."
    issues = set(target["issues"])
    ex = Extraction.model_validate(req.extraction)
    answered = False
    low = re.sub(r"[\s.!]+", " ", text.lower()).strip()
    cands = target.get("candidates") or []
    if "ambiguous" in issues and len(cands) == 1 and low in YES_WORDS:
        text = "1"
    if "ambiguous" in issues and len(cands) == 1 and low in NO_WORDS:
        return "Theek hai, sahi dawai ka naam likh dein."
    if "ambiguous" in issues and 2 <= len(cands) <= 3 and re.search(r"(?<!\w)(dono|donon|both|teeno|sab)(?!\w)", low):
        return _take_all_options(session, req, lines, target, text)
    if issues & {"ambiguous", "not_found"}:
        n = _number(text)
        if n is not None and "ambiguous" in issues and n == int(n) and not 1 <= int(n) <= len(cands):
            return f"Maazrat, {int(n)} number list mein nahi hai. 1 se {len(cands)} tak likhein.\n{first_question(req)}"
        if n is not None and "ambiguous" in issues and 1 <= int(n) <= len(target["candidates"]):
            target["product_id"] = target["candidates"][int(n) - 1]["product_id"]
            target["resolution"] = "customer"
            answered = True
        else:
            products = {
                str(p.id): p
                for p in session.scalars(select(Product).where(Product.tenant_id == req.tenant_id, Product.is_active.is_(True)))
            }
            pool = [products[c["product_id"]] for c in target["candidates"] if c["product_id"] in products]
            cands = pool or list(products.values())
            combined = f"{target['phrase']} {text}" if pool else text
            scored = sorted(
                ((max(score_product(text, p), score_product(combined, p)), p) for p in cands),
                key=lambda x: x[0],
                reverse=True,
            )
            if scored and scored[0][0] >= 0.6 and (len(scored) == 1 or scored[0][0] - scored[1][0] >= RESOLVE_GAP):
                target["product_id"] = str(scored[0][1].id)
                target["resolution"] = "customer"
                answered = True
            else:
                # Not one of the options: the shop named another product ("paracetamol"). Search the whole catalogue
                # with its wording; for a "which one?" question only if that wording matches something we sell.
                found = RuleExtractor().extract(text).extraction.lines
                wording = found[0].phrase if len(found) == 1 else text.strip()
                if "not_found" in issues or any(score_product(wording, p) >= CANDIDATE_MIN for p in products.values()):
                    ex.lines[target["line_no"] - 1].phrase = wording
                    ex.lines[target["line_no"] - 1].search_name = None
                    answered = True
    elif issues & ({"missing_quantity", "unit_mismatch"} | QTY_FLAGS):
        n = _number(text)
        if n is not None and 0 < n <= guardrails.MAX_QTY:
            target["quantity"] = n
            target["unit"] = None
            target["resolution"] = "customer"
            target["quantity_confirmed"] = True
            answered = True
    if not answered:
        q = first_question(req)
        return f"Maazrat, samajh nahi aaya. {q}" if q else "Maazrat, samajh nahi aaya."
    keep = {x["line_no"]: x for x in lines if x.get("resolution") in ("operator", "customer")}
    req.extraction = ex.model_dump()
    req.trace = (req.trace or []) + [{"step": "customer_answer", "answer": text[:300], "at": utcnow().isoformat()}]
    evaluate(session, req, ex, keep=keep)
    record(
        session,
        tenant_id=req.tenant_id,
        action="ai_order.customer_answered",
        entity_type="ai_order",
        entity_id=req.id,
        data={"number": req.number, "status": req.status},
    )
    return _after_answer(req)


YES_WORDS = {"haan", "han", "ha", "haa", "ji", "jee", "ji haan", "haan ji", "yes", "y", "sahi", "theek", "ok", "okay", "1"}
NO_WORDS = {"nahi", "nahin", "nhi", "nai", "na", "no", "n", "ghalat", "galat"}


def _after_answer(req: AiOrderRequest) -> str:
    from ordexa.modules.ai import replies

    q = first_question(req)
    if q:
        return f"Shukriya! Ab yeh bata dein:\n{q}"
    return (
        f"Shukriya! Order {req.number} update ho gaya:\n{replies.items_text(req.lines)}\n"
        f"Hamari team confirm karke bill bhej degi."
    )


def _take_all_options(session: Session, req: AiOrderRequest, lines: list[dict], target: dict, text: str) -> str:
    """'dono' in answer to 'which one?': order each option with the same quantity (the reply lists them)."""
    ex = Extraction.model_validate(req.extraction)
    new_ex, keep = [], {}
    for ln in lines:
        src = ex.lines[ln["line_no"] - 1]
        if ln is target:
            for c in target["candidates"][:3]:
                new_ex.append(src.model_copy(update={"phrase": c["name"][:200], "search_name": None}))
                keep[len(new_ex)] = {"product_id": c["product_id"], "resolution": "customer"}
        else:
            new_ex.append(src)
            if ln.get("resolution") in ("operator", "customer"):
                keep[len(new_ex)] = ln
    ex.lines = new_ex
    req.extraction = ex.model_dump()
    req.trace = (req.trace or []) + [{"step": "customer_answer", "answer": text[:300], "at": utcnow().isoformat()}]
    evaluate(session, req, ex, keep=keep)
    return _after_answer(req)


# ------------------------------------------------------------------ intake


def submit(
    session: Session,
    *,
    tenant_id: uuid.UUID,
    text: str,
    source: str,
    source_message_id: str,
    customer_id: uuid.UUID | None,
    sender: str | None = None,
    kind: str = "text",
    attachment_id: uuid.UUID | None = None,
) -> tuple[AiOrderRequest, bool]:
    """Create a request (idempotent per source message) and queue it for the AI worker."""
    text = (text or "").strip()
    if not text and kind in ("image", "audio"):
        text = "[photo order]" if kind == "image" else "[voice note]"
    if not text:
        raise OrdexaError(ErrorCode.VALIDATION_ERROR, "order text is empty")
    if len(text) > 4000:
        raise OrdexaError(ErrorCode.VALIDATION_ERROR, "order text is too long (4000 characters max)")
    existing = session.scalar(
        select(AiOrderRequest).where(
            AiOrderRequest.tenant_id == tenant_id,
            AiOrderRequest.source == source,
            AiOrderRequest.source_message_id == source_message_id,
        )
    )
    if existing:
        return existing, False
    if customer_id:
        c = session.get(Customer, customer_id)
        if not c or c.tenant_id != tenant_id:
            raise OrdexaError(ErrorCode.NOT_FOUND, "customer not found")
    req = AiOrderRequest(
        tenant_id=tenant_id,
        number=next_number(session, tenant_id, "AO"),
        source=source,
        source_message_id=source_message_id,
        sender=sender,
        customer_id=customer_id,
        raw_text=text,
        kind=kind,
        attachment_id=attachment_id,
        status="received",
        lines=[],
        warnings=[],
        questions=[],
        trace=[],
    )
    session.add(req)
    session.flush()
    enqueue(
        session,
        kind="ai.process_order",
        queue="ai",
        tenant_id=tenant_id,
        idempotency_key=f"ai:{req.id}",
        payload={"request_id": str(req.id)},
    )
    record(
        session,
        tenant_id=tenant_id,
        action="ai_order.received",
        entity_type="ai_order",
        entity_id=req.id,
        data={"number": req.number, "source": source},
    )
    return req, True


def run_inline_if_configured(session: Session, req: AiOrderRequest) -> None:
    if get_settings().ai_inline:
        process_request(session, req.id)


# ------------------------------------------------------------------ operator actions


def _get(session: Session, actor: Principal, request_id: uuid.UUID) -> AiOrderRequest:
    req = session.get(AiOrderRequest, request_id)
    if not req or req.tenant_id != actor.tenant_id:
        raise OrdexaError(ErrorCode.NOT_FOUND, "order request not found")
    return req


def _open(req: AiOrderRequest) -> None:
    if req.status in ("approved", "rejected"):
        raise OrdexaError(ErrorCode.CONFLICT, f"order is already {req.status}")
    if req.status in ("received", "processing"):
        raise OrdexaError(ErrorCode.CONFLICT, "order is still being processed")


def save_alias(
    session: Session,
    actor: Principal,
    *,
    phrase: str,
    product_id: uuid.UUID,
    customer_id: uuid.UUID | None,
    reason: str | None,
    source_request_id: uuid.UUID | None = None,
) -> ProductAlias:
    """Approve a phrase -> product mapping. Supersedes (never overwrites) the current version in one transaction."""
    actor.require("ai_alias.approve")
    product = session.get(Product, product_id)
    if not product or product.tenant_id != actor.tenant_id:
        raise OrdexaError(ErrorCode.NOT_FOUND, "product not found")
    if customer_id:
        c = session.get(Customer, customer_id)
        if not c or c.tenant_id != actor.tenant_id:
            raise OrdexaError(ErrorCode.NOT_FOUND, "customer not found")
    norm = normalise_phrase(phrase)
    if not norm:
        raise OrdexaError(ErrorCode.VALIDATION_ERROR, "phrase is empty")
    q = (
        select(ProductAlias)
        .where(ProductAlias.tenant_id == actor.tenant_id, ProductAlias.phrase == norm, ProductAlias.status == "current")
        .with_for_update()
    )
    q = q.where(ProductAlias.customer_id == customer_id) if customer_id else q.where(ProductAlias.customer_id.is_(None))
    current = session.scalar(q)
    if current and current.product_id == product.id:
        return current
    if current:
        current.status = "superseded"
        session.flush()
    alias = ProductAlias(
        tenant_id=actor.tenant_id,
        customer_id=customer_id,
        phrase=norm,
        product_id=product.id,
        version=(current.version + 1) if current else 1,
        supersedes_id=current.id if current else None,
        source_request_id=source_request_id,
        reason=reason,
        created_by=actor.user_id,
    )
    session.add(alias)
    session.flush()
    record(
        session,
        tenant_id=actor.tenant_id,
        actor_user_id=actor.user_id,
        action="ai_alias.saved",
        entity_type="ai_alias",
        entity_id=alias.id,
        data={
            "phrase": norm,
            "product": product.code,
            "customer_id": str(customer_id),
            "version": alias.version,
            "supersedes": str(current.id) if current else None,
        },
    )
    return alias


def revoke_alias(session: Session, actor: Principal, alias_id: uuid.UUID) -> None:
    actor.require("ai_alias.approve")
    a = session.get(ProductAlias, alias_id)
    if not a or a.tenant_id != actor.tenant_id or a.status != "current":
        raise OrdexaError(ErrorCode.NOT_FOUND, "current alias not found")
    a.status = "revoked"
    record(
        session,
        tenant_id=actor.tenant_id,
        actor_user_id=actor.user_id,
        action="ai_alias.revoked",
        entity_type="ai_alias",
        entity_id=a.id,
        data={"phrase": a.phrase},
    )


def resolve_line(
    session: Session,
    actor: Principal,
    request_id: uuid.UUID,
    *,
    line_no: int,
    product_id: uuid.UUID | None,
    quantity: Decimal | None,
    remember: bool,
) -> AiOrderRequest:
    """Operator correction of one line (product and/or quantity in the selling unit)."""
    actor.require("sales_order.create")
    req = _get(session, actor, request_id)
    _open(req)
    lines = [dict(x) for x in req.lines]
    line = next((x for x in lines if x["line_no"] == line_no), None)
    if line is None:
        raise OrdexaError(ErrorCode.NOT_FOUND, f"line {line_no} not found")
    if product_id is not None:
        p = session.get(Product, product_id)
        if not p or p.tenant_id != actor.tenant_id:
            raise OrdexaError(ErrorCode.NOT_FOUND, "product not found")
        line["product_id"] = str(p.id)
        if remember:
            save_alias(
                session,
                actor,
                phrase=line["phrase"],
                product_id=p.id,
                customer_id=req.customer_id,
                reason=f"confirmed on {req.number}",
                source_request_id=req.id,
            )
    if quantity is not None:
        if quantity <= 0:
            raise OrdexaError(ErrorCode.VALIDATION_ERROR, "quantity must be positive")
        line["quantity"] = float(quantity)
    line["resolution"] = "operator"
    line["unit"] = None  # quantity is now confirmed in the product's selling unit
    keep = {x["line_no"]: x for x in lines if x.get("resolution") in ("operator", "customer")}
    req.trace = (req.trace or []) + [
        {
            "step": "operator_resolve",
            "line": line_no,
            "by": actor.username,
            "product_id": line["product_id"],
            "quantity": line["quantity"],
            "remember": remember,
        }
    ]
    ex = Extraction.model_validate(req.extraction)
    ex.lines[line_no - 1].flags = []  # a person confirmed this line
    req.extraction = ex.model_dump()
    evaluate(session, req, ex, keep=keep)
    record(
        session,
        tenant_id=actor.tenant_id,
        actor_user_id=actor.user_id,
        action="ai_order.line_resolved",
        entity_type="ai_order",
        entity_id=req.id,
        data={"line": line_no, "remember": remember},
    )
    return req


def add_line(
    session: Session, actor: Principal, request_id: uuid.UUID, *, product_id: uuid.UUID, quantity: Decimal
) -> AiOrderRequest:
    actor.require("sales_order.create")
    req = _get(session, actor, request_id)
    _open(req)
    p = session.get(Product, product_id)
    if not p or p.tenant_id != actor.tenant_id:
        raise OrdexaError(ErrorCode.NOT_FOUND, "product not found")
    ex = Extraction.model_validate(req.extraction or {"lines": []})
    ex.lines.append(ExtractedLine(phrase=p.name, quantity=float(quantity), unit=None, note="added by operator"))
    req.extraction = ex.model_dump()
    keep = {x["line_no"]: x for x in req.lines if x.get("resolution") in ("operator", "customer")}
    keep[len(ex.lines)] = {"product_id": str(p.id), "quantity": float(quantity), "resolution": "operator", "unit": None}
    evaluate(session, req, ex, keep=keep)
    return req


def remove_line(session: Session, actor: Principal, request_id: uuid.UUID, line_no: int) -> AiOrderRequest:
    actor.require("sales_order.create")
    req = _get(session, actor, request_id)
    _open(req)
    ex = Extraction.model_validate(req.extraction)
    if not 1 <= line_no <= len(ex.lines):
        raise OrdexaError(ErrorCode.NOT_FOUND, f"line {line_no} not found")
    old = [x for x in req.lines if x["line_no"] != line_no]
    del ex.lines[line_no - 1]
    keep = {}
    for new_no, x in enumerate(old, start=1):
        if x.get("resolution") in ("operator", "customer"):
            keep[new_no] = x
    req.extraction = ex.model_dump()
    evaluate(session, req, ex, keep=keep)
    return req


def link_customer(session: Session, actor: Principal, request_id: uuid.UUID, customer_id: uuid.UUID) -> AiOrderRequest:
    actor.require("sales_order.create")
    req = _get(session, actor, request_id)
    _open(req)
    c = session.get(Customer, customer_id)
    if not c or c.tenant_id != actor.tenant_id:
        raise OrdexaError(ErrorCode.NOT_FOUND, "customer not found")
    req.customer_id = c.id
    keep = {x["line_no"]: x for x in req.lines if x.get("resolution") in ("operator", "customer")}
    evaluate(session, req, Extraction.model_validate(req.extraction), keep=keep)
    return req


def add_to_bill_target(req: AiOrderRequest) -> dict | None:
    """The bill made earlier today that the shop asked to add these items to (set when the message arrived)."""
    return next((t for t in reversed(req.trace or []) if t.get("step") == "add_to_bill"), None)


def approve(
    session: Session, actor: Principal, request_id: uuid.UUID, *, acknowledge_warnings: bool = False, new_bill: bool = False
) -> tuple[AiOrderRequest, sales.PostResult]:
    """Human approval: the draft becomes a posted invoice (W-series), batches picked earliest-expiry first."""
    actor.require("sales_order.approve")
    actor.require("invoice.create")
    req = session.scalar(select(AiOrderRequest).where(AiOrderRequest.id == request_id).with_for_update())
    if not req or req.tenant_id != actor.tenant_id:
        raise OrdexaError(ErrorCode.NOT_FOUND, "order request not found")
    _open(req)
    if req.status != "draft_ready":
        raise OrdexaError(ErrorCode.CONFLICT, "resolve the highlighted lines before approving")
    soft = [w for w in req.warnings if w["code"] in ("credit_review", "customer_inactive")]
    soft += [{"code": "insufficient_stock"} for ln in req.lines if "insufficient_stock" in ln["issues"]]
    if soft and not acknowledge_warnings:
        raise OrdexaError(
            ErrorCode.CONFLICT, "order has warnings; confirm to approve anyway", {"warnings": [w["code"] for w in soft]}
        )
    wh = _main_warehouse(session, req.tenant_id)
    inv_lines: list[sales.LineIn] = []
    n = 0
    for ln in req.lines:
        product = session.get(Product, uuid.UUID(ln["product_id"]))
        need = qty(Decimal(str(ln["quantity"])))
        batches = _fefo_batches(session, req.tenant_id, wh.id, product.id) if product.track_batches else []
        allocations: list[tuple[Batch | None, Decimal]] = []
        if not product.track_batches:
            allocations = [(None, need)]
        elif not batches:
            raise OrdexaError(ErrorCode.CONFLICT, f"{product.name} has no unexpired batch to sell")
        else:
            for b, on_hand in batches:
                if need <= 0:
                    break
                take = min(need, max(on_hand, Decimal(0)))
                if take > 0:
                    allocations.append((b, take))
                    need -= take
            if need > 0:  # shortfall stays on the latest batch and is flagged as oversold by post_invoice
                if allocations and allocations[-1][0] is batches[-1][0]:
                    allocations[-1] = (allocations[-1][0], allocations[-1][1] + need)
                else:
                    allocations.append((batches[-1][0], need))
        for b, q in allocations:
            n += 1
            price = b.sale_price if b is not None and b.sale_price is not None else product.sale_price
            inv_lines.append(
                sales.LineIn(line_no=n, product_id=product.id, batch_id=b.id if b else None, quantity=q, unit_price=price)
            )
    target = None if new_bill else add_to_bill_target(req)
    bill = session.get(Invoice, uuid.UUID(target["invoice_id"])) if target else None
    if bill is not None and bill.customer_id == req.customer_id and sales.amend_blocker(session, bill) is None:
        return _approve_into_bill(session, actor, req, bill, inv_lines)
    if target:  # the bill can no longer take items (next day, returned...): a separate bill, and say why
        why = sales.amend_blocker(session, bill) if bill is not None else "bill not found"
        req.trace = (req.trace or []) + [{"step": "add_to_bill_skipped", "reason": why or "staff chose a new bill"}]
    inv_id = uuid.uuid4()
    result = sales.post_invoice(
        session,
        actor,
        None,
        sales.InvoiceIn(
            id=inv_id,
            number=next_number(session, req.tenant_id, "W"),
            customer_id=req.customer_id,
            issued_at=utcnow(),
            lines=inv_lines,
        ),
        source="whatsapp_ai" if req.source == "whatsapp" else "ai_manual",
        warehouse_id=wh.id,
    )
    if result.status != "accepted":
        raise OrdexaError(ErrorCode.CONFLICT, f"invoice could not be posted: {result.reason}")
    req.invoice_id = inv_id
    req.status = "approved"
    req.reviewed_by = actor.user_id
    req.reviewed_at = utcnow()
    req.trace = (req.trace or []) + [
        {"step": "approved", "by": actor.username, "invoice": result.number, "flags": [f["code"] for f in result.flags]}
    ]
    record(
        session,
        tenant_id=req.tenant_id,
        actor_user_id=actor.user_id,
        action="ai_order.approved",
        entity_type="ai_order",
        entity_id=req.id,
        data={"number": req.number, "invoice": result.number, "total": result.total},
    )
    if req.source == "whatsapp" and req.sender:
        enqueue(
            session,
            kind="whatsapp.send",
            queue="outbox",
            tenant_id=req.tenant_id,
            idempotency_key=f"wa-approved:{req.id}",
            payload={
                "to": req.sender,
                "ref": result.number,
                "body": f"Order {req.number} confirm ho gaya. Bill {result.number}: Rs {result.total}. "
                f"Bill attached hai. Shukriya! / Your order is confirmed, bill attached.",
                "document": {"kind": "invoice", "invoice_id": str(inv_id), "filename": f"Bill-{result.number}.pdf"},
            },
        )
    return req, result


def _approve_into_bill(
    session: Session, actor: Principal, req: AiOrderRequest, bill: Invoice, lines: list[sales.LineIn]
) -> tuple[AiOrderRequest, sales.PostResult]:
    """Approve by adding the items to the shop's bill from earlier today (same number, updated total and PDF)."""
    result = sales.add_lines(session, actor, bill, lines, note=f"added from order {req.number}")
    if result.status != "accepted":
        raise OrdexaError(ErrorCode.CONFLICT, f"could not add to bill {bill.number}: {result.reason}")
    added = sum((ln.line_total for ln in bill.lines if ln.line_no in _added_line_nos(bill)), Decimal(0))
    req.invoice_id = bill.id
    req.status = "approved"
    req.reviewed_by = actor.user_id
    req.reviewed_at = utcnow()
    req.trace = (req.trace or []) + [
        {"step": "approved", "by": actor.username, "invoice": bill.number, "added_to_bill": True, "added": str(added)}
    ]
    record(
        session,
        tenant_id=req.tenant_id,
        actor_user_id=actor.user_id,
        action="ai_order.approved",
        entity_type="ai_order",
        entity_id=req.id,
        data={"number": req.number, "invoice": bill.number, "added_to_bill": True, "total": result.total},
    )
    if req.source == "whatsapp" and req.sender:
        enqueue(
            session,
            kind="whatsapp.send",
            queue="outbox",
            tenant_id=req.tenant_id,
            idempotency_key=f"wa-approved:{req.id}",
            payload={
                "to": req.sender,
                "ref": bill.number,
                "body": f"Order {req.number} ke items aap ke bill {bill.number} mein add ho gaye. "
                f"Updated bill {bill.number}: Rs {bill.total}. Updated bill attached hai. Shukriya!",
                "document": {"kind": "invoice", "invoice_id": str(bill.id), "filename": f"Bill-{bill.number}-updated.pdf"},
            },
        )
    return req, result


def _added_line_nos(bill: Invoice) -> set[int]:
    last = next((f for f in reversed(bill.flags or []) if f.get("code") == "items_added"), None)
    return set(last["lines"]) if last else set()


def reject(session: Session, actor: Principal, request_id: uuid.UUID, reason: str) -> AiOrderRequest:
    actor.require("sales_order.create")
    req = _get(session, actor, request_id)
    if req.status in ("approved", "rejected"):
        raise OrdexaError(ErrorCode.CONFLICT, f"order is already {req.status}")
    if len(reason.strip()) < 3:
        raise OrdexaError(ErrorCode.VALIDATION_ERROR, "give a reason")
    req.status = "rejected"
    req.review_note = reason.strip()[:300]
    req.reviewed_by = actor.user_id
    req.reviewed_at = utcnow()
    if req.source == "whatsapp" and req.sender:
        _queue_reply(
            session,
            req,
            f"Maazrat, aap ka order {req.number} is waqt process nahi ho saka. Wajah: {req.review_note}. "
            f"Mazeed maloomat ke liye hum se raabta karein.",
        )
    record(
        session,
        tenant_id=req.tenant_id,
        actor_user_id=actor.user_id,
        action="ai_order.rejected",
        entity_type="ai_order",
        entity_id=req.id,
        data={"reason": req.review_note},
    )
    return req


def delete_requests(session: Session, actor: Principal, ids: list[uuid.UUID]) -> int:
    """Hide junk requests from every list (soft delete, audited). Billed orders cannot be deleted."""
    actor.require("sales_order.approve")
    n = 0
    for rid in ids:
        req = session.get(AiOrderRequest, rid)
        if req is None or req.tenant_id != actor.tenant_id or req.status in ("approved", "deleted"):
            continue
        req.trace = (req.trace or []) + [{"step": "deleted", "by": actor.username, "was": req.status, "at": utcnow().isoformat()}]
        req.status = "deleted"
        req.reviewed_by = actor.user_id
        req.reviewed_at = utcnow()
        record(session, tenant_id=req.tenant_id, actor_user_id=actor.user_id, action="ai_order.deleted",
               entity_type="ai_order", entity_id=req.id, data={"number": req.number})  # fmt: skip
        n += 1
    return n


def mark_handled(session: Session, actor: Principal, request_id: uuid.UUID) -> AiOrderRequest:
    actor.require("sales_order.create")
    req = _get(session, actor, request_id)
    if req.status != "message":
        raise OrdexaError(ErrorCode.CONFLICT, "only shop messages can be marked as handled")
    req.status = "closed"
    req.review_note = f"Handled by {actor.username}"
    req.reviewed_by = actor.user_id
    req.reviewed_at = utcnow()
    record(session, tenant_id=req.tenant_id, actor_user_id=actor.user_id, action="ai_order.message_handled",
           entity_type="ai_order", entity_id=req.id, data={"number": req.number})  # fmt: skip
    return req


def reprocess(session: Session, actor: Principal, request_id: uuid.UUID) -> AiOrderRequest:
    actor.require("sales_order.create")
    req = _get(session, actor, request_id)
    if req.status in ("approved", "rejected"):
        raise OrdexaError(ErrorCode.CONFLICT, f"order is already {req.status}")
    req.status = "received"
    return process_request(session, req.id)
