"""Chats view (every conversation, staff replies) and clearing junk from the order desk."""

SHOP = "+923001110102"


def say(biz, text, phone=SHOP):
    r = biz.post("/api/v1/channels/whatsapp/simulate", json={"phone": phone, "text": text})
    assert r.status_code == 200, r.text
    return r.json()


def verified(biz):
    for m in ("hi", "1", "102"):
        say(biz, m)
    return biz


def test_conversations_list_and_staff_reply(make_biz):
    biz = verified(make_biz())
    say(biz, "5 botal calpol")
    say(biz, "hello", phone="+923339990001")  # a stranger
    convs = biz.get("/api/v1/channels/whatsapp/conversations").json()
    by_phone = {c["phone"]: c for c in convs}
    assert by_phone[SHOP]["name"] == "Shifa Pharmacy" and by_phone[SHOP]["customer_code"] == "102"
    assert by_phone[SHOP]["in_last_24h"] == 4 and "+923339990001" in by_phone
    assert [c["phone"] for c in biz.get("/api/v1/channels/whatsapp/conversations?q=shifa").json()] == [SHOP]
    r = biz.post("/api/v1/channels/whatsapp/reply", json={"phone": SHOP, "text": "Kal subah delivery ho jayegi."})
    assert r.status_code == 200
    msgs = biz.get(f"/api/v1/channels/whatsapp/messages?phone={SHOP}").json()
    assert msgs[-1]["direction"] == "out" and msgs[-1]["sent_by"] == "staff"
    assert msgs[-1]["body"] == "Kal subah delivery ho jayegi."
    assert any(m["ai_order_id"] for m in msgs if m["direction"] == "in")  # chat lines link to their order


def test_messages_have_their_own_tab_and_junk_can_be_deleted(make_biz):
    biz = verified(make_biz())
    say(biz, "bakwas service hai, kal ka order abhi tak nahi aaya")  # complaint -> Messages
    say(biz, "5 botal calpol")
    c = biz.get("/api/v1/ai/orders/counts").json()
    assert c["messages"] == 1 and c["open"] == 1
    assert [o["status"] for o in biz.get("/api/v1/ai/orders?status=messages").json()] == ["message"]
    junk = biz.get("/api/v1/ai/orders?status=messages").json()[0]
    order = biz.get("/api/v1/ai/orders?status=open").json()[0]
    biz.post(f"/api/v1/ai/orders/{order['id']}/approve", json={"acknowledge_warnings": True})
    r = biz.post("/api/v1/ai/orders/delete", json={"ids": [junk["id"], order["id"]]}).json()
    assert r["deleted"] == 1  # the billed order is never deleted
    assert biz.get("/api/v1/ai/orders?status=messages").json() == []
    assert biz.get(f"/api/v1/ai/orders/{order['id']}").json()["status"] == "approved"


def test_all_bills_list_and_detail(make_biz):
    biz = verified(make_biz())
    say(biz, "5 botal calpol")
    o = biz.get("/api/v1/ai/orders?status=open").json()[0]
    biz.post(f"/api/v1/ai/orders/{o['id']}/approve", json={"acknowledge_warnings": True})
    rows = biz.get("/api/v1/invoices?today=true").json()
    assert rows[0]["number"] == "W-000001" and rows[0]["source_label"] == "WhatsApp" and rows[0]["line_count"] == 1
    assert biz.get("/api/v1/invoices?q=shifa").json()[0]["number"] == "W-000001"
    d = biz.get(f"/api/v1/invoices/{rows[0]['id']}").json()
    assert d["lines"][0]["product_name"] == "Calpol Syrup 120ml" and d["total"] == "725.00"


def test_unread_clears_when_the_chat_is_opened(make_biz):
    biz = verified(make_biz())
    say(biz, "5 botal calpol")
    conv = {c["phone"]: c for c in biz.get("/api/v1/channels/whatsapp/conversations").json()}
    assert conv[SHOP]["unread"] == 4
    assert biz.post("/api/v1/channels/whatsapp/conversations/read", json={"phone": SHOP}).status_code == 200
    conv = {c["phone"]: c for c in biz.get("/api/v1/channels/whatsapp/conversations").json()}
    assert conv[SHOP]["unread"] == 0
    say(biz, "aur 2 box brufen bhi")  # a new message after reading
    conv = {c["phone"]: c for c in biz.get("/api/v1/channels/whatsapp/conversations").json()}
    assert conv[SHOP]["unread"] == 1


def test_bill_pdf_shows_in_the_chat_and_opens(make_biz):
    from ordexa.apps.worker import HANDLERS
    from ordexa.modules.jobs.service import run_one
    from ordexa.shared.db import get_sessionmaker

    biz = verified(make_biz())
    say(biz, "5 botal calpol")
    o = biz.get("/api/v1/ai/orders?status=open").json()[0]
    assert o["stage"] == "ready"
    biz.post(f"/api/v1/ai/orders/{o['id']}/approve", json={"acknowledge_warnings": True})
    with get_sessionmaker()() as s:
        while run_one(s, "outbox", HANDLERS["outbox"]):
            pass
    msgs = biz.get(f"/api/v1/channels/whatsapp/messages?phone={SHOP}").json()
    pdf = msgs[-1]["media"]
    assert pdf["kind"] == "pdf" and pdf["name"] == "Bill-W-000001.pdf"
    r = biz.client.get(pdf["path"], headers=biz.headers)
    assert r.status_code == 200 and r.content.startswith(b"%PDF") and r.headers["content-type"] == "application/pdf"
    assert any(m["order"] and m["order"]["number"] == o["number"] for m in msgs)  # order chip on the shop's message


def test_order_stages_for_the_desk(make_biz):
    biz = verified(make_biz())
    say(biz, "3 box panadol")  # bot asks the shop which one
    c = biz.get("/api/v1/ai/orders/counts").json()
    assert c["waiting_shop"] == 1 and c["ready"] == 0
    assert biz.get("/api/v1/ai/orders?status=open").json()[0]["stage"] == "waiting_shop"
    say(biz, "2")
    c = biz.get("/api/v1/ai/orders/counts").json()
    assert c["ready"] == 1 and c["waiting_shop"] == 0
