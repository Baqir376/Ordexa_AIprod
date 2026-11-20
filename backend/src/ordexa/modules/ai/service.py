"""AI order desk: intake -> extraction -> deterministic resolution and checks -> human approval -> invoice.

The model never touches the database or makes business decisions. It proposes lines; this module resolves them
against the catalogue and approved memory, checks units, stock, price and credit with ERP data, and a person with
``sales_order.approve`` turns the draft into an invoice through the same ``post_invoice`` path every bill uses.
"""

from __future__ import annotations

import logging
import re
import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from difflib import SequenceMatcher

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ordexa.config import get_settings
from ordexa.modules.ai.extract import ExtractedLine, Extraction, extract_order, normalise_phrase
from ordexa.modules.ai.models import AiOrderRequest, ProductAlias
from ordexa.modules.audit.models import record
from ordexa.modules.catalogue.models import Batch, Product
from ordexa.modules.customers.models import Customer
from ordexa.modules.finance.service import balance
from ordexa.modules.identity.service import Principal
from ordexa.modules.inventory.models import InventoryTransaction
from ordexa.modules.jobs.service import enqueue
from ordexa.modules.sales import service as sales
from ordexa.modules.tenants.models import Warehouse
from ordexa.shared.errors import ErrorCode, OrdexaError
from ordexa.shared.money import line_total, money, qty
from ordexa.shared.sequences import next_number

log = logging.getLogger("ordexa.ai")

RESOLVE_MIN = 0.75  # top candidate score needed to resolve without a person
RESOLVE_GAP = 0.12  # and its lead over the runner-up
CANDIDATE_MIN = 0.45
BLOCKING = {"ambiguous", "not_found", "missing_quantity", "unit_mismatch", "inactive_product"}


def utcnow() -> datetime:
    return datetime.now(UTC)


# ------------------------------------------------------------------ matching


def _tokens(text: str) -> list[str]:
    return [t for t in re.split(r"[\s\-/]+", normalise_phrase(text)) if t]


def _token_hit(tok: str, name_tokens: list[str]) -> bool:
    for nt in name_tokens:
        if tok == nt or (len(tok) >= 3 and nt.startswith(tok)) or SequenceMatcher(None, tok, nt).ratio() >= 0.8:
            return True
    return False


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
    coverage = sum(1 for t in ptoks if _token_hit(t, ntoks)) / len(ptoks)
    if coverage == 0:
        return 0.0
    return round(0.6 * coverage + 0.4 * SequenceMatcher(None, p, name).ratio(), 3)


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
            (Batch.expiry_date.is_(None)) | (Batch.expiry_date > date.today()),
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
        line = {
            "line_no": i,
            "phrase": ex.phrase,
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
            line.update({k: keep[i][k] for k in ("product_id", "quantity", "resolution", "unit") if k in keep[i]})
        product = None
        if line["product_id"]:
            product = by_id.get(uuid.UUID(str(line["product_id"])))
        if product is None:
            alias = _find_alias(session, req.tenant_id, req.customer_id, phrase)
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
                    ((score_product(phrase, p), p) for p in products if p.is_active), key=lambda x: x[0], reverse=True
                )
                cands = [(s, p) for s, p in scored if s >= CANDIDATE_MIN][:5]
                line["candidates"] = [{"product_id": str(p.id), "name": p.name, "code": p.code, "score": s} for s, p in cands]
                trace.append(
                    {
                        "step": "catalogue_search",
                        "line": i,
                        "phrase": phrase,
                        "top": [{"code": p.code, "score": s} for s, p in cands[:3]],
                    }
                )
                if cands and cands[0][0] >= RESOLVE_MIN and (len(cands) == 1 or cands[0][0] - cands[1][0] >= RESOLVE_GAP):
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
            if line["unit"] and line["unit"] != product.sale_unit and line.get("resolution") != "operator":
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
    if not lines:
        warnings.append({"code": "no_lines", "message": "No order lines could be read from this message."})
    if req.customer_id is None:
        warnings.append({"code": "unknown_customer", "message": "Sender is not linked to a customer."})
    if extraction.contains_instructions:
        warnings.append(
            {
                "code": "contains_instructions",
                "message": "Message asks for something besides an order (discount, approval, credit...). "
                "It was not acted on; review it yourself.",
            }
        )
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
        if "ambiguous" in ln["issues"]:
            opts = " / ".join(f"{k + 1}) {c['name']}" for k, c in enumerate(ln["candidates"][:4]))
            questions.append(f"'{ln['phrase']}': konsa chahiye? Which one: {opts}")
        elif "not_found" in ln["issues"]:
            questions.append(f"'{ln['phrase']}': yeh item humare paas nahi mila. Please write the full product name.")
        elif "missing_quantity" in ln["issues"]:
            questions.append(f"'{ln['phrase']}': kitni quantity chahiye? How many?")
        elif "unit_mismatch" in ln["issues"]:
            questions.append(
                f"'{ln['phrase']}': {ln['quantity']} {ln['unit']} — hum {ln['sale_unit']} mein bechte hain. "
                f"Kitne {ln['sale_unit']}?"
            )

    blocked = any(set(ln["issues"]) & BLOCKING for ln in lines) or not lines or req.customer_id is None
    req.lines = lines
    req.warnings = warnings
    req.questions = questions
    req.trace = (req.trace or []) + trace
    req.status = "needs_review" if blocked or extraction.contains_instructions else "draft_ready"
    req.processed_at = utcnow()


def process_request(session: Session, request_id: uuid.UUID) -> AiOrderRequest:
    req = session.get(AiOrderRequest, request_id)
    if req is None:
        raise LookupError(f"AI request {request_id} not found")
    if req.status not in ("received", "processing", "failed"):
        return req  # already processed (job retried after success)
    req.status = "processing"
    result = extract_order(req.raw_text)
    req.extractor = result.extractor
    req.extraction = result.extraction.model_dump()
    req.trace = result.trace
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
        _notify_customer(session, req)
    return req


def _notify_customer(session: Session, req: AiOrderRequest) -> None:
    """Queue a WhatsApp reply (sent by the outbox worker)."""
    if not req.sender:
        return
    if req.status == "needs_review" and req.questions:
        body = "Shukriya! Aap ka order mil gaya. Kuch cheezein confirm karni hain:\n- " + "\n- ".join(req.questions[:5])
    else:
        body = (
            f"Shukriya! Order {req.number} mil gaya ({len(req.lines)} items). Hamari team confirm karke bill "
            f"bhej degi. / Order received, we will confirm shortly."
        )
    enqueue(
        session,
        kind="whatsapp.send",
        queue="outbox",
        tenant_id=req.tenant_id,
        idempotency_key=f"wa-reply:{req.id}:{req.status}:{len(req.questions)}",
        payload={"to": req.sender, "body": body, "ref": req.number},
    )


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
) -> tuple[AiOrderRequest, bool]:
    """Create a request (idempotent per source message) and queue it for the AI worker."""
    text = (text or "").strip()
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
    keep = {x["line_no"]: x for x in lines if x.get("resolution") == "operator"}
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
    evaluate(session, req, Extraction.model_validate(req.extraction), keep=keep)
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
    keep = {x["line_no"]: x for x in req.lines if x.get("resolution") == "operator"}
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
        if x.get("resolution") == "operator":
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
    keep = {x["line_no"]: x for x in req.lines if x.get("resolution") == "operator"}
    evaluate(session, req, Extraction.model_validate(req.extraction), keep=keep)
    return req


def approve(
    session: Session, actor: Principal, request_id: uuid.UUID, *, acknowledge_warnings: bool = False
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
                f"Shukriya! / Your order is confirmed.",
            },
        )
    return req, result


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


def reprocess(session: Session, actor: Principal, request_id: uuid.UUID) -> AiOrderRequest:
    actor.require("sales_order.create")
    req = _get(session, actor, request_id)
    if req.status in ("approved", "rejected"):
        raise OrdexaError(ErrorCode.CONFLICT, f"order is already {req.status}")
    req.status = "received"
    return process_request(session, req.id)
