"""PostgreSQL-backed job queue. Boring on purpose: no broker to run until volume demands one."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from ordexa.modules.jobs.models import Job
from ordexa.shared.db import utcnow


def enqueue(
    session: Session,
    *,
    kind: str,
    idempotency_key: str,
    payload: dict | None = None,
    queue: str = "default",
    tenant_id: uuid.UUID | None = None,
) -> bool:
    """Returns False if a job with this key already exists (duplicate delivery)."""
    stmt = (
        insert(Job)
        .values(
            id=uuid.uuid4(), kind=kind, queue=queue, tenant_id=tenant_id, payload=payload or {}, idempotency_key=idempotency_key
        )
        .on_conflict_do_nothing(index_elements=["idempotency_key"])
        .returning(Job.id)
    )
    return session.execute(stmt).scalar() is not None


def claim(session: Session, queue: str) -> Job | None:
    """Lock one runnable job; concurrent workers skip it (FOR UPDATE SKIP LOCKED)."""
    job = session.scalar(
        select(Job)
        .where(Job.queue == queue, Job.status == "pending", Job.run_after <= utcnow())
        .order_by(Job.run_after)
        .limit(1)
        .with_for_update(skip_locked=True)
    )
    if job:
        job.status = "running"
        job.attempts += 1
    return job


def run_one(session: Session, queue: str, handlers: dict[str, Callable[[Session, Job], None]]) -> bool:
    """Claim and run a single job in its own transaction. Returns True if a job was processed."""
    job = claim(session, queue)
    if not job:
        session.rollback()
        return False
    session.commit()
    try:
        handler = handlers.get(job.kind)
        if handler is None:
            raise LookupError(f"no handler for job kind '{job.kind}'")
        handler(session, job)
        job.status, job.finished_at, job.last_error = "done", utcnow(), None
        session.commit()
    except Exception as exc:  # noqa: BLE001 - a failing job must never kill the worker loop
        session.rollback()
        job = session.get(Job, job.id)
        job.last_error = f"{type(exc).__name__}: {exc}"[:2000]
        if job.attempts >= job.max_attempts:
            job.status = "failed"  # dead-letter: visible to operators, never retried silently
        else:
            job.status = "pending"
            job.run_after = utcnow() + timedelta(seconds=min(2**job.attempts * 5, 3600))
        session.commit()
    return True
