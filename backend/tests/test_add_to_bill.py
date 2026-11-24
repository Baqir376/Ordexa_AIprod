"""Same-day additions go into the shop's existing bill (same number) instead of a second bill."""

from datetime import timedelta
from decimal import Decimal

from sqlalchemy import func, select

from ordexa.modules.finance.models import LedgerEntry
from ordexa.modules.inventory.models import InventoryTransaction
from ordexa.modules.sales.models import Invoice
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


def billed_shop(biz):
    for m in ("hi", "1", "102"):
        say(biz, m)
    say(biz, "5 botal calpol")
    approve(biz, open_order(biz))  # bill W-000001: 5 x 145 = 725


def test_items_are_added_to_todays_bill(make_biz):
    biz = make_biz()
    billed_shop(biz)
    r = say(biz, "2 box brufen bhi add kar dein")
    assert "aaj ke bill W-000001 mein add" in r
    o = biz.get(f"/api/v1/ai/orders/{open_order(biz)['id']}").json()
    assert o["add_to_bill"]["number"] == "W-000001" and o["add_to_bill"]["possible"]
    approve(biz, o)
    with get_sessionmaker()() as s:
        bills = s.scalars(select(Invoice).where(Invoice.number.like("W-%"))).all()
        assert [b.number for b in bills] == ["W-000001"]  # no second bill
        bill = bills[0]
        assert bill.total == Decimal("1485.00")  # 725 + 2 x 380
        assert [ln.product_name for ln in sorted(bill.lines, key=lambda x: x.line_no)] == [
            "Calpol Syrup 120ml",
            "Brufen 400mg Tablets",
        ]
        assert any(f["code"] == "items_added" for f in bill.flags)
        moved = s.scalars(select(InventoryTransaction).where(InventoryTransaction.source_id == bill.id)).all()
        assert sorted(float(m.qty_delta) for m in moved) == [-5.0, -2.0]  # stock out once per line
        ledger = s.scalar(
            select(func.sum(LedgerEntry.amount)).where(
                LedgerEntry.party_id == bill.customer_id, LedgerEntry.reference.like("W-000001%")
            )
        )
        assert ledger == Decimal("1485.00")
    from ordexa.apps.worker import HANDLERS
    from ordexa.modules.jobs.service import run_one

    with get_sessionmaker()() as s:
        while run_one(s, "outbox", HANDLERS["outbox"]):
            pass
    sent = biz.get(f"/api/v1/channels/whatsapp/messages?phone={SHOP}").json()[-1]["body"]
    assert sent.startswith("[PDF: Bill-W-000001-updated.pdf]") and "Rs 1485.00" in sent


def test_staff_can_still_make_a_separate_bill(make_biz):
    biz = make_biz()
    billed_shop(biz)
    say(biz, "2 box brufen bhi")
    approve(biz, open_order(biz), new_bill=True)
    with get_sessionmaker()() as s:
        assert sorted(b.number for b in s.scalars(select(Invoice).where(Invoice.number.like("W-%")))) == ["W-000001", "W-000002"]


def test_yesterdays_bill_is_not_changed(make_biz):
    biz = make_biz()
    billed_shop(biz)
    with get_sessionmaker()() as s:
        bill = s.scalar(select(Invoice).where(Invoice.number == "W-000001"))
        bill.issued_at = bill.issued_at - timedelta(days=1)
        s.commit()
    r = say(biz, "2 box brufen bhi")
    assert "add kar diye jayenge" not in r
    approve(biz, open_order(biz))
    with get_sessionmaker()() as s:
        assert s.scalar(select(func.count()).select_from(Invoice).where(Invoice.number.like("W-%"))) == 2


def test_pc_bills_are_never_changed_from_whatsapp(make_biz):
    from ordexa.modules.sales.service import amend_blocker

    biz = make_biz()
    billed_shop(biz)
    with get_sessionmaker()() as s:
        bill = s.scalar(select(Invoice).where(Invoice.number == "W-000001"))
        bill.source = "desktop"
        assert "made on a PC" in amend_blocker(s, bill)
        s.rollback()
