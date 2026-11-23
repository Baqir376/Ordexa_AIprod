"""Deployment-facing behaviour: hosted DB URLs, single-process mode, login throttling, failure states."""

import threading
import time
import uuid

import pytest

from ordexa.config import Settings, get_settings
from tests.conftest import PASSWORD


@pytest.mark.parametrize(
    "given, expected",
    [
        ("postgres://u:p@host/db?sslmode=require", "postgresql+psycopg://u:p@host/db?sslmode=require"),
        ("postgresql://u:p@host:5432/db", "postgresql+psycopg://u:p@host:5432/db"),
        ("postgresql+psycopg://u:p@h/db", "postgresql+psycopg://u:p@h/db"),
    ],
)
def test_hosted_database_urls_are_normalised(given, expected):
    assert Settings(database_url=given).database_url == expected


def test_production_requires_a_real_secret(monkeypatch):
    monkeypatch.setenv("ORDEXA_ENVIRONMENT", "production")
    monkeypatch.setenv("ORDEXA_JWT_SECRET", "short")
    get_settings.cache_clear()
    try:
        with pytest.raises(RuntimeError):
            get_settings()
    finally:
        monkeypatch.delenv("ORDEXA_ENVIRONMENT")
        monkeypatch.delenv("ORDEXA_JWT_SECRET")
        get_settings.cache_clear()


def test_repeated_wrong_passwords_are_throttled(client, make_biz):
    make_biz()
    bad = {"tenant_code": "ali-chemist", "username": "admin", "password": "nope-nope"}
    for _ in range(10):
        assert client.post("/api/v1/auth/login", json=bad).status_code == 401
    r = client.post("/api/v1/auth/login", json={**bad, "password": PASSWORD})
    assert r.status_code == 429 and r.json()["error"]["code"] == "RATE_LIMITED"
    # a different user is not affected
    assert client.post("/api/v1/auth/login", json={**bad, "username": "someone"}).status_code == 401


def test_webhook_is_also_served_by_the_api(client, monkeypatch):
    monkeypatch.setenv("ORDEXA_WHATSAPP_VERIFY_TOKEN", "vt")
    get_settings.cache_clear()
    try:
        r = client.get("/webhooks/whatsapp", params={"hub.mode": "subscribe", "hub.verify_token": "vt", "hub.challenge": "42"})
        assert r.status_code == 200 and r.text == "42"
        assert client.post("/webhooks/whatsapp", content=b"{}", headers={"x-hub-signature-256": "sha256=x"}).status_code == 401
    finally:
        get_settings.cache_clear()


def test_embedded_workers_process_ai_orders(make_biz):
    """Single-process hosting: worker threads inside the API pick up queued AI orders."""
    from ordexa.apps.worker import start_embedded

    biz = make_biz()
    customer = biz.get("/api/v1/customers").json()[0]
    o = biz.post(
        "/api/v1/ai/orders", json={"id": str(uuid.uuid4()), "customer_id": customer["id"], "text": "3 botal calpol"}
    ).json()
    assert o["status"] == "received"
    stop = threading.Event()
    threads = start_embedded(stop)
    try:
        for _ in range(50):
            if biz.get(f"/api/v1/ai/orders/{o['id']}").json()["status"] != "received":
                break
            time.sleep(0.2)
    finally:
        stop.set()
        for t in threads:
            t.join(timeout=5)
    assert biz.get(f"/api/v1/ai/orders/{o['id']}").json()["status"] == "draft_ready"


def test_ai_job_failure_is_visible_and_recoverable(make_biz, monkeypatch):
    from ordexa.apps.worker import HANDLERS
    from ordexa.modules.ai import service as ai
    from ordexa.modules.jobs.service import run_one
    from ordexa.shared.db import get_sessionmaker

    biz = make_biz()
    customer = biz.get("/api/v1/customers").json()[0]
    o = biz.post(
        "/api/v1/ai/orders", json={"id": str(uuid.uuid4()), "customer_id": customer["id"], "text": "3 botal calpol"}
    ).json()
    real = ai.extract_order
    monkeypatch.setattr(ai, "extract_order", lambda text: (_ for _ in ()).throw(RuntimeError("boom")))
    with get_sessionmaker()() as s:
        run_one(s, "ai", HANDLERS["ai"])
    failed = biz.get(f"/api/v1/ai/orders/{o['id']}").json()
    assert failed["status"] == "failed" and "boom" in failed["error"]
    monkeypatch.setattr(ai, "extract_order", real)
    assert biz.post(f"/api/v1/ai/orders/{o['id']}/reprocess").json()["status"] == "draft_ready"
