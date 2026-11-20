"""Background worker process. One process per queue (``ai``, ``outbox``, ``default``) so a crash or a
slow LLM call in one queue never affects billing or the other queues.

Run: ``python -m ordexa.apps.worker --queue ai``
"""

from __future__ import annotations

import argparse
import logging
import signal
import time
import uuid

from sqlalchemy.orm import Session

import ordexa.models  # noqa: F401  (register every table so foreign keys resolve)
from ordexa.config import get_settings
from ordexa.modules.jobs.models import Job
from ordexa.modules.jobs.service import run_one
from ordexa.shared.db import get_sessionmaker

log = logging.getLogger("ordexa.worker")


def _ping(session: Session, job: Job) -> None:
    log.info("ping job %s payload=%s", job.id, job.payload)


def _ai_process(session: Session, job: Job) -> None:
    from ordexa.modules.ai.service import process_request

    process_request(session, uuid.UUID(job.payload["request_id"]))


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


def main() -> None:
    parser = argparse.ArgumentParser(description="Ordexa background worker")
    parser.add_argument("--queue", default="default", choices=sorted(HANDLERS))
    parser.add_argument("--once", action="store_true", help="process available jobs, then exit")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    stop = False

    def _stop(*_):
        nonlocal stop
        stop = True

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    log.info("worker started on queue '%s'", args.queue)
    poll = get_settings().worker_poll_seconds
    while not stop:
        try:
            with get_sessionmaker()() as session:
                worked = run_one(session, args.queue, HANDLERS[args.queue])
        except Exception:
            log.exception("worker loop error")
            worked = False
            time.sleep(5)
        if args.once and not worked:
            break
        if not worked:
            time.sleep(poll)
    log.info("worker stopped")


if __name__ == "__main__":
    main()
