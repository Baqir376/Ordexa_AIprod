"""A shop changes its bill within one business day (add, change or remove items): the SAME bill is revised and the shop
gets the complete updated bill. Older bills are never changed; a new order is made instead."""

from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import func, select

from ordexa.modules.finance.models import LedgerEntry
from ordexa.modules.inventory.models import InventoryTransaction
from ordexa.modules.sales.models import Invoice
from ordexa.modules.sales.service import next_business_day
from ordexa.shared.db import get_sessionmaker

SHOP = "+923001110102"


def say(biz, text):
    r = biz.post("/api/v1/channels/whatsapp/simulate", json={"phone": SHOP, "text": text})
    assert r.status_code == 200, r.text
    return "\n".join(r.json()["bot_replies"]) or biz.get(f"/api/v1/channels/whatsapp/messages?phone={SHOP}").json()[-1]["body"]


def open_order(biz):
    return biz.get("/api/v1/ai/orders?status=open").json()[0]


def approve(biz, o, **extra):
    r = biz.post(f"/api/v1/ai/orders/{o['id']}/approve", json={"acknowledge_warnings": True, **extra})
    assert r.status_code == 200, r.text
    return r.json()


def deliver():
    from ordexa.apps.worker import HANDLERS
    from ordexa.modules.jobs.service import run_one

    with get_sessionmaker()() as s:
        while run_one(s, "outbox", HANDLERS["outbox"]):
            pass


def last_sent(biz):
    deliver()
    return biz.get(f"/api/v1/channels/whatsapp/messages?phone={SHOP}").json()[-1]["body"]


def bill_state():
    """(number of W bills, total, [(product, qty)], net stock out per product, shop balance)."""
    with get_sessionmaker()() as s:
        bills = s.scalars(select(Invoice).where(Invoice.number.like("W-%"))).all()
        b = bills[0]
        lines = [(ln.product_name, float(ln.quantity)) for ln in sorted(b.lines, key=lambda x: x.line_no)]
        sold = InventoryTransaction.kind.in_(("sale", "sale_correction"))
        moved = dict(
            s.execute(
                select(InventoryTransaction.product_id, func.sum(InventoryTransaction.qty_delta))
                .where(sold, InventoryTransaction.source_type.in_(("invoice", "invoice_revision")))
                .group_by(InventoryTransaction.product_id)
            ).all()
        )
        ledger = s.scalar(select(func.sum(LedgerEntry.amount)).where(LedgerEntry.party_id == b.customer_id))
        return len(bills), b.total, lines, moved, ledger


def billed(biz, text="10 box panadol extra"):
    for m in ("hi", "1", "102"):
        say(biz, m)
    say(biz, text)
    approve(biz, open_order(biz))  # W-000001


def age_bill(days):
    with get_sessionmaker()() as s:
        b = s.scalar(select(Invoice).where(Invoice.number == "W-000001"))
        b.issued_at = b.issued_at - timedelta(days=days)
        s.commit()


def test_add_a_new_item_and_more_of_an_existing_one(make_biz):
    biz = make_biz()
    billed(biz, "5 botal calpol")
    r = say(biz, "2 box brufen aur 3 botal calpol bhi add kar dein")
    assert "bill W-000001 mein yeh tabdeeli" in r
    assert "Calpol Syrup 120ml: 5 -> 8 bottle" in r and "Brufen 400mg Tablets: 2 box (naya item)" in r
    o = biz.get(f"/api/v1/ai/orders/{open_order(biz)['id']}").json()
    assert o["add_to_bill"]["possible"] and len(o["add_to_bill"]["changes"]) == 2
    approve(biz, o)
    n, total, lines, _, ledger = bill_state()
    assert n == 1 and lines == [("Calpol Syrup 120ml", 8.0), ("Brufen 400mg Tablets", 2.0)]
    assert total == Decimal("1920.00") and ledger == total  # 8 x 145 + 2 x 380
    sent = last_sent(biz)
    assert sent.startswith("[PDF: Bill-W-000001-updated.pdf]") and "Poora updated bill" in sent and "Rs 1920.00" in sent


def test_change_quantity_without_naming_the_product(make_biz):
    biz = make_biz()
    billed(biz)
    r = say(biz, "No, instead of 10, I need 50")
    assert "Panadol Extra Tablets: 10 -> 50" in r
    approve(biz, open_order(biz))
    n, total, lines, moved, ledger = bill_state()
    assert n == 1 and lines == [("Panadol Extra Tablets", 50.0)]  # one bill with 50, not a second bill
    assert total == Decimal("31000.00") and ledger == total and list(moved.values()) == [Decimal("-50")]


def test_lower_a_quantity(make_biz):
    biz = make_biz()
    billed(biz)
    say(biz, "nahi, panadol extra 10 ki jagah 4 kar dein")
    approve(biz, open_order(biz))
    n, total, lines, moved, ledger = bill_state()
    assert lines == [("Panadol Extra Tablets", 4.0)] and total == Decimal("2480.00") and ledger == total
    assert list(moved.values()) == [Decimal("-4")]  # 6 boxes went back to stock


def test_remove_an_item(make_biz):
    biz = make_biz()
    billed(biz, "5 botal calpol aur 2 box brufen")
    r = say(biz, "brufen nahi chahiye")
    assert "Brufen 400mg Tablets: hata diya jayega" in r
    approve(biz, open_order(biz))
    n, total, lines, moved, ledger = bill_state()
    assert n == 1 and lines == [("Calpol Syrup 120ml", 5.0)]
    assert total == Decimal("725.00") and ledger == total and sorted(moved.values()) == [Decimal("-5"), Decimal("0")]
    assert "Rs 725.00" in last_sent(biz)


def test_next_business_day_still_changes_the_same_bill(make_biz):
    biz = make_biz()
    billed(biz)
    age_bill(1)
    with get_sessionmaker()() as s:
        b = s.scalar(select(Invoice).where(Invoice.number == "W-000001"))
        from ordexa.modules.sales.service import amend_blocker
        from ordexa.modules.tenants.timeutil import business_today, local_date

        billed_on = local_date(s, b.tenant_id, b.issued_at)
        allowed = business_today(s, b.tenant_id) == next_business_day(billed_on)
        assert (amend_blocker(s, b) is None) == allowed  # yesterday counts unless yesterday was Saturday
    if allowed:
        assert "tabdeeli ki jayegi" in say(biz, "No, instead of 10, I need 50")


def test_older_bills_are_not_changed(make_biz):
    biz = make_biz()
    billed(biz)
    age_bill(3)
    r = say(biz, "No, instead of 10, I need 50")
    assert "tabdeeli ki jayegi" not in r


def test_sunday_is_not_a_business_day():
    assert next_business_day(date(2026, 11, 21)) == date(2026, 11, 23)  # Saturday -> Monday
    assert next_business_day(date(2026, 11, 24)) == date(2026, 11, 25)


def test_staff_can_still_make_a_separate_bill(make_biz):
    biz = make_biz()
    billed(biz, "5 botal calpol")
    say(biz, "2 box brufen bhi")
    approve(biz, open_order(biz), new_bill=True)
    with get_sessionmaker()() as s:
        assert sorted(b.number for b in s.scalars(select(Invoice).where(Invoice.number.like("W-%")))) == ["W-000001", "W-000002"]


def test_pc_bills_are_never_changed_from_whatsapp(make_biz):
    from ordexa.modules.sales.service import amend_blocker

    biz = make_biz()
    billed(biz, "5 botal calpol")
    with get_sessionmaker()() as s:
        bill = s.scalar(select(Invoice).where(Invoice.number == "W-000001"))
        bill.source = "desktop"
        assert "made on a PC" in amend_blocker(s, bill)
        s.rollback()
