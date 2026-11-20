from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from ordexa.shared.db import Base, TimestampMixin, new_id


class Device(TimestampMixin, Base):
    """An activated desktop (billing terminal). Each gets its own invoice series, e.g. ``T1-``,
    so two PCs billing offline can never produce the same invoice number."""

    __tablename__ = "devices"
    __table_args__ = (UniqueConstraint("tenant_id", "terminal_code"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=new_id)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"), index=True)
    name: Mapped[str] = mapped_column(String(100))
    terminal_code: Mapped[str] = mapped_column(String(10))  # T1, T2, ...
    warehouse_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("warehouses.id"))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    activated_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    last_sync_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
