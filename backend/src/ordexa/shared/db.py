"""Database engine, session handling and shared column helpers."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from functools import lru_cache

from sqlalchemy import BigInteger, DateTime, Sequence, create_engine, func, text
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

from ordexa.config import get_settings

# One global, monotonically increasing sequence stamps every row that desktops sync down.
# Master-data writers take a per-tenant advisory lock (see ``lock_master_data``) so that, within a
# tenant, sequence order equals commit order and a pull cursor can never skip a row.
SYNC_SEQ = Sequence("sync_seq")


def utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class SyncedMixin(TimestampMixin):
    """Rows that desktops pull. ``change_seq`` is bumped on every insert and update."""

    change_seq: Mapped[int] = mapped_column(
        BigInteger, SYNC_SEQ, server_default=SYNC_SEQ.next_value(), onupdate=SYNC_SEQ.next_value(), index=True
    )


def new_id() -> uuid.UUID:
    return uuid.uuid4()


@lru_cache
def get_engine():
    return create_engine(get_settings().database_url, pool_pre_ping=True)


@lru_cache
def get_sessionmaker() -> sessionmaker[Session]:
    return sessionmaker(bind=get_engine(), expire_on_commit=False)


def get_session() -> Iterator[Session]:
    """FastAPI dependency: one session per request; commit is explicit in services."""
    with get_sessionmaker()() as session:
        yield session


def lock_master_data(session: Session, tenant_id: uuid.UUID) -> None:
    """Serialise master-data writes per tenant for the rest of the transaction (see SYNC_SEQ)."""
    session.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:k, 0))"), {"k": f"master:{tenant_id}"})
