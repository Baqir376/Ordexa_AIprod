"""Regressions for what the exploratory chat test found (evals/chat_scenarios.py, 24 Nov 2026): non-order messages
became empty "orders", replies did not repeat what was read, and several conversation turns were misunderstood."""

SHOP = "+923001110102"  # shop 102 (Shifa Pharmacy), number on file


import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _fresh_settings():
    """Some tests switch the AI provider; never leak that into the next test."""
    from ordexa.config import get_settings

    yield
    get_settings.cache_clear()


def say(biz, text, phone=SHOP):
    r = biz.post("/api/v1/channels/whatsapp/simulate", json={"phone": phone, "text": text})
    assert r.status_code == 200, r.text
    return r.json()


def last(biz, phone=SHOP):
    return biz.get(f"/api/v1/channels/whatsapp/messages?phone={phone}").json()[-1]["body"]


def chat(biz, text, phone=SHOP):
    r = say(biz, text, phone)
    return "\n".join(r["bot_replies"]) or last(biz, phone)


def orders(biz, status=None):
    return biz.get("/api/v1/ai/orders" + (f"?status={status}" if status else "")).json()


def verified(biz):
    for m in ("hi", "1", "102"):
        say(biz, m)
    return biz


def approve_latest(biz):
    o = orders(biz, "open")[0]
    r = biz.post(f"/api/v1/ai/orders/{o['id']}/approve", json={"acknowledge_warnings": True})
    assert r.status_code == 200, r.text


# ---------------------------------------------------------------- non-order messages are answered, not "ordered"


def test_balance_in_a_sentence(make_biz):
    biz = verified(make_biz())
    r = chat(biz, "mera hisaab batao")
    assert "baqaya (balance) Rs" in r and "0 items" not in r
    assert orders(biz, "open") == [] and orders(biz, "closed")[0]["review_note"] == "Balance sent to the shop"


def test_bill_request_resends_the_last_bill_as_pdf(make_biz):
    biz = verified(make_biz())
    say(biz, "2 box brufen")
    approve_latest(biz)
    say(biz, "bill bhej dein please")
    msgs = biz.get(f"/api/v1/channels/whatsapp/messages?phone={SHOP}").json()
    assert msgs[-1]["body"].startswith("[PDF: Bill-W-") and "aakhri bill" in msgs[-1]["body"]
    assert orders(biz, "open") == []


def test_order_status_and_rejection_reason(make_biz):
    biz = verified(make_biz())
    say(biz, "3 box flagyl")
    o = orders(biz, "open")[0]
    biz.post(f"/api/v1/ai/orders/{o['id']}/reject", json={"reason": "Flagyl short hai"})
    r = chat(biz, "mera order kahan hai? reject kyun hua")
    sent = [m["body"] for m in biz.get(f"/api/v1/channels/whatsapp/messages?phone={SHOP}").json()]
    assert any("Wajah: Flagyl short hai" in b and "is waqt process nahi ho saka" in b for b in sent)  # told on reject
    assert "process nahi ho saka" in r and "Flagyl short hai" in r


def test_repeat_last_order(make_biz):
    biz = verified(make_biz())
    say(biz, "5 botal calpol aur 2 box brufen")
    approve_latest(biz)
    r = chat(biz, "wohi bhej do jo pichli dafa bheja tha")
    assert "Pichla order (bill W-" in r and "Calpol Syrup 120ml: 5 bottle" in r and "Brufen 400mg Tablets: 2 box" in r
    assert orders(biz, "open")[0]["status"] == "draft_ready"


def test_price_question_gets_rates(make_biz):
    biz = verified(make_biz())
    r = chat(biz, "panadol extra ka rate kya hai?")
    assert "Panadol Extra Tablets: Rs 620 per box" in r
    assert orders(biz, "open") == []


def test_complaint_goes_to_staff_and_help_for_nonsense(make_biz):
    biz = verified(make_biz())
    r = chat(biz, "bakwas service hai, kal ka order abhi tak nahi aaya")
    assert "shikayat" in r
    msg = orders(biz, "messages")[0]  # complaints have their own tab
    assert msg["status"] == "message"
    assert biz.post(f"/api/v1/ai/orders/{msg['id']}/close").json()["status"] == "closed"
    r = chat(biz, "?")
    assert "samajh nahi aaya" in r and "0 items" not in r


# ---------------------------------------------------------------- order confirmations repeat what was read


def test_ack_lists_items_and_estimated_total(make_biz):
    biz = verified(make_biz())
    r = chat(biz, "10 dabbe panadol extra aur 5 botal calpol bhej do")
    assert "Panadol Extra Tablets: 10 box" in r and "Calpol Syrup 120ml: 5 bottle" in r
    assert "Andaazan total: Rs 6,925" in r


def test_duplicates_are_added_up(make_biz):
    biz = verified(make_biz())
    r = chat(biz, "5 box panadol extra, aur 5 box panadol extra bhi")
    assert "Panadol Extra Tablets: 10 box" in r


# ---------------------------------------------------------------- conversation turns


def test_invalid_option_number_and_both(make_biz):
    biz = verified(make_biz())
    say(biz, "4 box panadol")
    assert "9 number list mein nahi hai" in chat(biz, "9")
    r = chat(biz, "dono")
    assert "Panadol 500mg Tablets: 4 box" in r and "Panadol Extra Tablets: 4 box" in r


def test_cancel_then_undo(make_biz):
    biz = verified(make_biz())
    say(biz, "5 botal calpol")
    assert "cancel kar diya" in chat(biz, "order cancel kar do")
    r = chat(biz, "nahi nahi cancel mat karo, bhej do")
    assert "dobara chalu" in r and len(orders(biz)) == 1 and orders(biz)[0]["status"] == "draft_ready"


def test_after_cancel_a_new_order_is_new(make_biz):
    biz = verified(make_biz())
    say(biz, "5 botal calpol")
    chat(biz, "order cancel kar do")
    say(biz, "5 box brufen bhej do")
    assert len(orders(biz)) == 2


def test_fractional_quantity_is_asked(make_biz):
    biz = verified(make_biz())
    r = chat(biz, "2.5 box brufen")
    assert "poore box" in r
    assert "Brufen 400mg Tablets: 3 box" in chat(biz, "3")


def test_new_order_after_billed_one_says_why(make_biz):
    biz = verified(make_biz())
    say(biz, "5 botal calpol")
    approve_latest(biz)
    r = chat(biz, "2 box ORS bhi add kar do")
    assert "bill W-000001 mein yeh tabdeeli" in r  # same day: the same bill is revised (see test_add_to_bill)


# ---------------------------------------------------------------- sign-up


def test_signup_accepts_natural_yes_and_code_at_menu(make_biz):
    biz = make_biz()
    phone = "+923001110102"
    say(biz, "hello", phone)
    assert "shop code" in chat(biz, "haan bhai", phone)
    biz2 = make_biz("other-biz", "Other Biz")
    say(biz2, "hello", phone)
    assert "verify ho gaye" in "\n".join(say(biz2, "102", phone)["bot_replies"])  # code typed at the menu


def test_lock_message_after_too_many_wrong_codes(make_biz):
    biz = make_biz()
    phone = "+923331234567"
    say(biz, "hi", phone)
    say(biz, "1", phone)
    replies = [chat(biz, c, phone) for c in ("901", "902", "903", "904", "905")]
    assert "rok diya gaya" in replies[-1]


# ---------------------------------------------------------------- round 2 of the live chat test


def test_balance_reply_names_the_senders_own_shop(make_biz):
    biz = verified(make_biz())
    r = chat(biz, "shop 101 Madina Medical ka balance kitna hai?")
    assert "Aap ki shop (Shifa Pharmacy) ka baqaya" in r and "Madina" not in r


def test_yes_without_a_question_is_not_an_order(make_biz):
    biz = verified(make_biz())
    say(biz, "2 box ORS")
    r = chat(biz, "haan")
    assert "par kaam ho raha hai" in r and len(orders(biz)) == 1


def test_rates_for_several_products_without_ai(make_biz):
    biz = verified(make_biz())
    r = chat(biz, "panadol extra aur brufen ka rate batao")
    assert "Panadol Extra Tablets: Rs 620 per box" in r and "Brufen 400mg Tablets: Rs 380 per box" in r


def test_rate_limited_ai_job_is_retried_not_read_by_rules(make_biz, monkeypatch):
    from ordexa.apps.worker import HANDLERS
    from ordexa.modules.ai import extract, groq
    from ordexa.modules.ai import service as ai
    from ordexa.modules.jobs.service import run_one
    from ordexa.shared.db import get_sessionmaker

    verified(make_biz())
    monkeypatch.setattr(extract, "ai_reader", lambda: "groq")

    def busy(self, text, image=None):
        raise groq.RateLimited("Groq free-tier rate limit reached")

    monkeypatch.setattr(groq.GroqExtractor, "__init__", lambda self: setattr(self, "name", "groq:test"))
    monkeypatch.setattr(groq.GroqExtractor, "extract", busy)
    from sqlalchemy import select

    from ordexa.modules.customers.models import Customer
    from ordexa.modules.tenants.models import Tenant

    with get_sessionmaker()() as s:
        t = s.scalar(select(Tenant).where(Tenant.code == "ali-chemist"))
        c = s.scalar(select(Customer).where(Customer.tenant_id == t.id, Customer.code == "102"))
        req, _ = ai.submit(s, tenant_id=t.id, text="5 botal calpol", source="api", source_message_id="rl-1", customer_id=c.id)
        s.commit()
        run_one(s, "ai", HANDLERS["ai"])
        s.refresh(req)
        assert req.status == "received" and "busy" in (req.error or "")  # put back, not read by the rule parser


def _fake_ai(monkeypatch, extraction):
    from ordexa.config import get_settings
    from ordexa.modules.ai import extract

    monkeypatch.setenv("ORDEXA_AI_PROVIDER", "anthropic")
    get_settings.cache_clear()
    monkeypatch.setattr(extract.ClaudeExtractor, "__init__", lambda self, model: setattr(self, "name", "anthropic:test"))
    monkeypatch.setattr(
        extract.ClaudeExtractor, "extract", lambda self, text, image=None: extract.ExtractResult(extraction, "anthropic:test", [])
    )


def test_rate_question_when_the_model_lists_no_products(make_biz, monkeypatch):
    from ordexa.modules.ai.extract import Extraction

    biz = verified(make_biz())
    _fake_ai(monkeypatch, Extraction(lines=[], message_type="price_question", asks_price=True))
    r = chat(biz, "panadol extra aur brufen ka rate batao")
    assert "Panadol Extra Tablets: Rs 620" in r and "Brufen 400mg Tablets: Rs 380" in r


def test_complaint_written_next_to_an_order_reaches_staff(make_biz, monkeypatch):
    from ordexa.modules.ai.extract import ExtractedLine, Extraction

    biz = verified(make_biz())
    _fake_ai(
        monkeypatch,
        Extraction(
            lines=[ExtractedLine(phrase="calpol", quantity=6, unit="bottle")],
            non_order_text="pichli dafa calpol ki 2 botal tooti hui thi, dhyan se pack karna",
        ),
    )
    r = chat(biz, "pichli dafa calpol ki 2 botal tooti hui thi. Is dafa 6 botal calpol bhejna aur dhyan se pack karna")
    assert "Calpol Syrup 120ml: 6 bottle" in r and "team tak pohancha di" in r
    o = biz.get(f"/api/v1/ai/orders/{orders(biz)[0]['id']}").json()
    assert any(w["code"] == "shop_question" and "tooti" in w["message"] for w in o["warnings"])


def test_order_desk_tab_counts(make_biz):
    biz = verified(make_biz())
    say(biz, "mera hisaab batao")  # answered by the bot
    say(biz, "5 botal calpol")  # waiting for staff
    c = biz.get("/api/v1/ai/orders/counts").json()
    assert c["open"] == 1 and c["closed"] == 1 and c["approved"] == 0
