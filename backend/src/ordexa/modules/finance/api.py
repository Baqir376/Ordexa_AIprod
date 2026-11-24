from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ordexa.modules.finance import service
from ordexa.modules.finance.models import CreditNote, Expense, Payment
from ordexa.modules.identity.service import Principal, current_principal, requires
from ordexa.modules.tenants.timeutil import business_today
from ordexa.shared.db import get_session
from ordexa.shared.errors import ErrorCode, OrdexaError

router = APIRouter(prefix="/api/v1", tags=["finance"])
D2 = {"max_digits": 14, "decimal_places": 2}


class PaymentBody(BaseModel):
    id: uuid.UUID
    direction: str = Field(pattern="^(in|out)$")
    party_id: uuid.UUID
    amount: Decimal = Field(gt=0, **D2)
    method: str = Field(default="cash", pattern="^(cash|bank|cheque)$")
    payment_date: date | None = None
    cheque_no: str | None = Field(default=None, max_length=40)
    cheque_bank: str | None = Field(default=None, max_length=100)
    cheque_date: date | None = None
    note: str | None = Field(default=None, max_length=300)


class ChequeStatusBody(BaseModel):
    status: str = Field(pattern="^(cleared|bounced)$")


class ReturnLineBody(BaseModel):
    product_id: uuid.UUID
    batch_id: uuid.UUID | None = None
    quantity: Decimal = Field(gt=0, max_digits=14, decimal_places=3)
    unit_price: Decimal = Field(ge=0, **D2)
    restock: bool = True


class CreditNoteBody(BaseModel):
    id: uuid.UUID
    customer_id: uuid.UUID
    invoice_id: uuid.UUID | None = None
    warehouse_id: uuid.UUID | None = None
    reason: str = Field(min_length=3, max_length=300)
    lines: list[ReturnLineBody] = Field(min_length=1, max_length=200)


class ExpenseBody(BaseModel):
    id: uuid.UUID
    category: str = Field(min_length=1, max_length=60)
    amount: Decimal = Field(gt=0, **D2)
    expense_date: date | None = None
    method: str = Field(default="cash", pattern="^(cash|bank|cheque)$")
    note: str | None = Field(default=None, max_length=300)


def payment_out(x: Payment) -> dict:
    return {
        "id": str(x.id),
        "number": x.number,
        "direction": x.direction,
        "party_type": x.party_type,
        "party_id": str(x.party_id),
        "party_name": x.party_name,
        "amount": str(x.amount),
        "method": x.method,
        "payment_date": x.payment_date.isoformat(),
        "cheque_no": x.cheque_no,
        "cheque_bank": x.cheque_bank,
        "cheque_date": x.cheque_date.isoformat() if x.cheque_date else None,
        "cheque_status": x.cheque_status,
        "note": x.note,
    }


@router.get("/payments")
def list_payments(
    direction: str | None = None,
    cheques_only: bool = False,
    limit: int = 200,
    p: Principal = Depends(requires("finance.view")),
    session: Session = Depends(get_session),
):
    stmt = select(Payment).where(Payment.tenant_id == p.tenant_id)
    if direction:
        stmt = stmt.where(Payment.direction == direction)
    if cheques_only:
        stmt = stmt.where(Payment.method == "cheque")
    stmt = stmt.order_by(Payment.payment_date.desc(), Payment.number.desc()).limit(min(max(limit, 1), 1000))
    return [payment_out(x) for x in session.scalars(stmt)]


@router.post("/payments", status_code=201)
def create_payment(body: PaymentBody, p: Principal = Depends(current_principal), session: Session = Depends(get_session)):
    pay, created = service.record_payment(session, p, service.PaymentIn(**body.model_dump()))
    session.commit()
    return {**payment_out(pay), "duplicate": not created}


@router.post("/payments/{payment_id}/cheque-status")
def cheque_status(
    payment_id: uuid.UUID,
    body: ChequeStatusBody,
    p: Principal = Depends(current_principal),
    session: Session = Depends(get_session),
):
    pay = service.set_cheque_status(session, p, payment_id, body.status)
    session.commit()
    return payment_out(pay)


@router.get("/ledger/{party_type}/{party_id}")
def ledger(
    party_type: str,
    party_id: uuid.UUID,
    p: Principal = Depends(requires("finance.view")),
    session: Session = Depends(get_session),
):
    if party_type not in ("customer", "supplier"):
        raise OrdexaError(ErrorCode.VALIDATION_ERROR, "party_type must be customer or supplier")
    service._party(session, p.tenant_id, party_type, party_id)
    return {
        "balance": str(service.balance(session, p.tenant_id, party_type, party_id)),
        "entries": service.statement(session, p.tenant_id, party_type, party_id),
    }


@router.get("/aging/{party_type}")
def aging(party_type: str, p: Principal = Depends(requires("finance.view")), session: Session = Depends(get_session)):
    if party_type not in ("customer", "supplier"):
        raise OrdexaError(ErrorCode.VALIDATION_ERROR, "party_type must be customer or supplier")
    return service.aging(session, p.tenant_id, party_type, business_today(session, p.tenant_id))


@router.get("/credit-notes")
def list_credit_notes(p: Principal = Depends(requires("invoice.view")), session: Session = Depends(get_session)):
    rows = session.scalars(
        select(CreditNote).where(CreditNote.tenant_id == p.tenant_id).order_by(CreditNote.created_at.desc()).limit(200)
    )
    return [
        {
            "id": str(c.id),
            "number": c.number,
            "customer_id": str(c.customer_id),
            "note_date": c.note_date.isoformat(),
            "reason": c.reason,
            "total": str(c.total),
            "lines": len(c.lines),
        }
        for c in rows
    ]


@router.post("/credit-notes", status_code=201)
def create_credit_note(body: CreditNoteBody, p: Principal = Depends(current_principal), session: Session = Depends(get_session)):
    cn, created = service.create_credit_note(
        session,
        p,
        id=body.id,
        customer_id=body.customer_id,
        warehouse_id=body.warehouse_id,
        invoice_id=body.invoice_id,
        reason=body.reason,
        lines=[service.ReturnLineIn(**ln.model_dump()) for ln in body.lines],
    )
    session.commit()
    return {"id": str(cn.id), "number": cn.number, "total": str(cn.total), "duplicate": not created}


@router.get("/expenses")
def list_expenses(p: Principal = Depends(requires("finance.view")), session: Session = Depends(get_session)):
    rows = session.scalars(
        select(Expense)
        .where(Expense.tenant_id == p.tenant_id)
        .order_by(Expense.expense_date.desc(), Expense.created_at.desc())
        .limit(500)
    )
    return [
        {
            "id": str(e.id),
            "category": e.category,
            "amount": str(e.amount),
            "expense_date": e.expense_date.isoformat(),
            "method": e.method,
            "note": e.note,
        }
        for e in rows
    ]


@router.post("/expenses", status_code=201)
def create_expense(body: ExpenseBody, p: Principal = Depends(current_principal), session: Session = Depends(get_session)):
    e, created = service.record_expense(session, p, **body.model_dump())
    session.commit()
    return {"id": str(e.id), "duplicate": not created}
