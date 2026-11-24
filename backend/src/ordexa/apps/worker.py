"""Background workers. Normally one process per queue (``ai``, ``channels``, ``outbox``) so a crash or a slow LLM
call in one queue never affects billing or the other queues. For single-process hosting the same loops can run as
threads inside the API (``ORDEXA_EMBEDDED_WORKERS=true``).

Run: ``python -m ordexa.apps.worker --queue ai``
"""

from __future__ import annotations

import argparse
import logging
import signal
import threading
import time
import uuid
from collections.abc import Callable

from sqlalchemy.orm import Session

import ordexa.models  # noqa: F401  (register every table so foreign keys resolve)
from ordexa.config import get_settings
from ordexa.modules.jobs.models import Job
from ordexa.modules.jobs.service import run_one
from ordexa.shared.db import get_sessionmaker

log = logging.getLogger("ordexa.worker")

WORKER_QUEUES = ("ai", "channels", "outbox")


def _ping(session: Session, job: Job) -> None:
    log.info("ping job %s payload=%s", job.id, job.payload)


def _ai_process(session: Session, job: Job) -> None:
    from ordexa.modules.ai.models import AiOrderRequest
    from ordexa.modules.ai.service import process_request

    request_id = uuid.UUID(job.payload["request_id"])
    from ordexa.modules.ai.groq import RateLimited

    try:
        # While retries are left, a free-tier rate limit puts the job back for a later try (10 s, 20 s, 40 s ...).
        process_request(session, request_id, defer_on_rate_limit=job.attempts < job.max_attempts)
    except RateLimited:
        session.rollback()
        req = session.get(AiOrderRequest, request_id)
        if req is not None:
            req.status = "received"
            req.error = "AI is busy (free-tier limit); retrying shortly"
            session.commit()
        raise
    except Exception as exc:
        # Record the failure on the order so staff see it (and can press "reprocess"); the job itself is retried
        # with backoff by run_one, and a retry reprocesses a request in the "failed" state.
        session.rollback()
        req = session.get(AiOrderRequest, request_id)
        if req is not None:
            req.status = "failed"
            req.error = f"{type(exc).__name__}: {exc}"[:2000]
            session.commit()
        raise


def _wa_inbound(session: Session, job: Job) -> None:
    from ordexa.modules.channels.service import inbound_job

    inbound_job(session, job)


def _wa_send(session: Session, job: Job) -> None:
    from ordexa.modules.channels.service import send_job

    send_job(session, job)


HANDLERS = {
    "default": {"ping": _ping},
    "ai": {"ping": _ping, "ai.process_order": _ai_process},
    "outbox": {"ping": _ping, "whatsapp.send": _wa_send},
    "channels": {"ping": _ping, "whatsapp.inbound": _wa_inbound},
}


def run_loop(queue: str, should_stop: Callable[[], bool], once: bool = False) -> None:
    """Claim and run jobs from one queue until ``should_stop()``; never dies on a job or database error."""
    log.info("worker started on queue '%s'", queue)
    poll = get_settings().worker_poll_seconds
    while not should_stop():
        try:
            with get_sessionmaker()() as session:
                worked = run_one(session, queue, HANDLERS[queue])
        except Exception:
            log.exception("worker loop error on queue '%s'", queue)
            worked = False
            time.sleep(5)
        if once and not worked:
            break
        if not worked:
            time.sleep(poll)
    log.info("worker stopped on queue '%s'", queue)


def start_embedded(stop: threading.Event) -> list[threading.Thread]:
    threads = []
    for q in WORKER_QUEUES:
        t = threading.Thread(target=run_loop, args=(q, stop.is_set), name=f"ordexa-worker-{q}", daemon=True)
        t.start()
        threads.append(t)
    return threads


def main() -> None:
    parser = argparse.ArgumentParser(description="Ordexa background worker")
    parser.add_argument("--queue", default="default", choices=sorted(HANDLERS))
    parser.add_argument("--once", action="store_true", help="process available jobs, then exit")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    run_loop(args.queue, stop.is_set, once=args.once)


if __name__ == "__main__":
    main()
