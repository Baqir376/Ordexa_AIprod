"""The AI reads follow-up messages with the open order as context; code checks every action before applying it."""

import pytest

from ordexa.modules.ai import conversation

SHOP = "+923001110102"


def say(biz, text):
    r = biz.post("/api/v1/channels/whatsapp/simulate", json={"phone": SHOP, "text": text})
    assert r.status_code == 200, r.text
    return "\n".join(r.json()["bot_replies"])


def orders(biz):
    return biz.get("/api/v1/ai/orders").json()


def detail(biz, o):
    return biz.get(f"/api/v1/ai/orders/{o['id']}").json()


@pytest.fixture
def plan(monkeypatch):
    """Fake follow-up model: returns whatever plan the test sets (orders themselves are still read by rules)."""
    state = {"plan": None, "seen": None}

    def fake_call(system, user, schema, name):
        state["seen"] = user
        if isinstance(state["plan"], Exception):
            raise state["plan"]
        return state["plan"]

    monkeypatch.setattr(conversation, "ai_reader", lambda: "groq")
    monkeypatch.setattr(conversation, "structured_call", fake_call)
    return state


def act(type_, line=None, product=None, quantity=None, unit=None):
    return {"type": type_, "line": line, "product": product, "search_name": None, "quantity": quantity, "unit": unit}


@pytest.fixture
def shop(make_biz):
    biz = make_biz()
    for m in ("hi", "1", "102"):
        say(biz, m)
    return biz


def test_ai_sees_the_order_and_question_and_changes_quantities(shop, plan):
    say(shop, "5 botal calpol aur 3 box brufen")
    plan["plan"] = {"intent": "change_order", "option": None, "actions": [act("set_quantity", line=2, quantity=6, unit="box")]}
    r = say(shop, "brufen ke 6 kar dein yaar")
    assert '2. shop wrote "brufen"' in plan["seen"] and "<shop_message>" in plan["seen"]
    assert "Brufen 400mg Tablets: 6 box" in r and len(orders(shop)) == 1
    o = detail(shop, orders(shop)[0])
    assert o["shop_updates"][-1]["kind"] == "changed" and "brufen ke 6" in o["shop_updates"][-1]["message"]


def test_ai_cannot_add_a_product_or_quantity_the_shop_never_wrote(shop, plan):
    say(shop, "5 botal calpol")
    plan["plan"] = {
        "intent": "change_order",
        "option": None,
        "actions": [act("add_item", product="augmentin", quantity=10, unit="box"), act("set_quantity", line=1, quantity=50)],
    }
    say(shop, "calpol ke 7 kar dein")  # the AI's actions are not grounded: rules handle it instead
    lines = detail(shop, orders(shop)[0])["lines"]
    assert [ln["product_name"] for ln in lines] == ["Calpol Syrup 120ml"]
    assert float(lines[0]["quantity"]) == 7  # from the shop's own words, not the AI's 50
    trace = detail(shop, orders(shop)[0])["trace"]
    assert any(t["step"] == "followup_action_rejected" for t in trace), [t["step"] for t in trace]


def test_ai_picks_option_by_name(shop, plan):
    say(shop, "3 box panadol")
    plan["plan"] = {"intent": "answer_question", "option": 2, "actions": []}
    assert "update ho gaya" in say(shop, "jo extra wali hai woh")
    assert detail(shop, orders(shop)[0])["lines"][0]["product_name"] == "Panadol Extra Tablets"


def test_question_for_staff_thanks_and_cancel(shop, plan):
    say(shop, "5 botal calpol")
    plan["plan"] = {"intent": "question_for_staff", "option": None, "actions": []}
    assert "team ko bhej diya" in say(shop, "kab tak delivery hogi?")
    o = detail(shop, orders(shop)[0])
    assert any(w["code"] == "shop_question" for w in o["warnings"]) and o["shop_updates"][-1]["kind"] == "message"
    assert "par kaam ho raha hai" in say(shop, "shukriya")  # no AI needed
    plan["plan"] = {"intent": "cancel_order", "option": None, "actions": []}
    assert "cancel kar diya" in say(shop, "rehne dein bhai ab zaroorat nahi")
    assert orders(shop)[0]["status"] == "rejected"
    assert detail(shop, orders(shop)[0])["shop_updates"][-1]["kind"] == "cancelled"


def test_new_order_intent_opens_a_new_order(shop, plan):
    say(shop, "5 botal calpol")
    plan["plan"] = {"intent": "new_order", "option": None, "actions": []}
    say(shop, "aur kal ke liye 2 box brufen alag bill mein")
    assert len(orders(shop)) == 2


def test_ai_failure_falls_back_to_rules(shop, plan):
    say(shop, "10 box panadol, 5 patta brufen")
    plan["plan"] = RuntimeError("Groq free-tier rate limit reached")
    say(shop, "1")
    say(shop, "balkay panadol ke 20 box kr dein aur brufen ke 6")
    o = detail(shop, orders(shop)[0])
    assert len(orders(shop)) == 1 and o["status"] == "draft_ready"


def test_thanks_without_an_open_order_creates_nothing(shop):
    assert "shukriya" in say(shop, "thank you").lower()
    assert orders(shop) == []
