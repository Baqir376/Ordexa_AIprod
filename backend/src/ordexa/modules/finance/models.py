from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import Date, DateTime, ForeignKey, Index, Integer, Numeric, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ordexa.shared.db import Base, new_id


class LedgerEntry(Base):
    """Party ledger: one row per balance-changing event for a customer or supplier.
    Customer: positive = they owe us more. Supplier: positive = we owe them more.
    ``(tenant_id, kind, source_id)`` is unique so a document can only post once."""

    __tablename__ = "ledger_entries"
    __table_args__ = (
        UniqueConstraint("tenant_id", "kind", "source_id", name="uq_ledger_source"),
        Index("ix_ledger_party", "tenant_id", "party_type", "party_id", "entry_date"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=new_id)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"))
    party_type: Mapped[str] = mapped_column(String(10))  # customer | supplier
    party_id: Mapped[uuid.UUID] = mapped_column()
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 2))
    # invoice, receipt, credit_note, purchase, payment, opening, cheque_bounced
    kind: Mapped[str] = mapped_column(String(20))
    source_id: Mapped[uuid.UUID] = mapped_column()
    reference: Mapped[str] = mapped_column(String(60))  # human document number
    entry_date: Mapped[date] = mapped_column(Date)
    memo: Mapped[str | None] = mapped_column(String(300))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Payment(Base):
    """Money in (customer receipt) or out (supplier payment). Cheques track clearing status."""

    __tablename__ = "payments"
    __table_args__ = (UniqueConstraint("tenant_id", "number"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)  # client-generated for idempotency
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"), index=True)
    number: Mapped[str] = mapped_column(String(30))
    direction: Mapped[str] = mapped_column(String(3))  # in | out
    party_type: Mapped[str] = mapped_column(String(10))
    party_id: Mapped[uuid.UUID] = mapped_column(index=True)
    party_name: Mapped[str] = mapped_column(String(200))
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 2))
    method: Mapped[str] = mapped_column(String(10))  # cash | bank | cheque
    payment_date: Mapped[date] = mapped_column(Date)
    cheque_no: Mapped[str | None] = mapped_column(String(40))
    cheque_bank: Mapped[str | None] = mapped_column(String(100))
    cheque_date: Mapped[date | None] = mapped_column(Date)
    cheque_status: Mapped[str | None] = mapped_column(String(10))  # pending | cleared | bounced
    source_invoice_id: Mapped[uuid.UUID | None] = mapped_column()  # cash paid at the counter
    device_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("devices.id"))
    note: Mapped[str | None] = mapped_column(String(300))
    created_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class CreditNote(Base):
    """Sales return: goods back into stock, customer balance reduced."""

    __tablename__ = "credit_notes"
    __table_args__ = (UniqueConstraint("tenant_id", "number"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"), index=True)
    number: Mapped[str] = mapped_column(String(30))
    customer_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("customers.id"), index=True)
    invoice_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("invoices.id"))
    warehouse_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("warehouses.id"))
    note_date: Mapped[date] = mapped_column(Date)
    reason: Mapped[str] = mapped_column(String(300))
    total: Mapped[Decimal] = mapped_column(Numeric(14, 2))
    created_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    lines: Mapped[list[CreditNoteLine]] = relationship(cascade="all, delete-orphan", order_by="CreditNoteLine.line_no")


class CreditNoteLine(Base):
    __tablename__ = "credit_note_lines"

    credit_note_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("credit_notes.id", ondelete="CASCADE"), primary_key=True)
    line_no: Mapped[int] = mapped_column(Integer, primary_key=True)
    product_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("products.id"))
    batch_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("batches.id"))
    product_name: Mapped[str] = mapped_column(String(200))
    quantity: Mapped[Decimal] = mapped_column(Numeric(14, 3))
    unit_price: Mapped[Decimal] = mapped_column(Numeric(14, 2))
    line_total: Mapped[Decimal] = mapped_column(Numeric(14, 2))
    restock: Mapped[bool] = mapped_column(default=True)  # False = damaged/expired, not returned to sellable stock


class Expense(Base):
    __tablename__ = "expenses"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"), index=True)
    category: Mapped[str] = mapped_column(String(60))
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 2))
    expense_date: Mapped[date] = mapped_column(Date)
    method: Mapped[str] = mapped_column(String(10), default="cash")
    note: Mapped[str | None] = mapped_column(String(300))
    created_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
