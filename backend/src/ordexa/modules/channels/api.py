from __future__ import annotations

import base64
import uuid

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ordexa.modules.ai import service as ai
from ordexa.modules.ai.models import AiOrderRequest
from ordexa.modules.channels import service
from ordexa.modules.channels.models import WhatsappContact, WhatsappMessage, WhatsappNumber
from ordexa.modules.customers.models import Customer
from ordexa.modules.identity.service import Principal, current_principal, requires
from ordexa.modules.jobs.service import run_one
from ordexa.modules.tenants.models import Tenant
from ordexa.shared.db import get_session
from ordexa.shared.errors import ErrorCode, OrdexaError

router = APIRouter(prefix="/api/v1/channels/whatsapp", tags=["whatsapp"])


class NumberIn(BaseModel):
    phone_number_id: str = Field(min_length=3, max_length=40)
    display_phone: str = Field(min_length=3, max_length=30)


class NewCustomerIn(BaseModel):
    code: str = Field(min_length=1, max_length=40)
    name: str = Field(min_length=1, max_length=200)
    address: str | None = Field(default=None, max_length=300)


class ApproveIn(BaseModel):
    customer_id: uuid.UUID | None = None
    new_customer: NewCustomerIn | None = None


class SimulateIn(BaseModel):
    phone: str = Field(min_length=6, max_length=20)
    text: str = Field(default="", max_length=4000)
    media_kind: str | None = Field(default=None, pattern="^(image|audio)$")  # simulate a photo or voice note
    media_base64: str | None = Field(default=None, max_length=14_000_000)
    media_type: str | None = Field(default=None, max_length=100)


def contact_out(session: Session, c: WhatsappContact) -> dict:
    claimed = session.get(Customer, c.claimed_customer_id) if c.claimed_customer_id else None
    linked = session.get(Customer, c.customer_id) if c.customer_id else None
    return {
        "id": str(c.id),
        "phone": c.phone,
        "profile_name": c.profile_name,
        "state": c.state,
        "customer": {"id": str(linked.id), "code": linked.code, "name": linked.name} if linked else None,
        "claimed_customer": {"id": str(claimed.id), "code": claimed.code, "name": claimed.name, "phone_on_file": claimed.phone}
        if claimed
        else None,
        "registration_details": c.registration_details,
        "last_message_at": c.last_message_at.isoformat() if c.last_message_at else None,
        "verified_at": c.verified_at.isoformat() if c.verified_at else None,
    }


@router.get("/numbers")
def list_numbers(p: Principal = Depends(requires("integration.configure")), session: Session = Depends(get_session)):
    rows = session.scalars(select(WhatsappNumber).where(WhatsappNumber.tenant_id == p.tenant_id))
    return [{"id": str(n.id), "phone_number_id": n.phone_number_id, "display_phone": n.display_phone} for n in rows]


@router.post("/numbers", status_code=201)
def add_number(
    body: NumberIn, p: Principal = Depends(requires("integration.configure")), session: Session = Depends(get_session)
):
    if session.scalar(select(WhatsappNumber).where(WhatsappNumber.phone_number_id == body.phone_number_id)):
        raise OrdexaError(ErrorCode.CONFLICT, "this WhatsApp number is already connected")
    n = WhatsappNumber(tenant_id=p.tenant_id, **body.model_dump())
    session.add(n)
    session.commit()
    return {"id": str(n.id)}


@router.get("/contacts")
def list_contacts(
    state: str | None = None, p: Principal = Depends(requires("customer.view")), session: Session = Depends(get_session)
):
    q = select(WhatsappContact).where(WhatsappContact.tenant_id == p.tenant_id)
    if state == "pending":
        q = q.where(WhatsappContact.state.in_(("pending_approval", "awaiting_otp")))
    elif state:
        q = q.where(WhatsappContact.state == state)
    return [contact_out(session, c) for c in session.scalars(q.order_by(WhatsappContact.last_message_at.desc().nulls_last()))]


@router.post("/contacts/{contact_id}/approve")
def approve_contact(
    contact_id: uuid.UUID, body: ApproveIn, p: Principal = Depends(current_principal), session: Session = Depends(get_session)
):
    c = service.approve_contact(
        session,
        p,
        contact_id,
        customer_id=body.customer_id,
        new_customer=body.new_customer.model_dump() if body.new_customer else None,
    )
    session.commit()
    return contact_out(session, c)


@router.post("/contacts/{contact_id}/block")
def block_contact(contact_id: uuid.UUID, p: Principal = Depends(current_principal), session: Session = Depends(get_session)):
    c = service.block_contact(session, p, contact_id)
    session.commit()
    return contact_out(session, c)


@router.post("/contacts/{contact_id}/unlink")
def unlink_contact(contact_id: uuid.UUID, p: Principal = Depends(current_principal), session: Session = Depends(get_session)):
    c = service.unlink_contact(session, p, contact_id)
    session.commit()
    return contact_out(session, c)


@router.get("/messages")
def messages(phone: str, p: Principal = Depends(requires("customer.view")), session: Session = Depends(get_session)):
    to = service.normalize_phone(phone)
    rows = session.scalars(
        select(WhatsappMessage)
        .where(WhatsappMessage.tenant_id == p.tenant_id, WhatsappMessage.phone == to)
        .order_by(WhatsappMessage.created_at, WhatsappMessage.direction)
        .limit(500)
    )
    return [
        {"direction": m.direction, "body": m.body, "status": m.status, "error": m.error, "created_at": m.created_at.isoformat()}
        for m in rows
    ]


@router.post("/simulate")
def simulate(
    body: SimulateIn, p: Principal = Depends(requires("integration.configure")), session: Session = Depends(get_session)
):
    """Test the whole WhatsApp flow without Meta: runs the same bot, AI processing and outbox inline."""
    tenant = session.get(Tenant, p.tenant_id)
    wamid = f"sim.{uuid.uuid4()}"
    media = None
    if body.media_kind:
        try:
            data = base64.b64decode(body.media_base64 or "", validate=True)
        except ValueError as exc:
            raise OrdexaError(ErrorCode.VALIDATION_ERROR, "media is not valid base64") from exc
        if not data:
            raise OrdexaError(ErrorCode.VALIDATION_ERROR, "attach a photo or voice note")
        media = {
            "kind": body.media_kind,
            "data": data,
            "mime": body.media_type or "application/octet-stream",
            "caption": body.text or None,
            "media_id": None,
        }
    elif not body.text.strip():
        raise OrdexaError(ErrorCode.VALIDATION_ERROR, "type a message")
    replies = service.handle_inbound(session, tenant, phone=body.phone, text=body.text or None, wamid=wamid, media=media)
    session.commit()
    req = session.scalar(
        select(AiOrderRequest).where(AiOrderRequest.tenant_id == tenant.id, AiOrderRequest.source_message_id == wamid)
    )
    if req is not None and req.status == "received":
        ai.process_request(session, req.id)
        session.commit()
    for _ in range(20):  # deliver queued replies (simulated unless a WhatsApp token is configured)
        if not run_one(session, "outbox", {"whatsapp.send": service.send_job}):
            break
    session.commit()
    return {"bot_replies": replies, "ai_order": {"id": str(req.id), "number": req.number, "status": req.status} if req else None}
