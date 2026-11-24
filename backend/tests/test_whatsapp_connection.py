"""Settings -> WhatsApp: connect a business's own Meta test/production number from the app (Meta is faked here)."""

import hashlib
import hmac
import io
import json

import pytest
from fastapi.testclient import TestClient

from ordexa.modules.channels import service as wa
from ordexa.modules.channels.models import WhatsappNumber
from ordexa.shared.crypto import decrypt
from ordexa.shared.db import get_sessionmaker
from tests.conftest import PASSWORD

CONN = {
    "phone_number_id": "123456789012345",
    "display_phone": "+1 555 0100",
    "access_token": "EAAG-test-token",
    "app_secret": "meta-app-secret",
    "public_url": "https://abc.trycloudflare.com",
}


class FakeMeta:
    """Captures Graph API calls; replies like Meta would."""

    def __init__(self):
        self.calls = []
        self.fail_with = None

    def __call__(self, req, timeout=20):
        self.calls.append(
            {
                "url": req.full_url,
                "method": req.get_method(),
                "auth": req.get_header("Authorization"),
                "body": json.loads(req.data) if req.data else None,
            }
        )
        if self.fail_with:
            import urllib.error

            raise urllib.error.HTTPError(
                req.full_url, self.fail_with, "err", {}, io.BytesIO(b'{"error":{"message":"Invalid OAuth access token"}}')
            )
        if req.get_method() == "GET":
            body = {"display_phone_number": "+1 555-0100", "verified_name": "Test Number", "id": "123456789012345"}
        else:
            body = {"messages": [{"id": "wamid.OUT1"}]}

        class R(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        return R(json.dumps(body).encode())


@pytest.fixture
def meta(monkeypatch):
    fake = FakeMeta()
    monkeypatch.setattr(wa.urllib.request, "urlopen", fake)
    return fake


def test_connection_is_saved_encrypted_and_secrets_never_returned(make_biz):
    biz = make_biz()
    r = biz.put("/api/v1/channels/whatsapp/connection", json=CONN).json()
    assert r["connected"] and r["has_access_token"] and r["has_app_secret"]
    assert "access_token" not in r and "app_secret" not in r and "EAAG" not in json.dumps(r)
    assert r["verify_token"] and r["webhook_url"] == "https://abc.trycloudflare.com/webhooks/whatsapp"
    with get_sessionmaker()() as s:
        n = s.query(WhatsappNumber).one()
        assert n.access_token_enc != "EAAG-test-token" and decrypt(n.access_token_enc) == "EAAG-test-token"
    # saving again without secrets keeps them
    r = biz.put("/api/v1/channels/whatsapp/connection", json={"phone_number_id": CONN["phone_number_id"]}).json()
    assert r["has_access_token"] and r["has_app_secret"]


def test_connection_rules(make_biz):
    biz = make_biz()
    assert (
        biz.put("/api/v1/channels/whatsapp/connection", json={**CONN, "public_url": "http://insecure.example"}).status_code == 422
    )
    biz.put("/api/v1/channels/whatsapp/connection", json=CONN)
    other = make_biz("other-pharma", "Other")
    assert other.put("/api/v1/channels/whatsapp/connection", json=CONN).status_code == 409
    biz.post(
        "/api/v1/users", json={"username": "clerk", "full_name": "C", "password": PASSWORD, "permissions": ["customer.view"]}
    )
    h = {"Authorization": f"Bearer {biz.login('clerk')}"}
    assert biz.get("/api/v1/channels/whatsapp/connection", headers=h).status_code == 403


def test_test_button_and_send_test(make_biz, meta):
    biz = make_biz()
    biz.put("/api/v1/channels/whatsapp/connection", json=CONN)
    r = biz.post("/api/v1/channels/whatsapp/connection/test").json()
    assert r["ok"] and "Test Number" in r["message"]
    assert meta.calls[-1]["auth"] == "Bearer EAAG-test-token" and "/v26.0/123456789012345" in meta.calls[-1]["url"]
    r = biz.post("/api/v1/channels/whatsapp/connection/send-test", json={"to": "03001234567"}).json()
    assert (
        r["ok"] and meta.calls[-1]["body"]["template"]["name"] == "hello_world" and meta.calls[-1]["body"]["to"] == "923001234567"
    )
    meta.fail_with = 401
    r = biz.post("/api/v1/channels/whatsapp/connection/test").json()
    assert not r["ok"] and "Invalid OAuth" in r["message"]
    assert "Invalid OAuth" in biz.get("/api/v1/channels/whatsapp/connection").json()["last_error"]


def test_webhook_uses_the_saved_verify_token_and_app_secret(make_biz):
    from ordexa.apps.webhook import app

    biz = make_biz()
    conn = biz.put("/api/v1/channels/whatsapp/connection", json=CONN).json()
    c = TestClient(app)
    ok = c.get(
        "/webhooks/whatsapp", params={"hub.mode": "subscribe", "hub.verify_token": conn["verify_token"], "hub.challenge": "777"}
    )
    assert ok.status_code == 200 and ok.text == "777"
    assert (
        c.get(
            "/webhooks/whatsapp", params={"hub.mode": "subscribe", "hub.verify_token": "wrong", "hub.challenge": "1"}
        ).status_code
        == 403
    )
    body = json.dumps(
        {
            "object": "whatsapp_business_account",
            "entry": [
                {
                    "changes": [
                        {
                            "field": "messages",
                            "value": {
                                "metadata": {"phone_number_id": CONN["phone_number_id"]},
                                "messages": [{"id": "wamid.IN1", "from": "923001110102", "type": "text", "text": {"body": "hi"}}],
                            },
                        }
                    ]
                }
            ],
        }
    ).encode()
    good = "sha256=" + hmac.new(b"meta-app-secret", body, hashlib.sha256).hexdigest()
    bad = "sha256=" + hmac.new(b"someone-else", body, hashlib.sha256).hexdigest()
    assert c.post("/webhooks/whatsapp", content=body, headers={"x-hub-signature-256": bad}).status_code == 401
    assert c.post("/webhooks/whatsapp", content=body, headers={"x-hub-signature-256": good}).json()["enqueued"] == 1
    assert c.post("/webhooks/whatsapp", content=b"[1,2]", headers={"x-hub-signature-256": good}).status_code == 400


def test_replies_go_out_through_the_saved_number(make_biz, meta):
    from ordexa.apps.worker import HANDLERS
    from ordexa.modules.jobs.service import enqueue, run_one

    biz = make_biz()
    biz.put("/api/v1/channels/whatsapp/connection", json=CONN)
    with get_sessionmaker()() as s:
        tenant_id = s.query(WhatsappNumber).one().tenant_id
        # an inbound message arrives through the webhook job, the bot replies through the outbox
        enqueue(
            s,
            kind="whatsapp.inbound",
            queue="channels",
            idempotency_key="wa:wamid.IN2",
            payload={
                "wamid": "wamid.IN2",
                "phone_number_id": CONN["phone_number_id"],
                "message": {"id": "wamid.IN2", "from": "923001110102", "type": "text", "text": {"body": "hi"}},
            },
        )
        s.commit()
        assert run_one(s, "channels", HANDLERS["channels"])
        assert run_one(s, "outbox", HANDLERS["outbox"])
    sent = meta.calls[-1]
    assert sent["url"].endswith("/123456789012345/messages") and sent["auth"] == "Bearer EAAG-test-token"
    msgs = biz.get("/api/v1/channels/whatsapp/messages?phone=+923001110102").json()
    assert msgs[-1]["direction"] == "out" and msgs[-1]["status"] == "sent"
    assert biz.get("/api/v1/channels/whatsapp/connection").json()["last_inbound_at"]
    # a rejected token is recorded instead of retried forever
    meta.fail_with = 401
    with get_sessionmaker()() as s:
        wa.queue_text(s, tenant_id, "+923001110102", "second reply", key="t2")
        s.commit()
        run_one(s, "outbox", HANDLERS["outbox"])
    msgs = biz.get("/api/v1/channels/whatsapp/messages?phone=+923001110102").json()
    assert msgs[-1]["status"] == "failed" and "Invalid OAuth" in msgs[-1]["error"]


def test_disconnect_falls_back_to_simulation(make_biz):
    biz = make_biz()
    biz.put("/api/v1/channels/whatsapp/connection", json=CONN)
    r = biz.client.delete("/api/v1/channels/whatsapp/connection", headers=biz.headers).json()
    assert r["connected"] is False and not r["has_access_token"]
