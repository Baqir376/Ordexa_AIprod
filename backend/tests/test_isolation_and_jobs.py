import hashlib
import hmac
import json
import uuid

from fastapi.testclient import TestClient

from ordexa.config import get_settings
from ordexa.modules.jobs.models import Job
from ordexa.modules.jobs.service import enqueue, run_one
from ordexa.shared.db import get_sessionmaker

# ------------------------------------------------------------------ tenant isolation (SRS AC-001)


def test_tenant_cannot_see_or_use_another_tenants_data(make_biz):
    a = make_biz("ali-chemist", "Ali Chemist")
    b = make_biz("noor-pharma", "Noor Pharma")
    a_dev, b_dev = a.activate("A-PC"), b.activate("B-PC")
    a_data = a.get("/api/v1/sync/pull", headers=a.dev_headers(a_dev)).json()
    a_product_ids = {p["id"] for p in a_data["products"]}
    b_data = b.get("/api/v1/sync/pull", headers=b.dev_headers(b_dev)).json()
    assert not a_product_ids & {p["id"] for p in b_data["products"]}

    # B cannot sync through A's device id.
    assert b.get("/api/v1/sync/pull", headers={**b.headers, "X-Device-Id": a_dev["id"]}).status_code == 404

    # B cannot bill A's product.
    p = a_data["products"][0]
    inv = {
        "id": str(uuid.uuid4()),
        "number": "T1-000001",
        "issued_at": "2026-11-17T10:00:00+05:00",
        "lines": [{"line_no": 1, "product_id": p["id"], "quantity": "1", "unit_price": p["sale_price"]}],
    }
    r = b.post("/api/v1/sync/push", json={"invoices": [inv]}, headers=b.dev_headers(b_dev)).json()
    assert r["results"][0]["status"] == "rejected"

    # B's invoice list never contains A's invoices, and B cannot see A's users.
    assert b.get("/api/v1/invoices").json() == []
    assert {u["username"] for u in b.get("/api/v1/users").json()} == {"admin"}


# ------------------------------------------------------------------ job queue


def test_enqueue_is_idempotent_and_failed_jobs_back_off_then_dead_letter():
    calls = []

    def boom(session, job):
        calls.append(job.id)
        raise RuntimeError("provider down")

    with get_sessionmaker()() as s:
        assert enqueue(s, kind="x", idempotency_key="k1", queue="ai") is True
        assert enqueue(s, kind="x", idempotency_key="k1", queue="ai") is False
        s.commit()
        assert run_one(s, "ai", {"x": boom}) is True
        job = s.query(Job).one()
        assert job.status == "pending" and job.attempts == 1 and "provider down" in job.last_error
        job.max_attempts, job.run_after = 1, job.created_at  # make it runnable and final
        s.commit()
        run_one(s, "ai", {"x": boom})
        assert s.query(Job).one().status == "failed"


def test_job_success():
    with get_sessionmaker()() as s:
        enqueue(s, kind="ping", idempotency_key="p1", queue="default", payload={"n": 1})
        s.commit()
        assert run_one(s, "default", {"ping": lambda s, j: None})
        assert s.query(Job).one().status == "done"
        assert run_one(s, "default", {}) is False


# ------------------------------------------------------------------ webhook receiver


def test_webhook_verifies_signature_and_dedups(monkeypatch):
    monkeypatch.setenv("ORDEXA_WHATSAPP_APP_SECRET", "app-secret")
    get_settings.cache_clear()
    try:
        from ordexa.apps.webhook import app

        c = TestClient(app)
        body = json.dumps(
            {
                "object": "whatsapp_business_account",
                "entry": [
                    {
                        "changes": [
                            {
                                "field": "messages",
                                "value": {
                                    "metadata": {"phone_number_id": "PN1"},
                                    "messages": [
                                        {"id": "wamid.1", "from": "923001110101", "type": "text", "text": {"body": "10 panadol"}}
                                    ],
                                },
                            }
                        ]
                    }
                ],
            }
        ).encode()
        sig = "sha256=" + hmac.new(b"app-secret", body, hashlib.sha256).hexdigest()
        assert c.post("/webhooks/whatsapp", content=body, headers={"x-hub-signature-256": "sha256=bad"}).status_code == 401
        r1 = c.post("/webhooks/whatsapp", content=body, headers={"x-hub-signature-256": sig})
        r2 = c.post("/webhooks/whatsapp", content=body, headers={"x-hub-signature-256": sig})
        assert r1.json()["enqueued"] == 1 and r2.json()["enqueued"] == 0
    finally:
        get_settings.cache_clear()
