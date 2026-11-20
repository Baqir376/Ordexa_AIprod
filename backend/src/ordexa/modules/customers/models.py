from __future__ import annotations

import uuid
from decimal import Decimal

from sqlalchemy import Boolean, ForeignKey, Numeric, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from ordexa.shared.db import Base, SyncedMixin, new_id


class Customer(SyncedMixin, Base):
    """A retail shop / buyer. ``code`` is the shop key (e.g. 101) used for WhatsApp registration later."""

    __tablename__ = "customers"
    __table_args__ = (UniqueConstraint("tenant_id", "code"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=new_id)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"), index=True)
    code: Mapped[str] = mapped_column(String(40))
    name: Mapped[str] = mapped_column(String(200))
    phone: Mapped[str | None] = mapped_column(String(30))
    address: Mapped[str | None] = mapped_column(String(300))
    credit_limit: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))  # None = no credit control
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
