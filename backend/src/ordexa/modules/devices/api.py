from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ordexa.modules.devices import service
from ordexa.modules.devices.models import Device
from ordexa.modules.identity.service import Principal, current_principal, requires
from ordexa.shared.db import get_session

router = APIRouter(prefix="/api/v1/devices", tags=["devices"])


class ActivateIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    warehouse_id: uuid.UUID | None = None


def _out(d: Device) -> dict:
    return {
        "id": str(d.id),
        "name": d.name,
        "terminal_code": d.terminal_code,
        "invoice_prefix": f"{d.terminal_code}-",
        "warehouse_id": str(d.warehouse_id),
        "is_active": d.is_active,
        "last_sync_at": d.last_sync_at.isoformat() if d.last_sync_at else None,
    }


@router.post("/activate", status_code=201)
def activate(body: ActivateIn, p: Principal = Depends(current_principal), session: Session = Depends(get_session)):
    d = service.activate(session, p, name=body.name, warehouse_id=body.warehouse_id)
    session.commit()
    return _out(d)


@router.get("")
def list_devices(p: Principal = Depends(requires("settings.configure")), session: Session = Depends(get_session)):
    return [
        _out(d) for d in session.scalars(select(Device).where(Device.tenant_id == p.tenant_id).order_by(Device.terminal_code))
    ]


@router.post("/{device_id}/deactivate")
def deactivate(device_id: uuid.UUID, p: Principal = Depends(current_principal), session: Session = Depends(get_session)):
    service.deactivate(session, p, device_id)
    session.commit()
    return {"ok": True}
