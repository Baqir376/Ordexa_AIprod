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


def _deliver():
    from ordexa.apps.worker import HANDLERS
    from ordexa.modules.jobs.service import run_one

    with get_sessionmaker()() as s:
        while run_one(s, "outbox", HANDLERS["outbox"]):
            pass


def _bill_state():
    with get_sessionmaker()() as s:
        bills = s.scalars(select(Invoice).where(Invoice.number.like("W-%"))).all()
        b = bills[0]
        lines = [(ln.product_name, float(ln.quantity)) for ln in sorted(b.lines, key=lambda x: x.line_no)]
        sold = InventoryTransaction.kind.in_(("sale", "sale_correction"))
        moved = s.scalar(
            select(func.sum(InventoryTransaction.qty_delta)).where(InventoryTransaction.product_id == b.lines[0].product_id, sold)
        )
        ledger = s.scalar(select(func.sum(LedgerEntry.amount)).where(LedgerEntry.party_id == b.customer_id))
        return len(bills), b.total, lines, moved, ledger


def _billed_panadol(biz, n=10):
    for m in ("hi", "1", "102"):
        say(biz, m)
    say(biz, f"{n} box panadol extra")
    approve(biz, open_order(biz))  # W-000001: n x 620


def test_correction_without_product_name_changes_the_same_bill(make_biz):
    biz = make_biz()
    _billed_panadol(biz)
    r = say(biz, "No, instead of 10, I need 50")
    assert "bill W-000001 mein yeh tabdeeli" in r and "Panadol Extra Tablets: 10 -> 50" in r
    o = biz.get(f"/api/v1/ai/orders/{open_order(biz)['id']}").json()
    assert o["add_to_bill"]["mode"] == "correct" and o["add_to_bill"]["possible"]
    approve(biz, o)
    n, total, lines, moved, ledger = _bill_state()
    assert n == 1 and lines == [("Panadol Extra Tablets", 50.0)]  # one bill, 50 not 60
    assert total == Decimal("31000.00") and ledger == Decimal("31000.00")
    assert moved == Decimal("-50")  # stock: 10 sold + 40 more
    _deliver()
    sent = biz.get(f"/api/v1/channels/whatsapp/messages?phone={SHOP}").json()[-1]["body"]
    assert "update ho gaya" in sent and "Panadol Extra Tablets: 10 → 50" in sent and "Bill-W-000001-updated.pdf" in sent


def test_correction_down_returns_stock_and_lowers_the_balance(make_biz):
    biz = make_biz()
    _billed_panadol(biz)
    say(biz, "nahi, panadol extra 10 ki jagah 4 kar dein")
    approve(biz, open_order(biz))
    n, total, lines, moved, ledger = _bill_state()
    assert n == 1 and lines == [("Panadol Extra Tablets", 4.0)]
    assert total == Decimal("2480.00") and ledger == Decimal("2480.00") and moved == Decimal("-4")


def test_correction_of_yesterdays_bill_makes_a_new_order(make_biz):
    from datetime import timedelta

    biz = make_biz()
    _billed_panadol(biz)
    with get_sessionmaker()() as s:
        b = s.scalar(select(Invoice).where(Invoice.number == "W-000001"))
        b.issued_at = b.issued_at - timedelta(days=1)
        s.commit()
    r = say(biz, "No, instead of 10, I need 50")
    assert "tabdeeli ki jayegi" not in r
