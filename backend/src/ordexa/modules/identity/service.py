"""Authentication, the request principal, and Administrator-managed staff permissions."""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from fastapi import Depends, Header
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ordexa.modules.audit.models import record
from ordexa.modules.identity.models import ROLE_ADMIN, ROLE_STAFF, User, UserPermission
from ordexa.modules.identity.permissions import ALL_PERMISSIONS, GRANTABLE
from ordexa.modules.tenants.models import Tenant
from ordexa.shared.db import get_session
from ordexa.shared.errors import ErrorCode, OrdexaError
from ordexa.shared.security import decode_token, hash_password, issue_token, verify_password


@dataclass(frozen=True)
class Principal:
    user_id: uuid.UUID
    tenant_id: uuid.UUID
    username: str
    full_name: str
    role: str
    permissions: frozenset[str]

    def has(self, permission: str) -> bool:
        return permission in self.permissions

    def require(self, permission: str) -> None:
        if permission not in self.permissions:
            raise OrdexaError(ErrorCode.PERMISSION_DENIED, f"missing permission '{permission}'")


def effective_permissions(session: Session, user: User) -> frozenset[str]:
    if user.role == ROLE_ADMIN:
        return ALL_PERMISSIONS  # SRS IAM-009
    rows = session.scalars(select(UserPermission.permission).where(UserPermission.user_id == user.id))
    return frozenset(rows) & GRANTABLE


def login(session: Session, *, tenant_code: str, username: str, password: str) -> dict:
    tenant = session.scalar(select(Tenant).where(Tenant.code == tenant_code.strip().lower()))
    user = tenant and session.scalar(select(User).where(User.tenant_id == tenant.id, User.username == username.strip().lower()))
    # Same error for unknown tenant, unknown user, wrong password or inactive user.
    if not user or not user.is_active or not verify_password(user.password_hash, password):
        raise OrdexaError(ErrorCode.AUTHENTICATION_REQUIRED, "invalid business code, username or password")
    token, exp = issue_token(user_id=user.id, tenant_id=tenant.id, perm_version=user.perm_version)
    perms = effective_permissions(session, user)
    return {
        "access_token": token,
        "token_type": "bearer",
        "expires_at": exp,
        "user": {
            "id": str(user.id),
            "username": user.username,
            "full_name": user.full_name,
            "role": user.role,
            "permissions": sorted(perms),
        },
        "tenant": {"id": str(tenant.id), "code": tenant.code, "name": tenant.name},
    }


def current_principal(authorization: str | None = Header(default=None), session: Session = Depends(get_session)) -> Principal:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise OrdexaError(ErrorCode.AUTHENTICATION_REQUIRED, "missing bearer token")
    claims = decode_token(authorization.split(" ", 1)[1])
    user = session.get(User, uuid.UUID(claims["sub"]))
    if not user or not user.is_active or str(user.tenant_id) != claims["tid"] or user.perm_version != claims.get("pv"):
        # A permission change, deactivation or role change invalidates older tokens (SRS IAM-013).
        raise OrdexaError(ErrorCode.AUTHENTICATION_REQUIRED, "session is no longer valid; sign in again")
    return Principal(user.id, user.tenant_id, user.username, user.full_name, user.role, effective_permissions(session, user))


def requires(permission: str):
    """FastAPI dependency factory: ``Depends(requires("product.manage"))``."""

    def _dep(principal: Principal = Depends(current_principal)) -> Principal:
        principal.require(permission)
        return principal

    return _dep


def _active_admin_count(session: Session, tenant_id: uuid.UUID) -> int:
    return (
        session.scalar(
            select(func.count())
            .select_from(User)
            .where(User.tenant_id == tenant_id, User.role == ROLE_ADMIN, User.is_active.is_(True))
        )
        or 0
    )


def create_user(
    session: Session,
    actor: Principal,
    *,
    username: str,
    full_name: str,
    password: str,
    role: str = ROLE_STAFF,
    permissions: list[str] | None = None,
) -> User:
    actor.require("user.manage")
    if role not in (ROLE_ADMIN, ROLE_STAFF):
        raise OrdexaError(ErrorCode.VALIDATION_ERROR, "role must be administrator or staff")
    if len(password) < 8:
        raise OrdexaError(ErrorCode.VALIDATION_ERROR, "password must be at least 8 characters")
    uname = username.strip().lower()
    if session.scalar(select(User).where(User.tenant_id == actor.tenant_id, User.username == uname)):
        raise OrdexaError(ErrorCode.CONFLICT, f"username '{uname}' already exists")
    user = User(
        tenant_id=actor.tenant_id, username=uname, full_name=full_name.strip(), password_hash=hash_password(password), role=role
    )
    session.add(user)
    session.flush()
    if role == ROLE_STAFF and permissions:
        _replace_permissions(session, actor, user, permissions)
    record(
        session,
        tenant_id=actor.tenant_id,
        actor_user_id=actor.user_id,
        action="user.created",
        entity_type="user",
        entity_id=user.id,
        data={"username": uname, "role": role},
    )
    return user


def _replace_permissions(session: Session, actor: Principal, user: User, permissions: list[str]) -> dict:
    wanted = set(permissions)
    bad = wanted - GRANTABLE
    if bad:
        raise OrdexaError(ErrorCode.VALIDATION_ERROR, "permissions cannot be granted to staff", {"permissions": sorted(bad)})
    current = set(session.scalars(select(UserPermission.permission).where(UserPermission.user_id == user.id)))
    added, removed = wanted - current, current - wanted
    for p in removed:
        session.delete(session.get(UserPermission, (user.id, p)))
    for p in added:
        session.add(UserPermission(user_id=user.id, permission=p, granted_by=actor.user_id))
    return {"added": sorted(added), "removed": sorted(removed)}


def set_permissions(session: Session, actor: Principal, user_id: uuid.UUID, permissions: list[str]) -> dict:
    actor.require("permission.assign")
    user = _tenant_user(session, actor, user_id)
    if user.role != ROLE_STAFF:
        raise OrdexaError(ErrorCode.VALIDATION_ERROR, "administrators hold all permissions implicitly")
    diff = _replace_permissions(session, actor, user, permissions)
    user.perm_version += 1
    record(
        session,
        tenant_id=actor.tenant_id,
        actor_user_id=actor.user_id,
        action="user.permissions_changed",
        entity_type="user",
        entity_id=user.id,
        data=diff,
    )
    return diff


def set_active(session: Session, actor: Principal, user_id: uuid.UUID, active: bool) -> None:
    actor.require("user.manage")
    user = _tenant_user(session, actor, user_id)
    tenant = session.get(Tenant, actor.tenant_id)
    if not active:
        if tenant.owner_user_id == user.id:
            raise OrdexaError(ErrorCode.CONFLICT, "the business owner cannot be deactivated")
        if user.role == ROLE_ADMIN and _active_admin_count(session, actor.tenant_id) <= 1:
            raise OrdexaError(ErrorCode.CONFLICT, "cannot deactivate the last active administrator")
    user.is_active = active
    user.perm_version += 1
    record(
        session,
        tenant_id=actor.tenant_id,
        actor_user_id=actor.user_id,
        action="user.activated" if active else "user.deactivated",
        entity_type="user",
        entity_id=user.id,
    )


def _tenant_user(session: Session, actor: Principal, user_id: uuid.UUID) -> User:
    user = session.get(User, user_id)
    if not user or user.tenant_id != actor.tenant_id:
        raise OrdexaError(ErrorCode.NOT_FOUND, "user not found")
    return user
