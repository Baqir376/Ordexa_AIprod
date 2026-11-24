"""Business dates in the tenant's own time zone (Asia/Karachi by default).

Servers run in UTC. Between 00:00 and 05:00 in Pakistan the UTC date is still "yesterday", so every business date
(ledger dates, report ranges, ageing, expiry) must come from here, never from ``date.today()``.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from ordexa.modules.tenants.models import Tenant


def tenant_tz(session: Session, tenant_id: uuid.UUID) -> ZoneInfo:
    tenant = session.get(Tenant, tenant_id)
    return ZoneInfo(tenant.timezone if tenant and tenant.timezone else "Asia/Karachi")


def business_today(session: Session, tenant_id: uuid.UUID) -> date:
    return datetime.now(tenant_tz(session, tenant_id)).date()


def local_date(session: Session, tenant_id: uuid.UUID, moment: datetime) -> date:
    """The business date of a timestamp (naive timestamps are treated as UTC)."""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(tenant_tz(session, tenant_id)).date()
