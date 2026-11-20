from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ordexa.modules.identity.service import Principal, current_principal
from ordexa.modules.tenants import service
from ordexa.modules.tenants.models import Tenant, Warehouse
from ordexa.shared.db import get_session

router = APIRouter(prefix="/api/v1", tags=["tenant"])


class BrandingIn(BaseModel):
    display_name: str | None = Field(default=None, max_length=200)
    license_text: str | None = Field(default=None, max_length=200)
    logo_data_url: str | None = None
    primary_color: str | None = None
    address: str | None = Field(default=None, max_length=300)
    phone: str | None = Field(default=None, max_length=30)


@router.get("/tenant")
def current_tenant(p: Principal = Depends(current_principal), session: Session = Depends(get_session)):
    t = session.get(Tenant, p.tenant_id)
    warehouses = session.scalars(select(Warehouse).where(Warehouse.tenant_id == p.tenant_id))
    return {
        "id": str(t.id),
        "code": t.code,
        "name": t.name,
        "branding": service.branding(t),
        "warehouses": [{"id": str(w.id), "code": w.code, "name": w.name} for w in warehouses],
    }


@router.put("/tenant/branding")
def update_branding(body: BrandingIn, p: Principal = Depends(current_principal), session: Session = Depends(get_session)):
    t = service.update_branding(session, p, body.model_dump(exclude_unset=True))
    session.commit()
    return service.branding(t)
