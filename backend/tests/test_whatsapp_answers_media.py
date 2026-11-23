"""Shops answer the bot's questions in the chat; photo orders and voice notes become orders with the file attached."""

import base64

import pytest

from ordexa.config import get_settings
from ordexa.modules.ai import extract
from ordexa.modules.ai.extract import ExtractedLine, Extraction, ExtractResult

SHOP = "+923001110102"  # shop 102's number on file: verified directly


def say(biz, text="", **media):
    r = biz.post("/api/v1/channels/whatsapp/simulate", json={"phone": SHOP, "text": text, **media})
    assert r.status_code == 200, r.text
    return r.json()


def verified(biz):
    for m in ("hi", "1", "102"):
        say(biz, m)


def order(biz, ao):
    return biz.get(f"/api/v1/ai/orders/{ao['id']}").json()


def last_reply(biz):
    return biz.get(f"/api/v1/channels/whatsapp/messages?phone={SHOP}").json()[-1]["body"]


def test_shop_answers_which_product_by_number(make_biz):
    biz = make_biz()
    verified(biz)
    r = say(biz, "3 box panadol aur 2 botal calpol")
    assert r["ai_order"]["status"] == "needs_review"
    assert "konsa chahiye" in last_reply(biz)  # one question at a time
    r = say(biz, "2")  # "2) Panadol Extra Tablets"
    assert r["ai_order"] is None  # not a new order
    assert "update ho gaya" in r["bot_replies"][0]
    o = biz.get("/api/v1/ai/orders").json()
    assert len(o) == 1 and o[0]["status"] == "draft_ready"
    lines = order(biz, o[0])["lines"]
    assert lines[0]["product_name"] == "Panadol Extra Tablets" and lines[0]["resolution"] == "customer"
    # the shop's choice is not saved as approved memory
    assert biz.get("/api/v1/ai/aliases").json() == []


def test_shop_answers_by_name_then_quantity(make_biz):
    biz = make_biz()
    verified(biz)
    say(biz, "3 box panadol, calpol")
    r = say(biz, "extra wali")
    assert "Ab yeh bata dein" in r["bot_replies"][0] and "kitni quantity" in r["bot_replies"][0]
    r = say(biz, "4")
    assert "update ho gaya" in r["bot_replies"][0]
    o = biz.get("/api/v1/ai/orders").json()[0]
    lines = order(biz, o)["lines"]
    assert o["status"] == "draft_ready"
    assert [(ln["product_name"], ln["quantity"]) for ln in lines] == [("Panadol Extra Tablets", 3), ("Calpol Syrup 120ml", 4)]


def test_unclear_answer_repeats_question_and_new_orders_still_work(make_biz):
    biz = make_biz()
    verified(biz)
    say(biz, "3 box panadol")
    r = say(biz, "kal")
    assert r["bot_replies"][0].startswith("Maazrat, samajh nahi aaya")
    r = say(biz, "5 box brufen aur 2 botal calpol")  # a real new order, not an answer
    assert r["ai_order"] is not None
    assert len(biz.get("/api/v1/ai/orders").json()) == 2


def test_wrong_unit_answered_with_quantity(make_biz):
    biz = make_biz()
    verified(biz)
    say(biz, "1 carton panadol extra")
    assert "Kitne box" in last_reply(biz)
    say(biz, "12")
    o = biz.get("/api/v1/ai/orders").json()[0]
    assert o["status"] == "draft_ready" and order(biz, o)["lines"][0]["quantity"] == 12


PNG = base64.b64encode(
    bytes.fromhex(
        "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4890000000d4944415478da6300010000050001"
        "0d0a2db40000000049454e44ae426082"
    )
).decode()


def test_photo_order_without_claude_goes_to_staff_with_the_picture(make_biz):
    biz = make_biz()
    verified(biz)
    r = say(biz, "", media_kind="image", media_base64=PNG, media_type="image/png")
    o = order(biz, r["ai_order"])
    assert o["kind"] == "image" and o["has_attachment"] and o["status"] == "needs_review"
    assert any(w["code"] == "photo_unread" for w in o["warnings"])
    assert "photo mil gaya" in last_reply(biz)
    pic = biz.get(f"/api/v1/ai/orders/{o['id']}/attachment")
    assert pic.status_code == 200 and pic.headers["content-type"] == "image/png" and pic.content.startswith(b"\x89PNG")
    # staff read the photo and add the items
    p = biz.get("/api/v1/products?q=calpol").json()[0]
    o = biz.post(f"/api/v1/ai/orders/{o['id']}/lines", json={"product_id": p["id"], "quantity": "6"}).json()
    assert o["status"] == "draft_ready" and o["warnings"] == []


def test_photo_order_read_by_claude(make_biz, monkeypatch):
    seen = {}

    def fake_extract(self, text, image=None):
        seen["image"] = image
        return ExtractResult(
            Extraction(
                lines=[
                    ExtractedLine(phrase="calpol", quantity=6, unit="bottle"),
                    ExtractedLine(phrase="brufen", quantity=2, unit="box"),
                ]
            ),
            "anthropic:test",
            [{"step": "extract"}],
        )

    monkeypatch.setenv("ORDEXA_AI_PROVIDER", "anthropic")
    get_settings.cache_clear()
    monkeypatch.setattr(extract.ClaudeExtractor, "__init__", lambda self, model: setattr(self, "name", "anthropic:test"))
    monkeypatch.setattr(extract.ClaudeExtractor, "extract", fake_extract)
    try:
        biz = make_biz()
        verified(biz)
        r = say(biz, "aaj ka order", media_kind="image", media_base64=PNG, media_type="image/png")
        o = order(biz, r["ai_order"])
        assert seen["image"][1] == "image/png" and seen["image"][0].startswith(b"\x89PNG")
        assert o["status"] == "draft_ready" and len(o["lines"]) == 2
    finally:
        get_settings.cache_clear()


def test_voice_note_reaches_staff_with_audio(make_biz):
    biz = make_biz()
    verified(biz)
    audio = base64.b64encode(b"OggS" + b"\x00" * 200).decode()
    r = say(biz, "", media_kind="audio", media_base64=audio, media_type="audio/ogg")
    o = order(biz, r["ai_order"])
    assert o["kind"] == "audio" and any(w["code"] == "voice_note" for w in o["warnings"])
    assert "voice message mil gaya" in last_reply(biz)
    clip = biz.get(f"/api/v1/ai/orders/{o['id']}/attachment")
    assert clip.headers["content-type"] == "audio/ogg" and clip.content.startswith(b"OggS")


def test_media_from_unregistered_number_starts_signup(make_biz):
    biz = make_biz()
    r = biz.post(
        "/api/v1/channels/whatsapp/simulate",
        json={"phone": "+923217776665", "media_kind": "image", "media_base64": PNG, "media_type": "image/png"},
    ).json()
    assert r["ai_order"] is None and "registered" in r["bot_replies"][0]


@pytest.mark.parametrize("bad", [{"text": ""}, {"media_kind": "image", "media_base64": "!!!"}])
def test_simulator_validates_input(make_biz, bad):
    biz = make_biz()
    assert biz.post("/api/v1/channels/whatsapp/simulate", json={"phone": SHOP, **bad}).status_code == 422
