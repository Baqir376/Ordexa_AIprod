"""Voice notes are often transcribed in Urdu script; products must still be found in the English-letter catalogue,
and the AI's English spelling must sound like what the shop said (it cannot swap in another medicine)."""

import pytest

from ordexa.config import get_settings
from ordexa.modules.ai import extract
from ordexa.modules.ai.extract import ExtractedLine, Extraction, ExtractResult, sounds_alike, urdu_to_latin

SHOP = "+923001110102"


def say(biz, text):
    r = biz.post("/api/v1/channels/whatsapp/simulate", json={"phone": SHOP, "text": text})
    assert r.status_code == 200, r.text
    return r.json()


def last_reply(biz):
    return biz.get(f"/api/v1/channels/whatsapp/messages?phone={SHOP}").json()[-1]["body"]


@pytest.fixture
def ai(monkeypatch):
    """Fake model: returns the lines set by the test."""
    state = {"lines": []}

    def fake_extract(self, text, image=None):
        return ExtractResult(Extraction(lines=state["lines"], language="ur"), "anthropic:test", [{"step": "extract"}])

    monkeypatch.setenv("ORDEXA_AI_PROVIDER", "anthropic")
    get_settings.cache_clear()
    monkeypatch.setattr(extract.ClaudeExtractor, "__init__", lambda self, model: setattr(self, "name", "anthropic:test"))
    monkeypatch.setattr(extract.ClaudeExtractor, "extract", fake_extract)
    yield state
    get_settings.cache_clear()


def verified(biz):
    for m in ("hi", "1", "102"):
        say(biz, m)


def test_sound_alike_guard():
    assert urdu_to_latin("بروفن") == "brofn"
    assert sounds_alike("پیرسیٹا مول", "paracetamol")
    assert sounds_alike("پینڈر ایکسٹرا", "panadol extra")
    assert not sounds_alike("پیرسیٹا مول", "brufen")
    assert not sounds_alike("پینڈر ایکسٹرا", "augmentin")


def test_urdu_script_product_is_found_through_its_english_spelling(make_biz, ai):
    biz = make_biz()
    verified(biz)
    ai["lines"] = [ExtractedLine(phrase="پیناڈول ایکسٹرا", search_name="panadol extra", quantity=2, unit="box")]
    r = say(biz, "دو ڈبے پیناڈول ایکسٹرا")
    o = biz.get(f"/api/v1/ai/orders/{r['ai_order']['id']}").json()
    assert o["status"] == "draft_ready"
    assert o["lines"][0]["product_name"] == "Panadol Extra Tablets" and o["lines"][0]["read_as"] == "panadol extra"


def test_generic_name_in_urdu_asks_which_brand_with_numbered_options(make_biz, ai):
    biz = make_biz()
    verified(biz)
    ai["lines"] = [ExtractedLine(phrase="پیرسیٹا مول", search_name="paracetamol", quantity=10, unit="strip")]
    say(biz, "دس پتے پیرسیٹا مول")
    reply = last_reply(biz)
    assert "(paracetamol)" in reply and "Number likh dein" in reply and "1) Panadol" in reply
    assert "nahi mila" not in reply
    assert "update ho gaya" in say(biz, "1")["bot_replies"][0]


def test_model_cannot_swap_in_a_different_medicine(make_biz, ai):
    biz = make_biz()
    verified(biz)
    ai["lines"] = [ExtractedLine(phrase="پیرسیٹا مول", search_name="augmentin", quantity=1, unit="box")]
    r = say(biz, "ایک ڈبہ پیرسیٹا مول")
    o = biz.get(f"/api/v1/ai/orders/{r['ai_order']['id']}").json()
    assert all(ln["product_name"] != "Augmentin 625mg Tablets" for ln in o["lines"])
    assert o["lines"][0]["read_as"] is None
    assert any(t.get("step") == "search_name_rejected" for t in o["trace"])


def test_unknown_product_reply_is_polite_and_gives_an_example(make_biz, ai):
    biz = make_biz()
    verified(biz)
    ai["lines"] = [ExtractedLine(phrase="zyxqwv", quantity=3, unit="box")]
    say(biz, "3 box zyxqwv")
    reply = last_reply(biz)
    assert "Maazrat" in reply and "English mein likh dein" in reply and "Panadol Extra" in reply
