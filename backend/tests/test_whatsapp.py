"""Phase 3: WhatsApp sign-up bot and verified ordering (all through the simulator - same code as the webhook)."""

import hashlib
import hmac
import json

import pytest

from ordexa.config import get_settings
from ordexa.modules.channels import service as wa


@pytest.fixture
def fixed_otp(monkeypatch):
    monkeypatch.setattr(wa.secrets, "randbelow", lambda n: 123456)


def say(biz, phone, text):
    r = biz.post("/api/v1/channels/whatsapp/simulate", json={"phone": phone, "text": text})
    assert r.status_code == 200, r.text
    return r.json()


def test_registered_shop_verifies_with_code_sent_to_number_on_file(make_biz, fixed_otp):
    biz = make_biz()
    phone = "+923339998877"  # shop 101 has 0300-1110101 on file
    assert "registered" in say(biz, phone, "Assalam o alaikum")["bot_replies"][0]
    say(biz, phone, "1")
    r = say(biz, phone, "101")
    assert "...0101" in r["bot_replies"][0]
    # the code went to the shop's registered number as a template message, not to the sender
    otp_msgs = biz.get("/api/v1/channels/whatsapp/messages?phone=+923001110101").json()
    assert "123456" in otp_msgs[-1]["body"]
    assert say(biz, phone, "000000")["bot_replies"] == ["Code ghalat hai. Dobara likhein."]
    assert "Verify ho gaya" in say(biz, phone, "123456")["bot_replies"][0]
    # now orders go to the AI desk
    r = say(biz, phone, "5 botal calpol aur 2 dabba brufen")
    assert r["ai_order"]["status"] == "draft_ready"
    convo = biz.get(f"/api/v1/channels/whatsapp/messages?phone={phone}").json()
    assert "Order AO-000001 mil gaya" in convo[-1]["body"]
    assert "Rs" in say(biz, phone, "balance")["bot_replies"][0]


def test_number_already_on_file_verifies_immediately(make_biz):
    biz = make_biz()
    phone = "+923001110102"  # equals shop 102's phone on file
    say(biz, phone, "hi")
    say(biz, phone, "1")
    assert "verify ho gaye" in say(biz, phone, "102")["bot_replies"][0]
    assert say(biz, phone, "3 botal calpol")["ai_order"] is not None


def test_unverified_numbers_cannot_order(make_biz):
    biz = make_biz()
    phone = "+923211234567"
    r = say(biz, phone, "10 box panadol bhej do")
    assert r["ai_order"] is None and "registered" in r["bot_replies"][0]
    say(biz, phone, "1")
    r = say(biz, phone, "10 box panadol")  # treated as a (wrong) shop code, never as an order
    assert r["ai_order"] is None
    assert biz.get("/api/v1/ai/orders").json() == []


def test_wrong_codes_lock_the_number(make_biz):
    biz = make_biz()
    phone = "+923211234568"
    say(biz, phone, "hi")
    say(biz, phone, "1")
    for i in range(5):
        say(biz, phone, f"99{i}")
    assert say(biz, phone, "101")["bot_replies"] == []  # locked for an hour


def test_new_shop_signup_needs_staff_approval(make_biz):
    biz = make_biz()
    phone = "+923451112223"
    say(biz, phone, "salam")
    say(biz, phone, "2")
    say(biz, phone, "Noor Medicos, Ichhra, Bilal Ahmed")
    assert say(biz, phone, "10 box panadol")["ai_order"] is None  # still pending
    pending = biz.get("/api/v1/channels/whatsapp/contacts?state=pending").json()
    assert pending[0]["registration_details"] == "Noor Medicos, Ichhra, Bilal Ahmed"
    r = biz.post(
        f"/api/v1/channels/whatsapp/contacts/{pending[0]['id']}/approve",
        json={"new_customer": {"code": "106", "name": "Noor Medicos", "address": "Ichhra"}},
    )
    assert r.json()["state"] == "verified" and r.json()["customer"]["code"] == "106"
    assert say(biz, phone, "3 botal calpol")["ai_order"]["status"] == "draft_ready"
    # staff can revoke ordering for the number
    biz.post(f"/api/v1/channels/whatsapp/contacts/{pending[0]['id']}/unlink")
    assert say(biz, phone, "3 botal calpol")["ai_order"] is None


def test_webhook_to_worker_to_bot(make_biz, monkeypatch):
    """Real path: signed webhook -> channels queue -> worker -> bot reply in the outbox."""
    from fastapi.testclient import TestClient

    from ordexa.apps.worker import HANDLERS
    from ordexa.modules.jobs.service import run_one
    from ordexa.shared.db import get_sessionmaker

    monkeypatch.setenv("ORDEXA_WHATSAPP_APP_SECRET", "s3cret")
    get_settings.cache_clear()
    try:
        biz = make_biz()
        biz.post("/api/v1/channels/whatsapp/numbers", json={"phone_number_id": "PN-1", "display_phone": "+92 42 1234567"})
        from ordexa.apps.webhook import app

        body = json.dumps(
            {
                "object": "whatsapp_business_account",
                "entry": [
                    {
                        "changes": [
                            {
                                "field": "messages",
                                "value": {
                                    "metadata": {"phone_number_id": "PN-1"},
                                    "messages": [
                                        {"id": "wamid.X1", "from": "923001110102", "type": "text", "text": {"body": "hi"}}
                                    ],
                                },
                            }
                        ]
                    }
                ],
            }
        ).encode()
        sig = "sha256=" + hmac.new(b"s3cret", body, hashlib.sha256).hexdigest()
        assert (
            TestClient(app).post("/webhooks/whatsapp", content=body, headers={"x-hub-signature-256": sig}).json()["enqueued"] == 1
        )
        with get_sessionmaker()() as s:
            assert run_one(s, "channels", HANDLERS["channels"])
            assert run_one(s, "outbox", HANDLERS["outbox"])
        msgs = biz.get("/api/v1/channels/whatsapp/messages?phone=+923001110102").json()
        assert [m["direction"] for m in msgs] == ["in", "out"] and msgs[1]["status"] == "simulated"
    finally:
        get_settings.cache_clear()
