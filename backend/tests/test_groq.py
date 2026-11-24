"""Free-tier AI via Groq: text orders, photo orders and voice notes (Groq's HTTP API is faked here; no network)."""

import base64
import json

import pytest

from ordexa.config import get_settings
from ordexa.modules.ai import extract, groq

SHOP = "+923001110102"
PNG = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"\x00" * 64).decode()
OGG = base64.b64encode(b"OggS" + b"\x00" * 200).decode()


def completion(lines):
    content = json.dumps(
        {
            "lines": lines,
            "customer_hint": None,
            "delivery_note": None,
            "language": "roman_ur",
            "contains_instructions": False,
            "non_order_text": None,
        }
    )
    return {
        "choices": [{"finish_reason": "stop", "message": {"content": content}}],
        "usage": {"prompt_tokens": 300, "completion_tokens": 60},
    }


@pytest.fixture
def fake_groq(monkeypatch):
    calls = []
    replies = {}

    def fake_post(path, body, content_type, timeout=60):
        calls.append((path, body, content_type))
        r = replies[path]
        if isinstance(r, Exception):
            raise r
        return r

    monkeypatch.setenv("GROQ_API_KEY", "gsk_test")
    monkeypatch.setenv("ORDEXA_AI_PROVIDER", "auto")
    monkeypatch.setenv("ORDEXA_AI_INLINE", "true")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    get_settings.cache_clear()
    monkeypatch.setattr(groq, "_post", fake_post)
    yield calls, replies
    get_settings.cache_clear()


def say(biz, text="", **media):
    r = biz.post("/api/v1/channels/whatsapp/simulate", json={"phone": SHOP, "text": text, **media})
    assert r.status_code == 200, r.text
    return r.json()


def verified(biz):
    for m in ("hi", "1", "102"):
        say(biz, m)


def test_auto_picks_groq_when_only_a_groq_key_is_set(fake_groq):
    assert extract.ai_reader() == "groq"


def test_text_order_read_by_groq_with_strict_schema(make_biz, fake_groq):
    calls, replies = fake_groq
    replies["/chat/completions"] = completion(
        [
            {"phrase": "panadol extra", "quantity": 10, "unit": "box", "note": None},
            {"phrase": "calpol", "quantity": 2, "unit": "bottle", "note": None},
        ]
    )
    biz = make_biz()
    verified(biz)
    r = say(biz, "bhai das dabba panadol extra aur do botal calpol bhej do")
    o = biz.get(f"/api/v1/ai/orders/{r['ai_order']['id']}").json()
    assert o["status"] == "draft_ready" and o["extractor"] == "groq:openai/gpt-oss-120b"
    body = json.loads(calls[-1][1])
    assert body["model"] == "openai/gpt-oss-120b"
    assert body["response_format"]["type"] == "json_schema" and body["response_format"]["json_schema"]["strict"] is True


def test_photo_order_read_by_groq_vision(make_biz, fake_groq):
    calls, replies = fake_groq
    replies["/chat/completions"] = completion([{"phrase": "brufen", "quantity": 5, "unit": "box", "note": None}])
    biz = make_biz()
    verified(biz)
    r = say(biz, "", media_kind="image", media_base64=PNG, media_type="image/png")
    o = biz.get(f"/api/v1/ai/orders/{r['ai_order']['id']}").json()
    assert o["status"] == "draft_ready" and o["lines"][0]["product_name"] == "Brufen 400mg Tablets"
    body = json.loads(calls[-1][1])
    assert body["model"] == "qwen/qwen3.8-27b"
    assert body["messages"][1]["content"][0]["image_url"]["url"].startswith("data:image/png;base64,")


def test_voice_note_transcribed_then_read(make_biz, fake_groq):
    calls, replies = fake_groq
    replies["/audio/transcriptions"] = {"text": "das dabba panadol extra aur do botal calpol"}
    replies["/chat/completions"] = completion(
        [
            {"phrase": "panadol extra", "quantity": 10, "unit": "box", "note": None},
            {"phrase": "calpol", "quantity": 2, "unit": "bottle", "note": None},
        ]
    )
    biz = make_biz()
    verified(biz)
    r = say(biz, "", media_kind="audio", media_base64=OGG, media_type="audio/ogg")
    o = biz.get(f"/api/v1/ai/orders/{r['ai_order']['id']}").json()
    assert o["transcript"] == "das dabba panadol extra aur do botal calpol"
    assert o["status"] == "draft_ready" and len(o["lines"]) == 2
    path, body, ctype = next(c for c in calls if c[0] == "/audio/transcriptions")
    assert ctype.startswith("multipart/form-data") and b'name="model"\r\n\r\nwhisper-large-v3' in body and b"OggS" in body
    assert o["trace"][0]["step"] == "speech_to_text"


def test_rate_limit_falls_back_to_rules_and_voice_waits_for_staff(make_biz, fake_groq):
    calls, replies = fake_groq
    replies["/chat/completions"] = RuntimeError("Groq free-tier rate limit reached")
    replies["/audio/transcriptions"] = RuntimeError("Groq free-tier rate limit reached")
    biz = make_biz()
    verified(biz)
    o = biz.get(f"/api/v1/ai/orders/{say(biz, '3 botal calpol')['ai_order']['id']}").json()
    assert o["extractor"] == "rules:v1" and o["status"] == "draft_ready"
    assert "rate limit" in o["trace"][0]["error"]
    v = biz.get(
        f"/api/v1/ai/orders/{say(biz, '', media_kind='audio', media_base64=OGG, media_type='audio/ogg')['ai_order']['id']}"
    ).json()
    assert v["status"] == "needs_review" and any(w["code"] == "voice_note" for w in v["warnings"])
    assert "error" in v["trace"][0]


def test_short_rate_limit_waits_and_retries_once(monkeypatch):
    import io
    import urllib.error

    calls = {"n": 0}

    class Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(req, timeout=60):
        calls["n"] += 1
        if calls["n"] == 1:
            raise urllib.error.HTTPError(req.full_url, 429, "Too Many Requests", {"retry-after": "0.01"}, io.BytesIO(b"{}"))
        return Resp(b'{"ok": true}')

    monkeypatch.setenv("GROQ_API_KEY", "gsk_test")
    get_settings.cache_clear()
    monkeypatch.setattr(groq.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(groq.time, "sleep", lambda s: None)
    try:
        assert groq._post("/chat/completions", b"{}", "application/json") == {"ok": True}
        assert calls["n"] == 2
    finally:
        get_settings.cache_clear()
