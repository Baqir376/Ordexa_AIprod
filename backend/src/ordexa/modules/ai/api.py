from __future__ import annotations

import uuid
from decimal import Decimal

from fastapi import APIRouter, Depends, Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ordexa.modules.ai import service
from ordexa.modules.ai.models import AiAttachment, AiOrderRequest, ProductAlias
from ordexa.modules.catalogue.models import Product
from ordexa.modules.customers.models import Customer
from ordexa.modules.identity.service import Principal, current_principal, requires
from ordexa.modules.sales.models import Invoice
from ordexa.shared.db import get_session

router = APIRouter(prefix="/api/v1/ai", tags=["ai"])


class SubmitIn(BaseModel):
    id: uuid.UUID  # client id: resubmitting the same id never creates a second request
    customer_id: uuid.UUID
    text: str = Field(min_length=1, max_length=4000)


class ResolveIn(BaseModel):
    line_no: int = Field(ge=1)
    product_id: uuid.UUID | None = None
    quantity: Decimal | None = Field(default=None, gt=0, max_digits=14, decimal_places=3)
    remember: bool = False


class AddLineIn(BaseModel):
    product_id: uuid.UUID
    quantity: Decimal = Field(gt=0, max_digits=14, decimal_places=3)


class ApproveIn(BaseModel):
    acknowledge_warnings: bool = False


class RejectIn(BaseModel):
    reason: str = Field(min_length=3, max_length=300)


class LinkCustomerIn(BaseModel):
    customer_id: uuid.UUID


class AliasIn(BaseModel):
    phrase: str = Field(min_length=1, max_length=200)
    product_id: uuid.UUID
    customer_id: uuid.UUID | None = None
    reason: str | None = Field(default=None, max_length=300)


SHOP_STEPS = {
    "customer_answer": "answered",
    "shop_changed_order": "changed",
    "cancelled_by_shop": "cancelled",
    "shop_message_for_staff": "message",
    "shop_thanks": "thanks",
}


def shop_updates(r: AiOrderRequest) -> list[dict]:
    """What the shop said or changed in the chat after sending the order (newest last), for the order desk."""
    out = []
    for t in r.trace or []:
        kind = SHOP_STEPS.get(t.get("step"))
        if kind:
            out.append(
                {
                    "kind": kind,
                    "at": t.get("at"),
                    "message": t.get("message") or t.get("answer"),
                    "changes": t.get("changes") or [],
                }
            )
    return out


def out(session: Session, r: AiOrderRequest, full: bool = True) -> dict:
    c = session.get(Customer, r.customer_id) if r.customer_id else None
    inv = session.get(Invoice, r.invoice_id) if r.invoice_id else None
    d = {
        "id": str(r.id),
        "number": r.number,
        "source": r.source,
        "sender": r.sender,
        "status": r.status,
        "customer": {"id": str(c.id), "code": c.code, "name": c.name} if c else None,
        "raw_text": r.raw_text,
        "kind": r.kind,
        "transcript": session.get(AiAttachment, r.attachment_id).transcript if r.attachment_id and r.kind == "audio" else None,
        "has_attachment": r.attachment_id is not None,
        "received_at": r.received_at.isoformat(),
        "processed_at": r.processed_at.isoformat() if r.processed_at else None,
        "line_count": len(r.lines or []),
        "warnings": r.warnings or [],
        "invoice": {"id": str(inv.id), "number": inv.number, "total": str(inv.total)} if inv else None,
        "review_note": r.review_note,
        "shop_updates": shop_updates(r),
    }
    if full:
        d.update(
            {
                "lines": r.lines or [],
                "questions": r.questions or [],
                "trace": r.trace or [],
                "extractor": r.extractor,
                "extraction": r.extraction,
                "error": r.error,
            }
        )
    return d


@router.get("/orders")
def list_orders(
    status: str | None = None, p: Principal = Depends(requires("sales_order.view")), session: Session = Depends(get_session)
):
    q = select(AiOrderRequest).where(AiOrderRequest.tenant_id == p.tenant_id)
    if status == "open":
        q = q.where(AiOrderRequest.status.in_(("received", "processing", "needs_review", "draft_ready", "failed", "message")))
    elif status:
        q = q.where(AiOrderRequest.status == status)
    q = q.order_by(AiOrderRequest.received_at.desc()).limit(200)
    return [out(session, r, full=False) for r in session.scalars(q)]


@router.post("/orders/{request_id}/close")
def close_message(
    request_id: uuid.UUID, p: Principal = Depends(requires("sales_order.create")), session: Session = Depends(get_session)
):
    """Staff dealt with a shop's message (complaint / question): take it off the open list."""
    req = service.mark_handled(session, p, request_id)
    session.commit()
    return out(session, req)


@router.get("/orders/{request_id}")
def get_order(
    request_id: uuid.UUID, p: Principal = Depends(requires("sales_order.view")), session: Session = Depends(get_session)
):
    return out(session, service._get(session, p, request_id))


@router.post("/orders", status_code=201)
def submit(body: SubmitIn, p: Principal = Depends(requires("sales_order.create")), session: Session = Depends(get_session)):
    """Typed or pasted order (phone call, SMS, WhatsApp forwarded by staff)."""
    req, created = service.submit(
        session,
        tenant_id=p.tenant_id,
        text=body.text,
        source="manual",
        source_message_id=str(body.id),
        customer_id=body.customer_id,
    )
    session.commit()
    if created:
        service.run_inline_if_configured(session, req)
        session.commit()
    return {**out(session, req), "duplicate": not created}


@router.post("/orders/{request_id}/resolve-line")
def resolve_line(
    request_id: uuid.UUID, body: ResolveIn, p: Principal = Depends(current_principal), session: Session = Depends(get_session)
):
    req = service.resolve_line(
        session, p, request_id, line_no=body.line_no, product_id=body.product_id, quantity=body.quantity, remember=body.remember
    )
    session.commit()
    return out(session, req)


@router.post("/orders/{request_id}/lines")
def add_line(
    request_id: uuid.UUID, body: AddLineIn, p: Principal = Depends(current_principal), session: Session = Depends(get_session)
):
    req = service.add_line(session, p, request_id, product_id=body.product_id, quantity=body.quantity)
    session.commit()
    return out(session, req)


@router.post("/orders/{request_id}/lines/{line_no}/remove")
def remove_line(
    request_id: uuid.UUID, line_no: int, p: Principal = Depends(current_principal), session: Session = Depends(get_session)
):
    req = service.remove_line(session, p, request_id, line_no)
    session.commit()
    return out(session, req)


@router.post("/orders/{request_id}/customer")
def link_customer(
    request_id: uuid.UUID,
    body: LinkCustomerIn,
    p: Principal = Depends(current_principal),
    session: Session = Depends(get_session),
):
    req = service.link_customer(session, p, request_id, body.customer_id)
    session.commit()
    return out(session, req)


@router.post("/orders/{request_id}/approve")
def approve(
    request_id: uuid.UUID, body: ApproveIn, p: Principal = Depends(current_principal), session: Session = Depends(get_session)
):
    req, inv = service.approve(session, p, request_id, acknowledge_warnings=body.acknowledge_warnings)
    session.commit()
    return {**out(session, req), "invoice_flags": inv.flags}


@router.post("/orders/{request_id}/reject")
def reject(
    request_id: uuid.UUID, body: RejectIn, p: Principal = Depends(current_principal), session: Session = Depends(get_session)
):
    req = service.reject(session, p, request_id, body.reason)
    session.commit()
    return out(session, req)


@router.post("/orders/{request_id}/reprocess")
def reprocess(request_id: uuid.UUID, p: Principal = Depends(current_principal), session: Session = Depends(get_session)):
    req = service.reprocess(session, p, request_id)
    session.commit()
    return out(session, req)


@router.get("/aliases")
def list_aliases(
    customer_id: uuid.UUID | None = None,
    include_history: bool = False,
    p: Principal = Depends(requires("sales_order.view")),
    session: Session = Depends(get_session),
):
    q = select(ProductAlias).where(ProductAlias.tenant_id == p.tenant_id)
    if customer_id:
        q = q.where(ProductAlias.customer_id == customer_id)
    if not include_history:
        q = q.where(ProductAlias.status == "current")
    rows = session.scalars(q.order_by(ProductAlias.phrase, ProductAlias.version.desc()).limit(1000))
    names = dict(session.execute(select(Product.id, Product.name).where(Product.tenant_id == p.tenant_id)).all())
    custs = dict(session.execute(select(Customer.id, Customer.name).where(Customer.tenant_id == p.tenant_id)).all())
    return [
        {
            "id": str(a.id),
            "phrase": a.phrase,
            "product_id": str(a.product_id),
            "product_name": names.get(a.product_id),
            "customer_id": str(a.customer_id) if a.customer_id else None,
            "customer_name": custs.get(a.customer_id) if a.customer_id else "All customers",
            "version": a.version,
            "status": a.status,
            "reason": a.reason,
            "created_at": a.created_at.isoformat(),
        }
        for a in rows
    ]


@router.post("/aliases", status_code=201)
def create_alias(body: AliasIn, p: Principal = Depends(current_principal), session: Session = Depends(get_session)):
    a = service.save_alias(
        session, p, phrase=body.phrase, product_id=body.product_id, customer_id=body.customer_id, reason=body.reason
    )
    session.commit()
    return {"id": str(a.id), "phrase": a.phrase, "version": a.version}


@router.post("/aliases/{alias_id}/revoke")
def revoke_alias(alias_id: uuid.UUID, p: Principal = Depends(current_principal), session: Session = Depends(get_session)):
    service.revoke_alias(session, p, alias_id)
    session.commit()
    return {"ok": True}


@router.get("/orders/{request_id}/attachment")
def attachment(
    request_id: uuid.UUID, p: Principal = Depends(requires("sales_order.view")), session: Session = Depends(get_session)
):
    """The original photo or voice note of an order (tenant-scoped)."""
    req = service._get(session, p, request_id)
    att = session.get(AiAttachment, req.attachment_id) if req.attachment_id else None
    if att is None or att.tenant_id != p.tenant_id:
        from ordexa.shared.errors import ErrorCode, OrdexaError

        raise OrdexaError(ErrorCode.NOT_FOUND, "this order has no photo or voice note")
    return Response(content=att.data, media_type=att.media_type, headers={"Cache-Control": "private, max-age=3600"})
