from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from ordexa.shared.db import Base, new_id


class WhatsappNumber(Base):
    """Maps a WhatsApp Business phone number (Meta phone_number_id) to the business that owns it."""

    __tablename__ = "whatsapp_numbers"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=new_id)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"), index=True)
    phone_number_id: Mapped[str] = mapped_column(String(40), unique=True)
    display_phone: Mapped[str] = mapped_column(String(30))
    # Connection details entered in Settings -> WhatsApp (secrets encrypted with shared.crypto, never returned)
    access_token_enc: Mapped[str | None] = mapped_column(Text)
    app_secret_enc: Mapped[str | None] = mapped_column(Text)
    verify_token: Mapped[str | None] = mapped_column(String(80))
    public_url: Mapped[str | None] = mapped_column(String(300))  # where Meta reaches this server (tunnel or host)
    waba_id: Mapped[str | None] = mapped_column(String(40))  # WhatsApp Business Account ID (to subscribe the app)
    last_inbound_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class WhatsappContact(Base):
    """A phone number talking to a business's WhatsApp line, and where it is in the sign-up flow.
    Orders are accepted only from ``verified`` contacts linked to a customer (never from random numbers)."""

    __tablename__ = "whatsapp_contacts"
    __table_args__ = (UniqueConstraint("tenant_id", "phone"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=new_id)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"), index=True)
    phone: Mapped[str] = mapped_column(String(20))  # E.164
    profile_name: Mapped[str | None] = mapped_column(String(100))
    # new | awaiting_choice | awaiting_code | awaiting_otp | awaiting_details | pending_approval | verified | blocked
    state: Mapped[str] = mapped_column(String(20), default="new", index=True)
    customer_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("customers.id"))  # set only when verified
    claimed_customer_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("customers.id"))  # shop code entered
    registration_details: Mapped[str | None] = mapped_column(String(500))  # new shop: name, area, owner
    otp_hash: Mapped[str | None] = mapped_column(String(200))
    otp_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    failed_attempts: Mapped[int] = mapped_column(Integer, default=0)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    verified_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_message_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))  # 24-hour reply window
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class WhatsappMessage(Base):
    """Every inbound and outbound message (inbound deduplicated by WhatsApp message id)."""

    __tablename__ = "whatsapp_messages"
    __table_args__ = (UniqueConstraint("tenant_id", "wamid"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=new_id)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"), index=True)
    contact_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("whatsapp_contacts.id"), index=True)
    direction: Mapped[str] = mapped_column(String(3))  # in | out
    phone: Mapped[str] = mapped_column(String(20))
    wamid: Mapped[str | None] = mapped_column(String(120))
    body: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(12))  # received | sent | simulated | failed
    ai_order_id: Mapped[uuid.UUID | None] = mapped_column()
    error: Mapped[str | None] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
