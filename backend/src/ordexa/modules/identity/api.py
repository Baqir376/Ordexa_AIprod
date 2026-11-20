from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ordexa.modules.identity import service
from ordexa.modules.identity.models import User
from ordexa.modules.identity.permissions import PERMISSIONS, PRESETS
from ordexa.modules.identity.service import Principal, current_principal, requires
from ordexa.shared.db import get_session

router = APIRouter(prefix="/api/v1", tags=["identity"])


class LoginIn(BaseModel):
    tenant_code: str = Field(min_length=1, max_length=40)
    username: str = Field(min_length=1, max_length=60)
    password: str = Field(min_length=1, max_length=200)


class UserIn(BaseModel):
    username: str = Field(min_length=2, max_length=60, pattern=r"^[A-Za-z0-9._-]+$")
    full_name: str = Field(min_length=1, max_length=200)
    password: str = Field(min_length=8, max_length=200)
    role: str = "staff"
    permissions: list[str] = []


class PermissionsIn(BaseModel):
    permissions: list[str]


class ActiveIn(BaseModel):
    active: bool


@router.post("/auth/login")
def login(body: LoginIn, session: Session = Depends(get_session)):
    return service.login(session, tenant_code=body.tenant_code, username=body.username, password=body.password)


@router.get("/auth/me")
def me(p: Principal = Depends(current_principal)):
    return {
        "id": str(p.user_id),
        "username": p.username,
        "full_name": p.full_name,
        "role": p.role,
        "tenant_id": str(p.tenant_id),
        "permissions": sorted(p.permissions),
    }


@router.get("/permissions/catalog")
def catalog(_: Principal = Depends(current_principal)):
    return {"modules": PERMISSIONS, "presets": PRESETS}


@router.get("/users")
def list_users(p: Principal = Depends(requires("user.manage")), session: Session = Depends(get_session)):
    users = session.scalars(select(User).where(User.tenant_id == p.tenant_id).order_by(User.username))
    return [
        {"id": str(u.id), "username": u.username, "full_name": u.full_name, "role": u.role, "is_active": u.is_active}
        for u in users
    ]


@router.post("/users", status_code=201)
def create_user(body: UserIn, p: Principal = Depends(current_principal), session: Session = Depends(get_session)):
    user = service.create_user(
        session,
        p,
        username=body.username,
        full_name=body.full_name,
        password=body.password,
        role=body.role,
        permissions=body.permissions,
    )
    session.commit()
    return {"id": str(user.id), "username": user.username}


@router.put("/users/{user_id}/permissions")
def set_permissions(
    user_id: uuid.UUID, body: PermissionsIn, p: Principal = Depends(current_principal), session: Session = Depends(get_session)
):
    diff = service.set_permissions(session, p, user_id, body.permissions)
    session.commit()
    return diff


@router.put("/users/{user_id}/active")
def set_active(
    user_id: uuid.UUID, body: ActiveIn, p: Principal = Depends(current_principal), session: Session = Depends(get_session)
):
    service.set_active(session, p, user_id, body.active)
    session.commit()
    return {"ok": True}
