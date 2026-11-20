from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from ordexa.shared.db import Base, SyncedMixin, TimestampMixin, new_id


class Tenant(SyncedMixin, Base):
    """A customer business (e.g. Ali Chemist). Branding and licence sync down to its desktops."""

    __tablename__ = "tenants"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=new_id)
    code: Mapped[str] = mapped_column(String(40), unique=True)  # typed at login, e.g. "ali-chemist"
    name: Mapped[str] = mapped_column(String(200))
    owner_user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", use_alter=True, name="fk_tenants_owner"))
    timezone: Mapped[str] = mapped_column(String(64), default="Asia/Karachi")
    currency: Mapped[str] = mapped_column(String(3), default="PKR")
    # Licence
    license_plan: Mapped[str] = mapped_column(String(20), default="basic")  # basic | advance | plus
    license_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    max_devices: Mapped[int] = mapped_column(Integer, default=2)
    # White-label branding shown in the desktop app
    display_name: Mapped[str] = mapped_column(String(200))
    license_text: Mapped[str] = mapped_column(String(200))  # e.g. "Licensed to Ali Chemist"
    logo_data_url: Mapped[str | None] = mapped_column(Text)  # small PNG/SVG as data: URL
    primary_color: Mapped[str] = mapped_column(String(9), default="#0F766E")
    address: Mapped[str | None] = mapped_column(String(300))
    phone: Mapped[str | None] = mapped_column(String(30))


class Branch(TimestampMixin, Base):
    __tablename__ = "branches"
    __table_args__ = (UniqueConstraint("tenant_id", "code"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=new_id)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"), index=True)
    code: Mapped[str] = mapped_column(String(20))
    name: Mapped[str] = mapped_column(String(200))


class Warehouse(SyncedMixin, Base):
    __tablename__ = "warehouses"
    __table_args__ = (UniqueConstraint("tenant_id", "code"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=new_id)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"), index=True)
    branch_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("branches.id"))
    code: Mapped[str] = mapped_column(String(20))
    name: Mapped[str] = mapped_column(String(200))
