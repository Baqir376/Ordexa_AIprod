"""Gap-free per-tenant document numbers for server-side documents (PI-000001, CN-000001, W-000001...)."""

from __future__ import annotations

import uuid

from sqlalchemy import Integer, String, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Mapped, Session, mapped_column

from ordexa.shared.db import Base


class DocumentSequence(Base):
    __tablename__ = "document_sequences"

    tenant_id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(String(20), primary_key=True)
    next_value: Mapped[int] = mapped_column(Integer, default=1)


def next_number(session: Session, tenant_id: uuid.UUID, key: str, width: int = 6) -> str:
    """Allocate the next number in the caller's transaction (row lock; rolled back with it)."""
    session.execute(
        insert(DocumentSequence)
        .values(tenant_id=tenant_id, key=key, next_value=1)
        .on_conflict_do_nothing(index_elements=["tenant_id", "key"])
    )
    row = session.scalar(
        select(DocumentSequence).where(DocumentSequence.tenant_id == tenant_id, DocumentSequence.key == key).with_for_update()
    )
    value = row.next_value
    row.next_value = value + 1
    return f"{key}-{value:0{width}d}"
