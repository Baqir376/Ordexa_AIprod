"""WhatsApp channel: sign-up bot, verified ordering, staff approval, and outbound delivery.

The conversation is a fixed state machine, not the LLM. Only a phone verified for a customer (by matching the
number on file, a one-time code sent to the shop's registered number, or a staff approval) can place orders;
its messages then go to the AI order desk. Shop codes alone never grant access.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import re
import secrets
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ordexa.config import get_settings
from ordexa.modules.ai import conversation
from ordexa.modules.ai import service as ai
from ordexa.modules.ai.models import AiAttachment, AiOrderRequest
from ordexa.modules.audit.models import record
from ordexa.modules.channels.models import WhatsappContact, WhatsappMessage, WhatsappNumber
from ordexa.modules.customers.models import Customer
from ordexa.modules.identity.service import Principal
from ordexa.modules.jobs.models import Job
from ordexa.modules.jobs.service import enqueue
from ordexa.modules.sales import service as sales
from ordexa.modules.sales.models import Invoice
from ordexa.modules.tenants.models import Tenant
from ordexa.shared.crypto import decrypt
from ordexa.shared.db import lock_master_data
from ordexa.shared.errors import ErrorCode, OrdexaError

log = logging.getLogger("ordexa.whatsapp")

OTP_TTL = timedelta(minutes=10)
MAX_FAILED = 5
LOCK_FOR = timedelta(hours=1)
REPLY_WINDOW = timedelta(hours=24)
MAX_MESSAGES_PER_HOUR = 30

YES = {"1", "haan", "han", "ji", "jee", "yes", "y", "registered", "register", "hai", "haan ji"}
NO = {"2", "nahi", "nahin", "no", "n", "new", "naya", "new account", "naya account"}
BALANCE = {"balance", "hisaab", "hisab", "khata", "bakaya", "baqaya", "kitna baqi", "kitna baki"}
HELP = {"menu", "help", "madad", "start", "hi", "hello", "salam", "assalam o alaikum", "aoa"}


def utcnow() -> datetime:
    return datetime.now(UTC)


def normalize_phone(raw: str | None) -> str | None:
    digits = re.sub(r"\D", "", raw or "")
    if not 9 <= len(digits) <= 15:
        return None
    if digits.startswith("00"):
        digits = digits[2:]
    elif digits.startswith("0"):
        digits = "92" + digits[1:]  # Pakistani local format 03xx -> +923xx
    return f"+{digits}"


def _hash_otp(contact_id: uuid.UUID, code: str) -> str:
    return hashlib.sha256(f"{contact_id}:{code}:{get_settings().jwt_secret}".encode()).hexdigest()


# ------------------------------------------------------------------ outbound


def queue_text(
    session: Session, tenant_id: uuid.UUID, to: str, body: str, key: str, template: str | None = None, by: str | None = None
) -> None:
    enqueue(
        session,
        kind="whatsapp.send",
        queue="outbox",
        tenant_id=tenant_id,
        idempotency_key=f"wa:{key}"[:200],
        payload={"to": to, "body": body, "template": template, **({"by": by} if by else {})},
    )


@dataclass
class Creds:
    """How to reach the Cloud API for one business: from Settings -> WhatsApp, else the environment."""

    phone_number_id: str
    token: str
    app_secret: str | None
    number: WhatsappNumber | None = None


def credentials(session: Session, tenant_id: uuid.UUID) -> Creds | None:
    n = session.scalar(
        select(WhatsappNumber).where(WhatsappNumber.tenant_id == tenant_id).order_by(WhatsappNumber.created_at.desc())
    )
    if n is not None:
        token = decrypt(n.access_token_enc)
        if token:
            return Creds(n.phone_number_id, token, decrypt(n.app_secret_enc), n)
    st = get_settings()
    if st.whatsapp_token and st.whatsapp_phone_number_id:
        return Creds(st.whatsapp_phone_number_id, st.whatsapp_token, st.whatsapp_app_secret or None, n)
    return None


def send_job(session: Session, job: Job) -> None:
    """Outbox worker handler. Real Cloud API when the business has connected WhatsApp; otherwise 'simulated'."""
    p = job.payload
    contact = session.scalar(
        select(WhatsappContact).where(WhatsappContact.tenant_id == job.tenant_id, WhatsappContact.phone == p["to"])
    )
    msg = WhatsappMessage(
        tenant_id=job.tenant_id,
        contact_id=contact.id if contact else None,
        direction="out",
        phone=p["to"],
        body=p["body"],
        status="simulated",
        sent_by=p.get("by") or "bot",
    )
    creds = credentials(session, job.tenant_id)
    if creds is not None:
        in_window = contact and contact.last_message_at and utcnow() - contact.last_message_at < REPLY_WINDOW
        if not in_window and not p.get("template"):
            msg.status, msg.error = "failed", "outside the 24-hour reply window; an approved template is required"
        else:
            try:
                doc = _document(session, p.get("document"))
                if doc is not None:
                    try:
                        msg.wamid = _cloud_send_document(creds, p["to"], *doc, caption=p["body"])
                        msg.body = f"[PDF: {doc[1]}] {p['body']}"
                    except PermanentSendError as exc:  # document refused: still tell the customer in text
                        log.warning("sending bill PDF failed, sending text instead: %s", exc)
                        msg.wamid = _cloud_send(creds, p["to"], p["body"], None)
                else:
                    msg.wamid = _cloud_send(creds, p["to"], p["body"], p.get("template"))
                msg.status = "sent"
            except PermanentSendError as exc:  # e.g. bad token, number not allowed: record it, don't retry forever
                msg.status, msg.error = "failed", str(exc)[:500]
                if creds.number is not None:
                    creds.number.last_error = str(exc)[:500]
    elif p.get("document"):
        msg.body = f"[PDF: {p['document'].get('filename', 'bill.pdf')}] {p['body']}"
    session.add(msg)


def _document(session: Session, spec: dict | None) -> tuple[bytes, str] | None:
    """Render the attachment named in an outbox job (currently: a bill PDF)."""
    if not spec or spec.get("kind") != "invoice":
        return None
    from ordexa.modules.sales.models import Invoice
    from ordexa.modules.sales.receipt import invoice_pdf

    inv = session.get(Invoice, uuid.UUID(spec["invoice_id"]))
    if inv is None:
        return None
    return invoice_pdf(session, inv), spec.get("filename") or f"Bill-{inv.number}.pdf"


def _upload_media(creds: Creds, data: bytes, filename: str, mime: str) -> str:
    """Upload a file to the Cloud API (multipart) and return its media id."""
    boundary = "ordexa" + uuid.uuid4().hex
    parts = [
        f'--{boundary}\r\nContent-Disposition: form-data; name="messaging_product"\r\n\r\nwhatsapp\r\n'.encode(),
        f'--{boundary}\r\nContent-Disposition: form-data; name="type"\r\n\r\n{mime}\r\n'.encode(),
        f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{filename}"\r\n'
        f"Content-Type: {mime}\r\n\r\n".encode()
        + data
        + b"\r\n",
        f"--{boundary}--\r\n".encode(),
    ]
    req = urllib.request.Request(
        f"{get_settings().whatsapp_api_base}/{creds.phone_number_id}/media",
        data=b"".join(parts),
        method="POST",
        headers={"Authorization": f"Bearer {creds.token}", "Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as r:  # noqa: S310 - fixed https Graph API base
            return json.loads(r.read())["id"]
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")[:400]
        if e.code == 429 or e.code >= 500:
            raise RuntimeError(f"WhatsApp media upload {e.code}: {detail}") from e
        raise PermanentSendError(f"WhatsApp media upload {e.code}: {detail}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"WhatsApp API unreachable: {e.reason}") from e


def _cloud_send_document(creds: Creds, to: str, data: bytes, filename: str, caption: str) -> str:
    media_id = _upload_media(creds, data, filename, "application/pdf")
    payload = {
        "messaging_product": "whatsapp",
        "to": to.lstrip("+"),
        "type": "document",
        "document": {"id": media_id, "filename": filename, "caption": caption[:1024]},
    }
    return graph_request(creds, "POST", f"{creds.phone_number_id}/messages", payload)["messages"][0]["id"]


class PermanentSendError(RuntimeError):
    pass


def graph_request(creds: Creds, method: str, path: str, payload: dict | None = None, timeout: int = 20) -> dict:
    s = get_settings()
    req = urllib.request.Request(
        f"{s.whatsapp_api_base}/{path.lstrip('/')}",
        data=json.dumps(payload).encode() if payload is not None else None,
        method=method,
        headers={"Authorization": f"Bearer {creds.token}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 - fixed https Graph API base
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")[:400]
        try:
            detail = json.loads(detail)["error"]["message"]
        except (ValueError, KeyError, TypeError):
            pass
        if e.code == 429 or e.code >= 500:
            raise RuntimeError(f"WhatsApp API {e.code}: {detail}") from e  # retried with backoff
        raise PermanentSendError(f"WhatsApp API {e.code}: {detail}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"WhatsApp API unreachable: {e.reason}") from e


def _cloud_send(creds: Creds, to: str, body: str, template: str | None) -> str:
    if template:  # business-initiated (e.g. one-time code): approved template with one body parameter
        payload = {
            "messaging_product": "whatsapp",
            "to": to.lstrip("+"),
            "type": "template",
            "template": {
                "name": template,
                "language": {"code": "en"},
                "components": [{"type": "body", "parameters": [{"type": "text", "text": body}]}],
            },
        }
    else:
        payload = {"messaging_product": "whatsapp", "to": to.lstrip("+"), "type": "text", "text": {"body": body[:4096]}}
    return graph_request(creds, "POST", f"{creds.phone_number_id}/messages", payload)["messages"][0]["id"]


# ------------------------------------------------------------------ inbound


def tenant_for_number(session: Session, phone_number_id: str | None) -> Tenant | None:
    if phone_number_id:
        n = session.scalar(select(WhatsappNumber).where(WhatsappNumber.phone_number_id == phone_number_id))
        if n:
            return session.get(Tenant, n.tenant_id)
    code = get_settings().whatsapp_tenant_code
    return session.scalar(select(Tenant).where(Tenant.code == code)) if code else None


def inbound_job(session: Session, job: Job) -> None:
    """Channels worker handler for webhook events."""
    msg = job.payload.get("message") or {}
    tenant = tenant_for_number(session, job.payload.get("phone_number_id"))
    number = session.scalar(
        select(WhatsappNumber).where(WhatsappNumber.phone_number_id == (job.payload.get("phone_number_id") or ""))
    )
    if number is not None:
        number.last_inbound_at = utcnow()
    if tenant is None:
        log.warning("message for unknown business number %s dropped", job.payload.get("phone_number_id"))
        return
    mtype = msg.get("type")
    text = (msg.get("text") or {}).get("body") if mtype == "text" else None
    media = None
    if mtype == "interactive":
        reply = msg.get("interactive", {})
        text = (reply.get("button_reply") or reply.get("list_reply") or {}).get("id")
    elif mtype in ("image", "audio"):
        m = msg.get(mtype) or {}
        media = {"kind": mtype, "media_id": m.get("id"), "mime": m.get("mime_type"), "caption": m.get("caption")}
    handle_inbound(
        session,
        tenant,
        phone=msg.get("from", ""),
        text=text,
        wamid=msg.get("id") or job.idempotency_key,
        profile_name=None,
        media=media,
    )


MAX_MEDIA_BYTES = 10 * 1024 * 1024


def download_media(creds: Creds, media_id: str) -> tuple[bytes, str]:
    """Fetch a WhatsApp media file (photo / voice note) with the business's Cloud API token."""
    meta = graph_request(creds, "GET", media_id)
    if int(meta.get("file_size") or 0) > MAX_MEDIA_BYTES:
        raise ValueError("media file is larger than 10 MB")
    auth = {"Authorization": f"Bearer {creds.token}"}
    with urllib.request.urlopen(urllib.request.Request(meta["url"], headers=auth), timeout=60) as r:  # noqa: S310
        data = r.read(MAX_MEDIA_BYTES + 1)
    if len(data) > MAX_MEDIA_BYTES:
        raise ValueError("media file is larger than 10 MB")
    return data, (meta.get("mime_type") or "application/octet-stream").split(";")[0]


def handle_inbound(
    session: Session,
    tenant: Tenant,
    *,
    phone: str,
    text: str | None,
    wamid: str,
    profile_name: str | None = None,
    media: dict | None = None,
) -> list[str]:
    """Process one inbound message; returns the reply texts queued (useful for the simulator and tests)."""
    to = normalize_phone(phone)
    if to is None:
        return []
    if session.scalar(select(WhatsappMessage).where(WhatsappMessage.tenant_id == tenant.id, WhatsappMessage.wamid == wamid)):
        return []  # duplicate delivery
    contact = session.scalar(
        select(WhatsappContact).where(WhatsappContact.tenant_id == tenant.id, WhatsappContact.phone == to).with_for_update()
    )
    if contact is None:
        contact = WhatsappContact(tenant_id=tenant.id, phone=to, profile_name=profile_name, state="new")
        session.add(contact)
        session.flush()
    contact.last_message_at = utcnow()
    inbound = WhatsappMessage(
        tenant_id=tenant.id,
        contact_id=contact.id,
        direction="in",
        phone=to,
        wamid=wamid,
        body=text or (f"[{media['kind']}] {media.get('caption') or ''}".strip() if media else "[non-text message]"),
        status="received",
    )
    session.add(inbound)
    session.flush()
    replies: list[str] = []

    def reply(body: str) -> None:
        replies.append(body)
        queue_text(session, tenant.id, to, body, key=f"{wamid}:{len(replies)}")

    if contact.locked_until and contact.locked_until > utcnow():
        return []
    # Abuse / cost guard: cap messages per number per hour (also protects the free AI quota).
    recent = session.scalar(
        select(func.count())
        .select_from(WhatsappMessage)
        .where(
            WhatsappMessage.tenant_id == tenant.id,
            WhatsappMessage.contact_id == contact.id,
            WhatsappMessage.direction == "in",
            WhatsappMessage.created_at >= utcnow() - timedelta(hours=1),
        )
    )
    if recent > MAX_MESSAGES_PER_HOUR:
        if recent == MAX_MESSAGES_PER_HOUR + 1:
            reply("Bohat zyada messages aa gaye hain. Thori dair baad dobara bhejein. / Too many messages, please wait.")
        return replies
    if contact.state == "blocked":
        return []
    if media and contact.state == "verified" and contact.customer_id:
        _media_order(session, tenant, contact, inbound, media, wamid, to)
        return replies
    if text is None and media:
        text = media.get("caption") or "hi"  # not registered yet: continue the sign-up flow
    if text is None:
        reply("Maazrat, yeh message type abhi samajh nahi aata. Please type your order, ya photo/voice note bhejein.")
        return replies
    t = text.strip()
    low = re.sub(r"\s+", " ", t.lower()).strip(" .!?")
    name = tenant.display_name

    if contact.state == "verified" and contact.customer_id:
        customer = session.get(Customer, contact.customer_id)
        if low in BALANCE:
            from ordexa.modules.ai.replies import balance_text

            reply(balance_text(session, tenant.id, customer.id))
        elif low in HELP:
            reply(
                f"{name}: order bhejne ke liye items aur quantity likhein, e.g. '10 box panadol, 5 patta brufen'.\n"
                f"Balance dekhne ke liye 'balance' likhein."
            )
        elif (open_req := conversation.open_order(session, tenant.id, to)) and (
            answer := conversation.handle_reply(session, open_req, t)
        ) is not None:
            # An answer, a change ("balkay 20 box"), an extra item ("... bhi") or a cancel for the unfinished order.
            inbound.ai_order_id = open_req.id
            reply(answer)
        elif conversation.is_thanks(t):
            reply(f"Shukriya! {name} se order karne ka shukriya.")
        else:
            req, created = ai.submit(
                session,
                tenant_id=tenant.id,
                text=t,
                source="whatsapp",
                source_message_id=wamid,
                customer_id=customer.id,
                sender=to,
            )
            inbound.ai_order_id = req.id
            if created and (conversation.ADD.search(t) or conversation.SAME_BILL.search(t)):
                prev = session.scalar(
                    select(AiOrderRequest)
                    .where(
                        AiOrderRequest.tenant_id == tenant.id,
                        AiOrderRequest.sender == to,
                        AiOrderRequest.id != req.id,
                        AiOrderRequest.status.notin_(("closed", "message")),
                    )
                    .order_by(AiOrderRequest.received_at.desc())
                    .limit(1)
                )
                bill = session.get(Invoice, prev.invoice_id) if prev is not None and prev.invoice_id else None
                if bill is not None and sales.amend_blocker(session, bill) is None:
                    # Same day, bill not returned: these items go into that bill when staff approve.
                    req.trace = (req.trace or []) + [
                        {"step": "add_to_bill", "invoice_id": str(bill.id), "number": bill.number, "order": prev.number}
                    ]
                elif (
                    prev is not None
                    and prev.status == "approved"
                    and prev.reviewed_at
                    and prev.reviewed_at > utcnow() - timedelta(hours=6)
                ):
                    req.trace = (req.trace or []) + [{"step": "after_billed_order", "number": prev.number}]
            if created:
                ai.run_inline_if_configured(session, req)
        return replies

    if contact.state == "awaiting_choice" and low not in YES | NO and re.fullmatch(r"[A-Za-z]{0,3}-?\d{2,8}", t):
        contact.state = "awaiting_code"  # typed the shop code straight away
    elif contact.state == "awaiting_choice" and low.split(" ")[0] in YES:
        low = "1"
    if contact.state in ("new",) or low in HELP:
        contact.state = "awaiting_choice"
        reply(
            f"Assalam-o-Alaikum! {name} WhatsApp ordering.\nKya aap ka shop pehle se registered hai?\n"
            f"1) Haan, mera shop code hai\n2) Naya account banana hai\n(Reply 1 or 2)"
        )
    elif contact.state == "awaiting_choice":
        if low in YES:
            contact.state = "awaiting_code"
            reply("Apna shop code likhein (jo bill par likha hota hai), e.g. 101.")
        elif low in NO:
            contact.state = "awaiting_details"
            reply(
                "Naya account: shop ka naam, area aur owner ka naam aik message mein likhein.\n"
                "e.g. 'Madina Medical Store, Samanabad, Ahmed Ali'"
            )
        else:
            reply("Please reply 1 (registered shop) or 2 (new account).")
    elif contact.state == "awaiting_code":
        customer = session.scalar(
            select(Customer).where(Customer.tenant_id == tenant.id, Customer.code == t.upper(), Customer.is_active.is_(True))
        )
        if customer is None:
            customer = session.scalar(
                select(Customer).where(Customer.tenant_id == tenant.id, Customer.code == t, Customer.is_active.is_(True))
            )
        if customer is None:
            _fail(contact)
            reply(
                "Bohat ghalat koshishen. Aap ka number 1 ghante ke liye rok diya gaya hai, phir dobara try karein."
                if contact.locked_until and contact.locked_until > utcnow()
                else "Yeh code nahi mila. Dobara check karke likhein, ya 2 likh kar naya account banayein."
            )
            if low in NO:
                contact.state = "awaiting_details"
        else:
            contact.claimed_customer_id = customer.id
            on_file = normalize_phone(customer.phone)
            if on_file == to:
                _verify(session, contact, customer, verified_by=None, how="number on file")
                reply(f"Shukriya! Aap {customer.name} ke taur par verify ho gaye. Ab order bhej sakte hain.")
            elif on_file:
                code = f"{secrets.randbelow(10**6):06d}"
                contact.otp_hash = _hash_otp(contact.id, code)
                contact.otp_expires_at = utcnow() + OTP_TTL
                contact.state = "awaiting_otp"
                queue_text(
                    session,
                    tenant.id,
                    on_file,
                    f"{name}: WhatsApp ordering verification code {code}. Kisi ko na batayein.",
                    key=f"otp:{contact.id}:{wamid}",
                    template="ordexa_otp",
                )
                reply(
                    f"{customer.name} ke registered number (...{on_file[-4:]}) par 6-digit code bheja hai. "
                    f"Woh code yahan likhein. Ya staff aap ko approve kar sakta hai."
                )
            else:
                contact.state = "pending_approval"
                reply("Shukriya! Hamari team aap ka number verify karke jald activate kar degi.")
    elif contact.state == "awaiting_otp":
        ok = (
            contact.otp_hash
            and contact.otp_expires_at
            and contact.otp_expires_at > utcnow()
            and hmac.compare_digest(contact.otp_hash, _hash_otp(contact.id, re.sub(r"\D", "", t)))
        )
        if ok:
            customer = session.get(Customer, contact.claimed_customer_id)
            _verify(session, contact, customer, verified_by=None, how="one-time code")
            reply(f"Verify ho gaya! Ab {customer.name} ke liye order bhej sakte hain.")
        else:
            _fail(contact)
            if contact.otp_expires_at and contact.otp_expires_at <= utcnow():
                contact.state = "awaiting_code"
                reply("Code expire ho gaya. Shop code dobara likhein.")
            else:
                reply(
                    "Bohat ghalat koshishen. Aap ka number 1 ghante ke liye rok diya gaya hai, phir dobara try karein."
                    if contact.locked_until and contact.locked_until > utcnow()
                    else "Code ghalat hai. Dobara likhein."
                )
    elif contact.state == "awaiting_details":
        contact.registration_details = t[:500]
        contact.state = "pending_approval"
        reply("Shukriya! Aap ki details mil gayi hain. Hamari team verify karke account activate kar degi.")
        record(
            session,
            tenant_id=tenant.id,
            action="whatsapp.signup_requested",
            entity_type="whatsapp_contact",
            entity_id=contact.id,
            data={"phone": to},
        )
    elif contact.state == "pending_approval":
        reply("Aap ki request verification mein hai. Activate hote hi aap ko bata diya jayega.")
    return replies


def _fail(contact: WhatsappContact) -> None:
    contact.failed_attempts += 1
    if contact.failed_attempts >= MAX_FAILED:
        contact.locked_until = utcnow() + LOCK_FOR
        contact.failed_attempts = 0
        contact.state = "awaiting_choice"


def _verify(session: Session, contact: WhatsappContact, customer: Customer, verified_by: uuid.UUID | None, how: str) -> None:
    contact.customer_id = customer.id
    contact.state = "verified"
    contact.verified_at = utcnow()
    contact.verified_by = verified_by
    contact.otp_hash = None
    contact.failed_attempts = 0
    record(
        session,
        tenant_id=contact.tenant_id,
        actor_user_id=verified_by,
        action="whatsapp.verified",
        entity_type="whatsapp_contact",
        entity_id=contact.id,
        data={"phone": contact.phone, "customer": customer.code, "how": how},
    )


# ------------------------------------------------------------------ staff actions (human in the loop)


def approve_contact(
    session: Session, actor: Principal, contact_id: uuid.UUID, *, customer_id: uuid.UUID | None, new_customer: dict | None
) -> WhatsappContact:
    """Link a phone to an existing customer, or create the customer from the sign-up details."""
    actor.require("customer.create")
    contact = session.get(WhatsappContact, contact_id)
    if not contact or contact.tenant_id != actor.tenant_id:
        raise OrdexaError(ErrorCode.NOT_FOUND, "contact not found")
    if contact.state == "verified":
        raise OrdexaError(ErrorCode.CONFLICT, "already verified")
    if customer_id:
        customer = session.get(Customer, customer_id)
        if not customer or customer.tenant_id != actor.tenant_id:
            raise OrdexaError(ErrorCode.NOT_FOUND, "customer not found")
    elif new_customer:
        lock_master_data(session, actor.tenant_id)
        if session.scalar(select(Customer).where(Customer.tenant_id == actor.tenant_id, Customer.code == new_customer["code"])):
            raise OrdexaError(ErrorCode.CONFLICT, f"customer code '{new_customer['code']}' already exists")
        customer = Customer(
            tenant_id=actor.tenant_id,
            code=new_customer["code"],
            name=new_customer["name"],
            phone=contact.phone,
            address=new_customer.get("address"),
        )
        session.add(customer)
        session.flush()
    else:
        raise OrdexaError(ErrorCode.VALIDATION_ERROR, "choose a customer or enter new customer details")
    _verify(session, contact, customer, verified_by=actor.user_id, how="staff approval")
    queue_text(
        session,
        actor.tenant_id,
        contact.phone,
        f"Mubarak! Aap ka WhatsApp ordering account ({customer.name}, code {customer.code}) activate ho gaya. "
        f"Ab order bhej sakte hain, e.g. '10 box panadol'.",
        key=f"approved:{contact.id}",
    )
    return contact


def block_contact(session: Session, actor: Principal, contact_id: uuid.UUID) -> WhatsappContact:
    actor.require("customer.create")
    contact = session.get(WhatsappContact, contact_id)
    if not contact or contact.tenant_id != actor.tenant_id:
        raise OrdexaError(ErrorCode.NOT_FOUND, "contact not found")
    contact.state = "blocked"
    contact.customer_id = None
    record(
        session,
        tenant_id=actor.tenant_id,
        actor_user_id=actor.user_id,
        action="whatsapp.blocked",
        entity_type="whatsapp_contact",
        entity_id=contact.id,
        data={"phone": contact.phone},
    )
    return contact


def unlink_contact(session: Session, actor: Principal, contact_id: uuid.UUID) -> WhatsappContact:
    """Revoke ordering for a number (lost phone, staff left the shop). The number must sign up again."""
    actor.require("customer.update")
    contact = session.get(WhatsappContact, contact_id)
    if not contact or contact.tenant_id != actor.tenant_id:
        raise OrdexaError(ErrorCode.NOT_FOUND, "contact not found")
    contact.state = "new"
    contact.customer_id = None
    contact.claimed_customer_id = None
    record(
        session,
        tenant_id=actor.tenant_id,
        actor_user_id=actor.user_id,
        action="whatsapp.unlinked",
        entity_type="whatsapp_contact",
        entity_id=contact.id,
        data={"phone": contact.phone},
    )
    return contact


def _media_order(
    session: Session, tenant: Tenant, contact: WhatsappContact, inbound: WhatsappMessage, media: dict, wamid: str, to: str
) -> None:
    """A verified shop sent a photo of its order list or a voice note: keep the file with a new order request.
    Photos are read by Claude; voice notes wait for staff to listen (or a speech-to-text provider, when added)."""
    data, mime = media.get("data"), media.get("mime")
    error = None
    creds = credentials(session, tenant.id) if data is None and media.get("media_id") else None
    if creds is not None:
        try:
            data, mime = download_media(creds, media["media_id"])
        except Exception as exc:  # noqa: BLE001 - the order still reaches staff, with the error
            error = f"could not download the {media['kind']}: {exc}"[:300]
            log.warning("media download failed for %s: %s", wamid, exc)
    attachment_id = None
    if data:
        att = AiAttachment(
            tenant_id=tenant.id,
            media_type=(mime or "application/octet-stream"),
            size=len(data),
            data=data,
            source_media_id=media.get("media_id"),
        )
        session.add(att)
        session.flush()
        attachment_id = att.id
    req, created = ai.submit(
        session,
        tenant_id=tenant.id,
        text=media.get("caption") or "",
        source="whatsapp",
        source_message_id=wamid,
        customer_id=contact.customer_id,
        sender=to,
        kind=media["kind"],
        attachment_id=attachment_id,
    )
    if error:
        req.error = error
    inbound.ai_order_id = req.id
    if created:
        ai.run_inline_if_configured(session, req)
