from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import DateTime, ForeignKey, Integer, Numeric, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ordexa.shared.db import Base


class Invoice(Base):
    """A posted sales invoice. The primary key is generated on the desktop (UUID) so an offline invoice
    has the same identity before and after sync; replaying a push can never create a second copy."""

    __tablename__ = "invoices"
    __table_args__ = (UniqueConstraint("tenant_id", "number"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)  # client-generated
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"), index=True)
    device_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("devices.id"))
    warehouse_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("warehouses.id"))
    number: Mapped[str] = mapped_column(String(30))  # e.g. T1-000123
    customer_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("customers.id"), index=True)  # None = walk-in
    customer_name: Mapped[str] = mapped_column(String(200))  # snapshot
    status: Mapped[str] = mapped_column(String(20), default="posted")
    source: Mapped[str] = mapped_column(String(20))  # desktop | whatsapp_ai | api
    issued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))  # device clock at billing
    synced_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    subtotal: Mapped[Decimal] = mapped_column(Numeric(14, 2))
    discount: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=Decimal(0))
    total: Mapped[Decimal] = mapped_column(Numeric(14, 2))
    paid_amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=Decimal(0))
    flags: Mapped[list] = mapped_column(JSONB, default=list)  # oversold, credit_exceeded, price_changed, ...
    payload_hash: Mapped[str] = mapped_column(String(64))  # detects a replay with different content
    created_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))

    lines: Mapped[list[InvoiceLine]] = relationship(
        back_populates="invoice", order_by="InvoiceLine.line_no", cascade="all, delete-orphan"
    )


class InvoiceLine(Base):
    __tablename__ = "invoice_lines"

    invoice_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("invoices.id", ondelete="CASCADE"), primary_key=True)
    line_no: Mapped[int] = mapped_column(Integer, primary_key=True)
    product_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("products.id"))
    batch_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("batches.id"))
    product_name: Mapped[str] = mapped_column(String(200))  # snapshot (SRS PRICE-008)
    batch_no: Mapped[str | None] = mapped_column(String(60))
    quantity: Mapped[Decimal] = mapped_column(Numeric(14, 3))
    bonus_quantity: Mapped[Decimal] = mapped_column(Numeric(14, 3), default=Decimal(0))  # e.g. 10+1 scheme
    unit: Mapped[str] = mapped_column(String(20))
    unit_price: Mapped[Decimal] = mapped_column(Numeric(14, 2))
    discount: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=Decimal(0))
    line_total: Mapped[Decimal] = mapped_column(Numeric(14, 2))

    invoice: Mapped[Invoice] = relationship(back_populates="lines")
