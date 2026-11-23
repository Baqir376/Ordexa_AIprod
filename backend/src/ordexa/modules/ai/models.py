from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, LargeBinary, String, Text, UniqueConstraint, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from ordexa.shared.db import Base, new_id


class AiOrderRequest(Base):
    """One incoming order message and its AI-assisted processing. AI output here is a proposal only;
    an invoice exists only after a person approves it (SRS AI-004, SALE-011)."""

    __tablename__ = "ai_order_requests"
    __table_args__ = (UniqueConstraint("tenant_id", "source", "source_message_id", name="uq_ai_source_message"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=new_id)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"), index=True)
    number: Mapped[str] = mapped_column(String(30))  # AO-000001
    source: Mapped[str] = mapped_column(String(20))  # whatsapp | manual
    source_message_id: Mapped[str] = mapped_column(String(120))  # wamid or client id (dedup)
    sender: Mapped[str | None] = mapped_column(String(40))  # verified phone (E.164)
    customer_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("customers.id"), index=True)
    raw_text: Mapped[str] = mapped_column(Text)  # preserved exactly as received (AI-001); caption for media
    kind: Mapped[str] = mapped_column(String(10), default="text", server_default="text")  # text | image | audio
    attachment_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("ai_attachments.id"))
    # received | processing | needs_review | draft_ready | approved | rejected | failed
    status: Mapped[str] = mapped_column(String(20), default="received", index=True)
    extractor: Mapped[str | None] = mapped_column(String(60))  # e.g. "anthropic:claude-opus-5-5" | "rules:v1"
    extraction: Mapped[dict | None] = mapped_column(JSONB)  # schema-validated model/rule output
    lines: Mapped[list] = mapped_column(JSONB, default=list)  # resolved lines with candidates and evidence
    warnings: Mapped[list] = mapped_column(JSONB, default=list)
    questions: Mapped[list] = mapped_column(JSONB, default=list)  # clarification questions for the customer
    trace: Mapped[list] = mapped_column(JSONB, default=list)  # evidence and decision trace (AI-020)
    error: Mapped[str | None] = mapped_column(Text)
    invoice_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("invoices.id"))
    reviewed_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    review_note: Mapped[str | None] = mapped_column(String(300))
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AiAttachment(Base):
    """Photo of an order list or a voice note, kept with the order so staff can see/hear the original."""

    __tablename__ = "ai_attachments"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=new_id)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"), index=True)
    media_type: Mapped[str] = mapped_column(String(100))  # image/jpeg, audio/ogg ...
    size: Mapped[int] = mapped_column(Integer)
    data: Mapped[bytes] = mapped_column(LargeBinary)
    source_media_id: Mapped[str | None] = mapped_column(String(120))  # WhatsApp media id
    transcript: Mapped[str | None] = mapped_column(Text)  # speech-to-text, when a provider is configured
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ProductAlias(Base):
    """Approved operational memory: "this customer's 'usual panadol' means product X". Versioned:
    a correction supersedes the current row instead of overwriting it (AI-016, AI-017)."""

    __tablename__ = "ai_product_aliases"
    __table_args__ = (
        Index(
            "uq_ai_alias_current",
            "tenant_id",
            text("coalesce(customer_id, '00000000-0000-0000-0000-000000000000'::uuid)"),
            "phrase",
            unique=True,
            postgresql_where=text("status = 'current'"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=new_id)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"), index=True)
    customer_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("customers.id"))  # None = whole business
    phrase: Mapped[str] = mapped_column(String(200))  # normalised
    product_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("products.id"))
    version: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(12), default="current")  # current | superseded | revoked
    supersedes_id: Mapped[uuid.UUID | None] = mapped_column()
    source_request_id: Mapped[uuid.UUID | None] = mapped_column()
    reason: Mapped[str | None] = mapped_column(String(300))
    created_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
