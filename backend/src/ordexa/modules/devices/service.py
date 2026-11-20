"""Desktop activation, licence seat checks, and device lookup for sync."""

from __future__ import annotations

import uuid

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ordexa.modules.audit.models import record
from ordexa.modules.devices.models import Device
from ordexa.modules.identity.service import Principal
from ordexa.modules.tenants.models import Tenant, Warehouse
from ordexa.shared.db import lock_master_data, utcnow
from ordexa.shared.errors import ErrorCode, OrdexaError


def activate(session: Session, actor: Principal, *, name: str, warehouse_id: uuid.UUID | None) -> Device:
    """An Administrator activates this PC. It receives the next terminal code (T1, T2, ...), which is also
    its invoice series prefix."""
    actor.require("settings.configure")
    lock_master_data(session, actor.tenant_id)  # serialises terminal-code allocation and seat counting
    tenant = session.get(Tenant, actor.tenant_id)
    if tenant.license_expires_at and tenant.license_expires_at < utcnow():
        raise OrdexaError(ErrorCode.LICENSE_LIMIT, "licence has expired; contact your Ordexa provider")
    active = (
        session.scalar(select(func.count()).select_from(Device).where(Device.tenant_id == tenant.id, Device.is_active.is_(True)))
        or 0
    )
    if active >= tenant.max_devices:
        raise OrdexaError(ErrorCode.LICENSE_LIMIT, f"licence allows {tenant.max_devices} active PCs; deactivate one first")
    if warehouse_id is None:
        warehouse = session.scalar(select(Warehouse).where(Warehouse.tenant_id == tenant.id).order_by(Warehouse.created_at))
    else:
        warehouse = session.get(Warehouse, warehouse_id)
    if not warehouse or warehouse.tenant_id != tenant.id:
        raise OrdexaError(ErrorCode.NOT_FOUND, "warehouse not found")
    total = session.scalar(select(func.count()).select_from(Device).where(Device.tenant_id == tenant.id)) or 0
    device = Device(
        tenant_id=tenant.id,
        name=name.strip()[:100] or "PC",
        terminal_code=f"T{total + 1}",
        warehouse_id=warehouse.id,
        activated_by=actor.user_id,
    )
    session.add(device)
    session.flush()
    record(
        session,
        tenant_id=tenant.id,
        actor_user_id=actor.user_id,
        action="device.activated",
        entity_type="device",
        entity_id=device.id,
        data={"name": device.name, "terminal": device.terminal_code},
    )
    return device


def deactivate(session: Session, actor: Principal, device_id: uuid.UUID) -> None:
    actor.require("settings.configure")
    device = session.get(Device, device_id)
    if not device or device.tenant_id != actor.tenant_id:
        raise OrdexaError(ErrorCode.NOT_FOUND, "device not found")
    device.is_active = False
    record(
        session,
        tenant_id=actor.tenant_id,
        actor_user_id=actor.user_id,
        action="device.deactivated",
        entity_type="device",
        entity_id=device.id,
    )


def device_for(session: Session, principal: Principal, device_id: str | None) -> Device:
    try:
        device = session.get(Device, uuid.UUID(device_id or ""))
    except ValueError:
        device = None
    if not device or device.tenant_id != principal.tenant_id:
        raise OrdexaError(ErrorCode.NOT_FOUND, "device not activated for this business")
    if not device.is_active:
        raise OrdexaError(ErrorCode.PERMISSION_DENIED, "this PC has been deactivated by an administrator")
    return device
