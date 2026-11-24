from __future__ import annotations

import base64
import secrets
import uuid
from datetime import timedelta

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ordexa.modules.ai import service as ai
from ordexa.modules.ai.models import AiOrderRequest
from ordexa.modules.audit.models import record
from ordexa.modules.channels import service
from ordexa.modules.channels.models import WhatsappContact, WhatsappMessage, WhatsappNumber
from ordexa.modules.channels.service import utcnow
from ordexa.modules.customers.models import Customer
from ordexa.modules.identity.service import Principal, current_principal, requires
from ordexa.modules.jobs.service import run_one
from ordexa.modules.tenants.models import Tenant
from ordexa.shared.crypto import encrypt
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
    rows = list(
        session.scalars(
            select(WhatsappMessage)
            .where(WhatsappMessage.tenant_id == p.tenant_id, WhatsappMessage.phone == to)
            .order_by(WhatsappMessage.created_at, WhatsappMessage.direction)
            .limit(500)
        )
    )
    ids = [m.ai_order_id for m in rows if m.ai_order_id]
    orders = {o.id: o for o in session.scalars(select(AiOrderRequest).where(AiOrderRequest.id.in_(ids)))} if ids else {}
    return [
        {
            "media": _media(m, orders.get(m.ai_order_id)),
            "order": _order_chip(orders.get(m.ai_order_id)) if m.direction == "in" else None,
            "direction": m.direction,
            "body": m.body,
            "status": m.status,
            "error": m.error,
            "created_at": m.created_at.isoformat(),
            "ai_order_id": str(m.ai_order_id) if m.ai_order_id else None,
            "sent_by": m.sent_by if m.direction == "out" else None,
        }
        for m in rows
    ]


def _media(m: WhatsappMessage, order: AiOrderRequest | None) -> dict | None:
    """What the chat can show or play: a bill PDF we sent, or the photo / voice note the shop sent."""
    if m.media_ref and m.media_ref.startswith("invoice:"):
        inv_id = m.media_ref.split(":", 1)[1]
        name = m.body[6 : m.body.index("]")] if m.body.startswith("[PDF: ") and "]" in m.body else "Bill.pdf"
        return {"kind": "pdf", "path": f"/api/v1/invoices/{inv_id}/pdf", "name": name}
    if m.direction == "in" and order is not None and order.attachment_id and order.kind in ("image", "audio"):
        return {"kind": order.kind, "path": f"/api/v1/ai/orders/{order.id}/attachment", "name": order.number}
    return None


def _order_chip(order: AiOrderRequest | None) -> dict | None:
    if order is None or order.status in ("closed", "deleted"):
        return None
    return {"id": str(order.id), "number": order.number, "status": order.status}


class ReadIn(BaseModel):
    phone: str = Field(min_length=6, max_length=20)


@router.post("/conversations/read")
def mark_read(body: ReadIn, p: Principal = Depends(requires("customer.view")), session: Session = Depends(get_session)):
    """Staff opened this chat: its unread count goes back to zero."""
    to = service.normalize_phone(body.phone) or body.phone
    c = session.scalar(select(WhatsappContact).where(WhatsappContact.tenant_id == p.tenant_id, WhatsappContact.phone == to))
    if c is not None:
        c.last_read_at = utcnow()
        session.commit()
    return {"ok": True}


@router.get("/conversations")
def conversations(
    q: str | None = None, p: Principal = Depends(requires("customer.view")), session: Session = Depends(get_session)
):
    """Every WhatsApp chat, newest first: who it is, the last message and how many messages came in today."""
    newest = (
        select(WhatsappMessage.phone, func.max(WhatsappMessage.created_at).label("at"))
        .where(WhatsappMessage.tenant_id == p.tenant_id)
        .group_by(WhatsappMessage.phone)
        .subquery()
    )
    last = (
        select(WhatsappMessage)
        .join(newest, (WhatsappMessage.phone == newest.c.phone) & (WhatsappMessage.created_at == newest.c.at))
        .where(WhatsappMessage.tenant_id == p.tenant_id)
    )
    rows = list({m.phone: m for m in session.scalars(last)}.values())  # one row per chat
    since = utcnow() - timedelta(hours=24)
    counts = dict(
        session.execute(
            select(WhatsappMessage.phone, func.count())
            .where(
                WhatsappMessage.tenant_id == p.tenant_id, WhatsappMessage.direction == "in", WhatsappMessage.created_at >= since
            )
            .group_by(WhatsappMessage.phone)
        ).all()
    )
    contacts = {c.phone: c for c in session.scalars(select(WhatsappContact).where(WhatsappContact.tenant_id == p.tenant_id))}
    # Unread = shop messages since staff last opened the chat (never opened: everything from the last 24 hours).
    unread: dict[str, int] = {}
    for phone, at in session.execute(
        select(WhatsappMessage.phone, WhatsappMessage.created_at).where(
            WhatsappMessage.tenant_id == p.tenant_id, WhatsappMessage.direction == "in", WhatsappMessage.created_at >= since
        )
    ):
        c = contacts.get(phone)
        if c is None or c.last_read_at is None or at > c.last_read_at:
            unread[phone] = unread.get(phone, 0) + 1
    out = []
    for m in rows:
        c = contacts.get(m.phone)
        cust = session.get(Customer, c.customer_id) if c and c.customer_id else None
        name = cust.name if cust else (c.registration_details.split(",")[0] if c and c.registration_details else None)
        item = {
            "phone": m.phone,
            "name": name,
            "customer_code": cust.code if cust else None,
            "state": c.state if c else None,
            "last_body": m.body[:160],
            "last_direction": m.direction,
            "last_at": m.created_at.isoformat(),
            "in_last_24h": counts.get(m.phone, 0),
            "unread": unread.get(m.phone, 0),
        }
        if q and q.lower() not in f"{m.phone} {name or ''} {item['customer_code'] or ''}".lower():
            continue
        out.append(item)
    return sorted(out, key=lambda x: x["last_at"], reverse=True)[:300]


class ReplyIn(BaseModel):
    phone: str = Field(min_length=6, max_length=20)
    text: str = Field(min_length=1, max_length=2000)


@router.post("/reply")
def staff_reply(body: ReplyIn, p: Principal = Depends(requires("sales_order.create")), session: Session = Depends(get_session)):
    """Staff answer a shop from the app (complaints, delivery questions). WhatsApp only allows free text within
    24 hours of the shop's last message; after that the send is recorded as failed with the reason."""
    to = service.normalize_phone(body.phone)
    if to is None:
        raise OrdexaError(ErrorCode.VALIDATION_ERROR, "invalid phone number")
    service.queue_text(session, p.tenant_id, to, body.text.strip(), key=f"staff:{uuid.uuid4()}", by="staff")
    record(session, tenant_id=p.tenant_id, actor_user_id=p.user_id, action="whatsapp.staff_reply",
           entity_type="whatsapp_contact", entity_id=None, data={"phone": to})  # fmt: skip
    session.commit()
    for _ in range(5):  # deliver now (real Cloud API when connected, simulated otherwise)
        if not run_one(session, "outbox", {"whatsapp.send": service.send_job}):
            break
    session.commit()
    return {"queued": True}


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


# ------------------------------------------------------------------ Settings -> WhatsApp connection


class ConnectionIn(BaseModel):
    phone_number_id: str = Field(min_length=5, max_length=40, pattern=r"^\d+$")
    display_phone: str = Field(default="", max_length=30)
    access_token: str | None = Field(default=None, max_length=2000)  # omitted = keep the saved one
    app_secret: str | None = Field(default=None, max_length=200)  # omitted = keep the saved one
    verify_token: str | None = Field(default=None, max_length=80)
    public_url: str | None = Field(default=None, max_length=300)
    waba_id: str | None = Field(default=None, max_length=40, pattern=r"^\d*$")


class SendTestIn(BaseModel):
    to: str = Field(min_length=6, max_length=20)


WEBHOOK_PATH = "/webhooks/whatsapp"


def _connection(session: Session, tenant_id: uuid.UUID) -> WhatsappNumber | None:
    return session.scalar(
        select(WhatsappNumber).where(WhatsappNumber.tenant_id == tenant_id).order_by(WhatsappNumber.created_at.desc())
    )


def connection_out(n: WhatsappNumber | None) -> dict:
    if n is None:
        return {"connected": False, "webhook_path": WEBHOOK_PATH}
    base = (n.public_url or "").rstrip("/")
    return {
        "connected": bool(n.access_token_enc),
        "phone_number_id": n.phone_number_id,
        "display_phone": n.display_phone,
        "has_access_token": bool(n.access_token_enc),  # secrets are never returned
        "has_app_secret": bool(n.app_secret_enc),
        "verify_token": n.verify_token,
        "public_url": n.public_url,
        "waba_id": n.waba_id,
        "webhook_path": WEBHOOK_PATH,
        "webhook_url": f"{base}{WEBHOOK_PATH}" if base else None,
        "last_inbound_at": n.last_inbound_at.isoformat() if n.last_inbound_at else None,
        "last_error": n.last_error,
    }


@router.get("/connection")
def get_connection(p: Principal = Depends(requires("integration.configure")), session: Session = Depends(get_session)):
    return connection_out(_connection(session, p.tenant_id))


@router.put("/connection")
def save_connection(
    body: ConnectionIn, p: Principal = Depends(requires("integration.configure")), session: Session = Depends(get_session)
):
    """Save the Meta details for this business. Token and app secret are stored encrypted and never shown again."""
    clash = session.scalar(select(WhatsappNumber).where(WhatsappNumber.phone_number_id == body.phone_number_id))
    if clash is not None and clash.tenant_id != p.tenant_id:
        raise OrdexaError(ErrorCode.CONFLICT, "this WhatsApp number is connected to another business")
    n = clash or _connection(session, p.tenant_id)
    if n is None:
        n = WhatsappNumber(tenant_id=p.tenant_id, phone_number_id=body.phone_number_id, display_phone=body.display_phone)
        session.add(n)
    n.phone_number_id = body.phone_number_id
    n.display_phone = body.display_phone or n.display_phone or ""
    if body.access_token:
        n.access_token_enc = encrypt(body.access_token.strip())
    if body.app_secret:
        n.app_secret_enc = encrypt(body.app_secret.strip())
    n.verify_token = (body.verify_token or "").strip() or n.verify_token or secrets.token_urlsafe(24)
    if body.waba_id is not None:
        n.waba_id = body.waba_id.strip() or None
    if body.public_url is not None:
        url = body.public_url.strip().rstrip("/")
        if url and not url.startswith("https://"):
            raise OrdexaError(ErrorCode.VALIDATION_ERROR, "the public address must start with https:// (Meta requires HTTPS)")
        n.public_url = url or None
    n.last_error = None
    record(
        session,
        tenant_id=p.tenant_id,
        actor_user_id=p.user_id,
        action="whatsapp.connection_saved",
        entity_type="whatsapp_number",
        entity_id=n.phone_number_id,
        data={"token_changed": bool(body.access_token), "secret_changed": bool(body.app_secret)},
    )
    session.commit()
    return connection_out(n)


@router.post("/connection/test")
def test_connection(p: Principal = Depends(requires("integration.configure")), session: Session = Depends(get_session)):
    """Ask Meta about the number with the saved token: proves the phone number ID and token are right."""
    creds = service.credentials(session, p.tenant_id)
    if creds is None:
        raise OrdexaError(ErrorCode.VALIDATION_ERROR, "save the phone number ID and access token first")
    try:
        info = service.graph_request(creds, "GET", f"{creds.phone_number_id}?fields=display_phone_number,verified_name")
    except Exception as exc:  # noqa: BLE001 - shown to the admin as the reason
        if creds.number is not None:
            creds.number.last_error = str(exc)[:500]
            session.commit()
        return {"ok": False, "message": str(exc)}
    n = creds.number
    if n is not None:
        n.display_phone = info.get("display_phone_number") or n.display_phone
        n.last_error = None
        session.commit()
    sub_note = ""
    if n is not None and n.waba_id:
        # Known Meta quirk: the webhook verifies fine but no messages arrive until the app is subscribed to the WABA.
        try:
            # Always subscribe *this* app (idempotent). Another app being subscribed (e.g. Meta's dashboard test-webhook
            # app) is not enough: messages then go to that app, not to Ordexa.
            service.graph_request(creds, "POST", f"{n.waba_id}/subscribed_apps", {})
            subs = service.graph_request(creds, "GET", f"{n.waba_id}/subscribed_apps").get("data", [])
            names = ", ".join((a.get("whatsapp_business_api_data") or {}).get("name", "?") for a in subs)
            sub_note = f" Your app is subscribed to receive messages (subscribed apps: {names})."
        except Exception as exc:  # noqa: BLE001
            sub_note = f" Could not check the app subscription: {exc}"
    else:
        sub_note = " (Add the WhatsApp Business Account ID to also check that messages will be delivered.)"
    return {
        "ok": True,
        "message": f"Connected to {info.get('display_phone_number')} ({info.get('verified_name')}).{sub_note}",
        "display_phone": info.get("display_phone_number"),
        "verified_name": info.get("verified_name"),
    }


@router.post("/connection/send-test")
def send_test(
    body: SendTestIn, p: Principal = Depends(requires("integration.configure")), session: Session = Depends(get_session)
):
    """Send Meta's built-in 'hello_world' template to a phone (on a test number it must be an allowed recipient)."""
    creds = service.credentials(session, p.tenant_id)
    if creds is None:
        raise OrdexaError(ErrorCode.VALIDATION_ERROR, "save the phone number ID and access token first")
    to = service.normalize_phone(body.to)
    if to is None:
        raise OrdexaError(ErrorCode.VALIDATION_ERROR, "enter the phone number with country code, e.g. +923001234567")
    payload = {
        "messaging_product": "whatsapp",
        "to": to.lstrip("+"),
        "type": "template",
        "template": {"name": "hello_world", "language": {"code": "en_US"}},
    }
    try:
        res = service.graph_request(creds, "POST", f"{creds.phone_number_id}/messages", payload)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "message": str(exc)}
    return {
        "ok": True,
        "message": f"Test message sent to {to}. Check WhatsApp on that phone.",
        "wamid": (res.get("messages") or [{}])[0].get("id"),
    }


@router.delete("/connection")
def disconnect(p: Principal = Depends(requires("integration.configure")), session: Session = Depends(get_session)):
    n = _connection(session, p.tenant_id)
    if n is not None:
        n.access_token_enc = None
        n.app_secret_enc = None
        record(
            session,
            tenant_id=p.tenant_id,
            actor_user_id=p.user_id,
            action="whatsapp.disconnected",
            entity_type="whatsapp_number",
            entity_id=n.phone_number_id,
        )
        session.commit()
    return connection_out(n)
