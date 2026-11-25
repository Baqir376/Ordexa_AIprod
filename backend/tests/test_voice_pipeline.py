"""Voice notes: catalogue-aware recognition, script/language repair, sound-alike correction and staff tools.
Groq's HTTP API is faked (no network). The accuracy work behind these rules is in evals/voice/."""

import base64
import json

import pytest

from ordexa.config import get_settings
from ordexa.modules.ai import groq, phonetic, voice

SHOP = "+923001110102"
OGG = base64.b64encode(b"OggS" + b"\x00" * 200).decode()
CATALOGUE = ["AUGMENTIN 625MG TAB", "RISEK 20MG CAP", "KLARICID 500MG TAB", "MUCAINE SYP", "SURBEX  Z", "FLAGYL 400MG TAB",
             "LEFLOX 250MG TAB", "CEFSPAN 400MG CAP", "VENTOLIN SYP", "MOTILIUM TAB", "DISPRIN TAB",
             "NOVIDAT 250MG TAB"]  # fmt: skip


def heard(text, language="urdu", logprob=-0.2, no_speech=0.01):
    return {"text": text, "language": language, "segments": [{"text": text, "avg_logprob": logprob, "no_speech_prob": no_speech}]}


def completion(lines):
    content = json.dumps(
        {"lines": lines, "customer_hint": None, "delivery_note": None, "language": "ur", "contains_instructions": False,
         "non_order_text": None, "message_type": "order", "asks_price": False}
    )  # fmt: skip
    return {"choices": [{"finish_reason": "stop", "message": {"content": content}}], "usage": {}}


@pytest.fixture
def fake_groq(monkeypatch):
    calls = []
    replies = {}

    def fake_post(path, body, content_type, timeout=60):
        calls.append((path, body))
        r = replies[path]
        r = r.pop(0) if isinstance(r, list) else r
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


def voice_order(biz):
    for m in ("hi", "1", "102"):
        say(biz, m)
    r = say(biz, "", media_kind="audio", media_base64=OGG, media_type="audio/ogg")
    return biz.get(f"/api/v1/ai/orders/{r['ai_order']['id']}").json()


# ------------------------------------------------------------------ sound-alike matching (pure)


@pytest.mark.parametrize(
    "said, brand",
    [
        ("پانچ ڈبے رائزیک بھیج دیں", "risek"),  # Urdu script
        ("दस डिब्बे ऑगमेंटिन", "augmentin"),  # Hindi script
        ("8 boxes of Claracid", "klaricid"),  # misspelt by recognition
        ("لیف لوکس بارہ ڈبے", "leflox"),  # split in two words
        ("سیف اسپین چار سو کے چھ ڈبے", "cefspan"),  # name split in two words
        ("بیس پتے فلیجل", "flagyl"),
    ],
)
def test_heard_names_find_the_catalogue_product(said, brand):
    assert brand in {c["brand"] for c in phonetic.candidates([said], CATALOGUE)}


def test_fillers_and_numbers_never_pass_for_products():
    assert phonetic.candidates(["please send four boxes and ten bottles", "aur das dabbe bhej do"], CATALOGUE) == []


def test_correction_guard():
    assert phonetic.alike("Raizet", "RISEK 20MG CAP")
    assert phonetic.alike("کلیریسڈ", "KLARICID 500MG TAB")
    assert not phonetic.alike("Raizet", "AUGMENTIN 625MG TAB")


# ------------------------------------------------------------------ recognition


def test_catalogue_prompt_and_clean_audio_and_hints(make_biz, fake_groq):
    calls, replies = fake_groq
    replies["/audio/transcriptions"] = heard("پانچ ڈبے رائزیک اور دس ڈبے آگمنٹن")
    replies["/chat/completions"] = completion(
        [{"phrase": "رائزیک", "search_name": "risek 20mg capsules", "quantity": 5, "unit": "box", "note": None},
         {"phrase": "آگمنٹن", "search_name": "augmentin", "quantity": 10, "unit": "box", "note": None}]
    )  # fmt: skip
    o = voice_order(make_biz())
    stt = next(b for p, b in calls if p == "/audio/transcriptions")
    assert b"Products: " in stt and b"Augmentin" in stt and b"Risek" in stt  # this business's own catalogue
    assert b'name="response_format"\r\n\r\nverbose_json' in stt
    reader = json.loads(next(b for p, b in calls if p == "/chat/completions"))["messages"][1]["content"]
    assert "[Voice note" in reader and "Risek 20mg Capsules" in reader  # sound-alike hint for the reader
    assert [ln["product_name"] for ln in o["lines"]] == ["Risek 20mg Capsules", "Augmentin 625mg Tablets"]
    assert o["voice"]["clarity"] == "clear" and o["voice"]["language"] == "ur"
    assert any(h["product"] == "Risek 20mg Capsules" for h in o["voice"]["hints"])


def test_misheard_latin_name_is_corrected_only_when_it_sounds_alike(make_biz, fake_groq):
    calls, replies = fake_groq
    replies["/audio/transcriptions"] = heard("10 boxes of Augmentin and 5 boxes of Raizet", language="english")
    replies["/chat/completions"] = completion(
        [{"phrase": "Raizet", "search_name": "risek 20mg capsules", "quantity": 5, "unit": "box", "note": None},
         {"phrase": "Raizet", "search_name": "calpol syrup", "quantity": 1, "unit": "bottle", "note": None}]
    )  # fmt: skip
    o = voice_order(make_biz())
    assert o["lines"][0]["product_name"] == "Risek 20mg Capsules"
    assert o["lines"][1]["product_name"] != "Calpol Syrup 120ml"  # the model cannot swap in an unrelated medicine


def test_hindi_script_is_read_in_latin_letters(make_biz, fake_groq):
    calls, replies = fake_groq
    replies["/audio/transcriptions"] = heard("दस डिब्बे ऑगमेंटिन भेज दो", language="hindi")
    replies["/chat/completions"] = completion(
        [{"phrase": "augmentin", "search_name": None, "quantity": 10, "unit": "box", "note": None}]
    )
    o = voice_order(make_biz())
    assert sum(1 for p, _ in calls if p == "/audio/transcriptions") == 1
    assert o["voice"]["text"].startswith("das dibbe") and o["voice"]["alt"].startswith("दस")
    assert "das dibbe" in json.loads(next(b for p, b in calls if p == "/chat/completions"))["messages"][1]["content"]


def test_wrong_language_is_heard_again_as_urdu(make_biz, fake_groq):
    calls, replies = fake_groq
    replies["/audio/transcriptions"] = [heard("IK DABA NEK SIM ČALIS", language="slovenian"), heard("ایک ڈبہ نیکسم")]
    replies["/chat/completions"] = completion([])
    o = voice_order(make_biz())
    stt = [b for p, b in calls if p == "/audio/transcriptions"]
    assert len(stt) == 2 and b'name="language"\r\n\r\nur' in stt[1] and b'name="language"' not in stt[0]
    assert o["voice"]["text"] == "ایک ڈبہ نیکسم" and o["voice"]["alt"] == "IK DABA NEK SIM ČALIS"


def test_hard_to_hear_note_is_flagged_for_staff(make_biz, fake_groq):
    calls, replies = fake_groq
    replies["/audio/transcriptions"] = heard("das dabbe augmentin", language="urdu", logprob=-1.2)
    replies["/chat/completions"] = completion(
        [{"phrase": "augmentin", "search_name": None, "quantity": 10, "unit": "box", "note": None}]
    )
    o = voice_order(make_biz())
    assert o["voice"]["clarity"] == "unclear"
    assert any(w["code"] == "voice_unclear" for w in o["warnings"])


# ------------------------------------------------------------------ staff tools


def test_staff_fix_what_was_said_and_listen_again(make_biz, fake_groq):
    calls, replies = fake_groq
    replies["/audio/transcriptions"] = [heard("das dabbe ...", logprob=-1.3), heard("das dabbe augmentin aur 5 risek")]
    replies["/chat/completions"] = [
        completion([]),
        completion([{"phrase": "flagyl", "search_name": None, "quantity": 20, "unit": "strip", "note": None}]),
        completion([{"phrase": "augmentin", "search_name": None, "quantity": 10, "unit": "box", "note": None}]),
    ]
    biz = make_biz()
    o = voice_order(biz)
    # staff listened and typed what the shop said: no new recognition call, the order is read from the text
    r = biz.post(f"/api/v1/ai/orders/{o['id']}/transcript", json={"text": "bees pattay flagyl"})
    assert r.status_code == 200, r.text
    o = r.json()
    assert o["transcript"] == "bees pattay flagyl" and o["voice"]["edited"] and o["voice"]["clarity"] == "clear"
    assert o["lines"][0]["product_name"] == "Flagyl 400mg Tablets"
    assert sum(1 for p, _ in calls if p == "/audio/transcriptions") == 1
    # listen again: recognition runs once more
    o = biz.post(f"/api/v1/ai/orders/{o['id']}/listen-again").json()
    assert o["transcript"] == "das dabbe augmentin aur 5 risek"
    assert sum(1 for p, _ in calls if p == "/audio/transcriptions") == 2


def test_clean_audio_falls_back_without_ffmpeg(monkeypatch):
    monkeypatch.setattr(voice.shutil, "which", lambda _: None)
    assert voice.clean_audio(b"OggS123", "audio/ogg") == (b"OggS123", "audio/ogg", False)


def test_live_regression_short_brands_and_misheard_starts_and_endings():
    """From a live noisy Urdu note: "آرمنٹن" is Augmentin, "رائزت" is Risek, and never ORS (short names need an
    exact hearing, or the reader would be steered to a wrong product)."""
    names = ["Augmentin 625mg Tablets", "Risek 20mg Capsules", "ORS Sachet", "Calpol Syrup 120ml"]
    got = {c["products"][0] for c in phonetic.candidates(["دس ڈبے آرمنٹن اور پانچ ڈبے رائزت بھیج دیں"], names)}
    assert got == {"Augmentin 625mg Tablets", "Risek 20mg Capsules"}
    assert not phonetic.alike("رائزت", "ORS Sachet") and phonetic.alike("ORS", "ORS Sachet")


def test_several_lookalikes_are_asked_not_guessed(make_biz, fake_groq):
    """ "Rizet" sounds like Risek and like Rovista: the order must not silently pick one."""
    calls, replies = fake_groq
    biz = make_biz()
    biz.post("/api/v1/products", json={"code": "RV1", "name": "Rovista 20mg Tablets", "sale_unit": "box", "sale_price": "300"})
    replies["/audio/transcriptions"] = heard("5 dabbe Rizet bhej do", language="urdu")
    replies["/chat/completions"] = completion(
        [{"phrase": "Rizet", "search_name": "rovista", "quantity": 5, "unit": "box", "note": None}]
    )
    o = voice_order(biz)
    line = o["lines"][0]
    assert line["product_id"] is None and "ambiguous" in line["issues"]
    names = [c["name"] for c in line["candidates"]]
    assert "Risek 20mg Capsules" in names and "Rovista 20mg Tablets" in names


def test_misheard_name_found_even_when_the_reader_gives_no_spelling(make_biz, fake_groq):
    calls, replies = fake_groq
    replies["/audio/transcriptions"] = heard("12 dabbe Augmantin", language="urdu")
    replies["/chat/completions"] = completion(
        [{"phrase": "Augmantin", "search_name": None, "quantity": 12, "unit": "box", "note": None}]
    )
    o = voice_order(make_biz())
    assert o["lines"][0]["product_name"] == "Augmentin 625mg Tablets"


def test_filler_words_are_never_corrected_into_products():
    assert not phonetic.alike("Wally", "VILOC 500MG TAB")
    assert phonetic.candidates(["Klaricid 500 wali 3 dabbi"], ["VILOC 500MG TAB", "KLARICID 500MG TAB"])[0]["brand"] == "klaricid"
    assert "viloc" not in {c["brand"] for c in phonetic.candidates(["Claricid 500 Wally 3 dubbi"], ["VILOC 500MG TAB"])}


def test_exact_name_beats_a_lookalike_with_the_same_outline():
    got = phonetic.candidates(["Amoxil 500 ke 3 dabbe"], ["MIXEL 100ML SYP", "AMOXIL 500MG CAP"])
    assert [c["brand"] for c in got] == ["amoxil"]
