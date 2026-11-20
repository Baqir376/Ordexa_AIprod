from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

from sqlalchemy import Boolean, Date, ForeignKey, Numeric, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from ordexa.shared.db import Base, SyncedMixin, new_id


class Product(SyncedMixin, Base):
    __tablename__ = "products"
    __table_args__ = (UniqueConstraint("tenant_id", "code"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=new_id)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"), index=True)
    code: Mapped[str] = mapped_column(String(40))
    name: Mapped[str] = mapped_column(String(200))
    generic_name: Mapped[str | None] = mapped_column(String(200))
    manufacturer: Mapped[str | None] = mapped_column(String(200))
    pack_size: Mapped[str | None] = mapped_column(String(60))  # e.g. "10x10 tablets"
    sale_unit: Mapped[str] = mapped_column(String(20), default="box")
    sale_price: Mapped[Decimal] = mapped_column(Numeric(14, 2))
    barcode: Mapped[str | None] = mapped_column(String(60), index=True)
    track_batches: Mapped[bool] = mapped_column(Boolean, default=True)
    reorder_level: Mapped[Decimal | None] = mapped_column(Numeric(14, 3))  # low-stock alert / auto reorder
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


class Batch(SyncedMixin, Base):
    """Pharma batch/lot with expiry. Sales pick earliest-expiry first (FEFO)."""

    __tablename__ = "batches"
    __table_args__ = (UniqueConstraint("tenant_id", "product_id", "batch_no"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=new_id)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"), index=True)
    product_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("products.id"), index=True)
    batch_no: Mapped[str] = mapped_column(String(60))
    expiry_date: Mapped[date | None] = mapped_column(Date)
    sale_price: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))  # overrides product price if set
    cost_price: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))  # from the latest purchase (stock valuation)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
