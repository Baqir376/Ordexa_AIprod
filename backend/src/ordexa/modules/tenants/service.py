"""Tenant bootstrap (platform-level) and branding."""

from __future__ import annotations

import re
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from ordexa.modules.audit.models import record
from ordexa.modules.identity.models import ROLE_ADMIN, User
from ordexa.modules.identity.service import Principal
from ordexa.modules.tenants.models import Branch, Tenant, Warehouse
from ordexa.shared.db import lock_master_data
from ordexa.shared.errors import ErrorCode, OrdexaError
from ordexa.shared.security import hash_password

CODE_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,38}[a-z0-9]$")
COLOR_RE = re.compile(r"^#[0-9A-Fa-f]{6}$")


def create_tenant(
    session: Session,
    *,
    code: str,
    name: str,
    admin_username: str,
    admin_password: str,
    admin_full_name: str = "Administrator",
    license_plan: str = "basic",
    max_devices: int = 2,
    license_expires_at: datetime | None = None,
) -> Tenant:
    """Platform operation (CLI / onboarding): business + owner Administrator + main branch + main warehouse."""
    code = code.strip().lower()
    if not CODE_RE.match(code):
        raise OrdexaError(ErrorCode.VALIDATION_ERROR, "business code: 3-40 chars, lowercase letters, digits, '-'")
    if session.scalar(select(Tenant).where(Tenant.code == code)):
        raise OrdexaError(ErrorCode.CONFLICT, f"business code '{code}' already exists")
    if len(admin_password) < 8:
        raise OrdexaError(ErrorCode.VALIDATION_ERROR, "password must be at least 8 characters")
    tenant = Tenant(
        code=code,
        name=name,
        display_name=name,
        license_text=f"Licensed to {name}",
        license_plan=license_plan,
        max_devices=max_devices,
        license_expires_at=license_expires_at,
    )
    session.add(tenant)
    session.flush()
    admin = User(
        tenant_id=tenant.id,
        username=admin_username.strip().lower(),
        full_name=admin_full_name,
        password_hash=hash_password(admin_password),
        role=ROLE_ADMIN,
    )
    session.add(admin)
    session.flush()
    tenant.owner_user_id = admin.id  # SRS IAM-012: ownership is separate from the role
    branch = Branch(tenant_id=tenant.id, code="MAIN", name="Main branch")
    session.add(branch)
    session.flush()
    session.add(Warehouse(tenant_id=tenant.id, branch_id=branch.id, code="MAIN", name="Main store"))
    record(
        session,
        tenant_id=tenant.id,
        actor_user_id=admin.id,
        action="tenant.created",
        entity_type="tenant",
        entity_id=tenant.id,
        data={"code": code, "plan": license_plan},
    )
    session.flush()
    return tenant


def branding(tenant: Tenant) -> dict:
    return {
        "display_name": tenant.display_name,
        "license_text": tenant.license_text,
        "logo_data_url": tenant.logo_data_url,
        "primary_color": tenant.primary_color,
        "address": tenant.address,
        "phone": tenant.phone,
        "powered_by": "Powered by Ordexa ERP",
        "license_plan": tenant.license_plan,
        "currency": tenant.currency,
        "timezone": tenant.timezone,
        "license_expires_at": tenant.license_expires_at.isoformat() if tenant.license_expires_at else None,
    }


def update_branding(session: Session, actor: Principal, changes: dict) -> Tenant:
    actor.require("settings.configure")
    lock_master_data(session, actor.tenant_id)
    tenant = session.get(Tenant, actor.tenant_id)
    if "primary_color" in changes and not COLOR_RE.match(changes["primary_color"] or ""):
        raise OrdexaError(ErrorCode.VALIDATION_ERROR, "primary_color must look like #0F766E")
    logo = changes.get("logo_data_url")
    if logo is not None and (not logo.startswith("data:image/") or len(logo) > 300_000):
        raise OrdexaError(ErrorCode.VALIDATION_ERROR, "logo must be an image data URL under ~200 KB")
    for field in ("display_name", "license_text", "logo_data_url", "primary_color", "address", "phone"):
        if field in changes:
            setattr(tenant, field, changes[field])
    record(
        session,
        tenant_id=tenant.id,
        actor_user_id=actor.user_id,
        action="tenant.branding_updated",
        entity_type="tenant",
        entity_id=tenant.id,
        data={k: v for k, v in changes.items() if k != "logo_data_url"},
    )
    return tenant
