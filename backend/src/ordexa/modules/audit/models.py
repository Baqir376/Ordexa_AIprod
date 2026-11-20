from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, Session, mapped_column

from ordexa.shared.db import Base, new_id


class AuditEvent(Base):
    __tablename__ = "audit_events"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=new_id)
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("tenants.id"), index=True)
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    actor_device_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("devices.id"))
    action: Mapped[str] = mapped_column(String(60))
    entity_type: Mapped[str] = mapped_column(String(40))
    entity_id: Mapped[str] = mapped_column(String(60))
    data: Mapped[dict] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


def record(
    session: Session,
    *,
    tenant_id,
    action: str,
    entity_type: str,
    entity_id,
    actor_user_id=None,
    actor_device_id=None,
    data: dict | None = None,
) -> None:
    """Append an audit event in the caller's transaction (it commits or rolls back with the change)."""
    session.add(
        AuditEvent(
            tenant_id=tenant_id,
            actor_user_id=actor_user_id,
            actor_device_id=actor_device_id,
            action=action,
            entity_type=entity_type,
            entity_id=str(entity_id),
            data=data or {},
        )
    )
