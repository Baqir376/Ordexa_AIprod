"""Shops change their mind in the chat: corrections, swaps, extra items, removals and cancels apply to the open order
instead of opening new orders (replays the pilot chat of 24 Nov 2026)."""

SHOP = "+923001110102"  # shop 102's number on file: verified directly


def say(biz, text):
    r = biz.post("/api/v1/channels/whatsapp/simulate", json={"phone": SHOP, "text": text})
    assert r.status_code == 200, r.text
    return r.json()


def reply(biz, text):
    r = say(biz, text)
    return "\n".join(r["bot_replies"]) or biz.get(f"/api/v1/channels/whatsapp/messages?phone={SHOP}").json()[-1]["body"]


def orders(biz):
    return biz.get("/api/v1/ai/orders").json()


def detail(biz, o):
    return biz.get(f"/api/v1/ai/orders/{o['id']}").json()


def verified(biz):
    for m in ("hi", "1", "102"):
        say(biz, m)


def test_correction_updates_the_same_order(make_biz):
    biz = make_biz()
    verified(biz)
    say(biz, "10 box panadol, 5 patta brufen")
    r = reply(biz, "1")
    assert "5 strip" in r and "5.0" not in r  # quantities shown without ".0"
    r = reply(biz, "balkay panadol ke 20 box kr dein aur brufen ke 6")
    assert len(orders(biz)) == 1  # no second order
    o = detail(biz, orders(biz)[0])
    assert o["status"] == "draft_ready"
    got = {ln["product_name"]: float(ln["quantity"]) for ln in o["lines"]}
    assert got == {"Panadol 500mg Tablets": 20.0, "Brufen 400mg Tablets": 6.0}
    assert "ab yeh hai" in r and "Panadol 500mg Tablets: 20" in r


def test_shop_names_another_product_instead_of_choosing(make_biz):
    biz = make_biz()
    verified(biz)
    r = reply(biz, "mujhe arinac syrup chahiye")
    assert "konsa chahiye" in r
    r = reply(biz, "balkay mujhe paracetamol chahiye")
    assert "samajh nahi aaya" not in r and len(orders(biz)) == 1
    # paracetamol is an ingredient, not a product: the question says so and shows it next to each brand
    assert "naam ki item hamari list mein nahi hai" in r and "(Paracetamol)" in r
    r = reply(biz, "1")
    o = detail(biz, orders(biz)[0])
    assert o["lines"][0]["product_name"] and "arinac" not in o["lines"][0]["phrase"].lower()


def test_drop_this_one_and_send_another(make_biz):
    biz = make_biz()
    verified(biz)
    say(biz, "2 box arinac syrup")
    r = reply(biz, "is ko choro paracetamol dedo")
    assert len(orders(biz)) == 1 and "paracetamol" in r.lower()
    o = detail(biz, orders(biz)[0])
    assert len(o["lines"]) == 1 and o["lines"][0]["phrase"] == "paracetamol" and float(o["lines"][0]["quantity"]) == 2


def test_extra_item_is_added_to_the_waiting_order(make_biz):
    biz = make_biz()
    verified(biz)
    say(biz, "5 botal calpol")
    assert orders(biz)[0]["status"] == "draft_ready"
    r = reply(biz, "3 box brufen bhi")
    assert len(orders(biz)) == 1 and "add kar diya" in r
    names = {ln["product_name"] for ln in detail(biz, orders(biz)[0])["lines"]}
    assert names == {"Calpol Syrup 120ml", "Brufen 400mg Tablets"}


def test_remove_an_item_and_cancel_the_order(make_biz):
    biz = make_biz()
    verified(biz)
    say(biz, "5 botal calpol aur 3 box brufen")
    r = reply(biz, "brufen nahi chahiye")
    assert "nikal diya" in r
    assert [ln["product_name"] for ln in detail(biz, orders(biz)[0])["lines"]] == ["Calpol Syrup 120ml"]
    r = reply(biz, "order cancel kar dein")
    assert "cancel kar diya" in r and orders(biz)[0]["status"] == "rejected"


def test_a_plain_new_order_while_one_is_waiting_is_a_new_order(make_biz):
    biz = make_biz()
    verified(biz)
    say(biz, "5 botal calpol")
    say(biz, "3 box brufen")
    assert len(orders(biz)) == 2


def test_old_unfinished_order_is_not_reopened(make_biz):
    biz = make_biz()
    verified(biz)
    say(biz, "10 box panadol")  # asks which panadol: left unanswered
    say(biz, "5 botal calpol")  # a newer complete order
    first = next(o for o in orders(biz) if o["status"] == "needs_review")
    newest = next(o for o in orders(biz) if o["status"] == "draft_ready")
    assert biz.post(f"/api/v1/ai/orders/{newest['id']}/approve", json={"acknowledge_warnings": True}).status_code == 200
    say(biz, "2 box brufen bhi")  # the newest order is billed: this must not be added to the old unfinished one
    assert len(detail(biz, first)["lines"]) == 1
    assert len(orders(biz)) == 3
