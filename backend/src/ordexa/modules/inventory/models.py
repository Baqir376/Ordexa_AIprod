from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import DateTime, ForeignKey, Index, Integer, Numeric, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from ordexa.shared.db import Base, new_id


class InventoryTransaction(Base):
    """Immutable stock ledger (SRS INV-002). Balances are sums of this table, never edited in place.
    ``(tenant_id, source_type, source_id, source_line)`` is unique so a document line posts at most once."""

    __tablename__ = "inventory_transactions"
    __table_args__ = (
        UniqueConstraint("tenant_id", "source_type", "source_id", "source_line", name="uq_inv_source"),
        Index("ix_inv_balance", "tenant_id", "warehouse_id", "product_id", "batch_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=new_id)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"))
    warehouse_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("warehouses.id"))
    product_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("products.id"))
    batch_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("batches.id"))
    qty_delta: Mapped[Decimal] = mapped_column(Numeric(14, 3))
    kind: Mapped[str] = mapped_column(String(20))  # opening | sale | receipt | adjustment
    source_type: Mapped[str] = mapped_column(String(30))
    source_id: Mapped[uuid.UUID] = mapped_column()
    source_line: Mapped[int] = mapped_column(Integer, default=0)
    reason: Mapped[str | None] = mapped_column(String(300))
    created_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
