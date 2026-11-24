"""Phase 1: purchases with batches, party ledger, receipts and cheques, returns, transfers, reports."""

import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from tests.conftest import PASSWORD


def _setup(biz):
    dev = biz.activate("PC1")
    data = biz.get("/api/v1/sync/pull", headers=biz.dev_headers(dev)).json()
    products = {p["code"]: p for p in data["products"]}
    batches = {b["batch_no"]: b for b in data["batches"]}
    customers = {c["code"]: c for c in data["customers"]}
    suppliers = {s["code"]: s for s in biz.get("/api/v1/suppliers").json()}
    return dev, products, batches, customers, suppliers


def _sell(biz, dev, number, product, batch, qty, customer=None, paid="0"):
    inv = {
        "id": str(uuid.uuid4()),
        "number": number,
        "customer_id": customer["id"] if customer else None,
        "issued_at": datetime.now(UTC).isoformat(),
        "paid_amount": paid,
        "lines": [
            {
                "line_no": 1,
                "product_id": product["id"],
                "batch_id": batch["id"] if batch else None,
                "quantity": qty,
                "unit_price": product["sale_price"],
            }
        ],
    }
    r = biz.post("/api/v1/sync/push", json={"invoices": [inv]}, headers=biz.dev_headers(dev)).json()
    assert r["results"][0]["status"] == "accepted", r
    return r["results"][0]


def _stock(biz, product_id, batch_no):
    rows = [r for r in biz.get("/api/v1/reports/stock").json() if r["product_id"] == product_id and r["batch_no"] == batch_no]
    return Decimal(rows[0]["on_hand"]) if rows else Decimal(0)


def _balance(biz, party_type, party_id):
    return Decimal(biz.get(f"/api/v1/ledger/{party_type}/{party_id}").json()["balance"])


def test_purchase_creates_batch_adds_stock_and_payable(make_biz):
    biz = make_biz()
    _, products, _, _, suppliers = _setup(biz)
    pn, sup = products["P001"], suppliers["S01"]
    exp = (date.today() + timedelta(days=500)).isoformat()
    body = {
        "id": str(uuid.uuid4()),
        "supplier_id": sup["id"],
        "supplier_bill_no": "AH-778",
        "bill_date": date.today().isoformat(),
        "paid_amount": "10000",
        "lines": [
            {
                "product_id": pn["id"],
                "quantity": "50",
                "bonus_quantity": "5",
                "unit_cost": "380",
                "batch_no": "PN2511",
                "expiry_date": exp,
                "sale_price": "460",
            }
        ],
    }
    r = biz.post("/api/v1/purchases", json=body)
    assert r.status_code == 201, r.text
    assert r.json()["number"] == "PI-000001" and r.json()["total"] == "19000.00"
    assert _stock(biz, pn["id"], "PN2511") == 55  # bonus goods are stock too
    assert _balance(biz, "supplier", sup["id"]) == Decimal("9000.00")  # 19,000 - 10,000 paid
    again = biz.post("/api/v1/purchases", json=body).json()
    assert again["duplicate"] is True and _stock(biz, pn["id"], "PN2511") == 55
    batch = next(b for b in biz.get(f"/api/v1/products/{pn['id']}/batches").json() if b["batch_no"] == "PN2511")
    assert batch["sale_price"] == "460.00"
    # Same batch number with a different expiry is a data-entry error.
    bad = {**body, "id": str(uuid.uuid4()), "lines": [{**body["lines"][0], "expiry_date": date.today().isoformat()}]}
    assert biz.post("/api/v1/purchases", json=bad).status_code == 409


def test_batch_tracked_purchase_requires_batch_and_expiry(make_biz):
    biz = make_biz()
    _, products, _, _, suppliers = _setup(biz)
    body = {
        "id": str(uuid.uuid4()),
        "supplier_id": suppliers["S01"]["id"],
        "bill_date": date.today().isoformat(),
        "lines": [{"product_id": products["P001"]["id"], "quantity": "5", "unit_cost": "380"}],
    }
    assert biz.post("/api/v1/purchases", json=body).status_code == 422


def test_customer_ledger_receipts_cheques_and_counter_cash(make_biz):
    biz = make_biz()
    dev, products, batches, customers, _ = _setup(biz)
    shop = customers["102"]
    _sell(biz, dev, "T1-000001", products["P001"], batches["PN2407"], "10", shop, paid="1000")  # 4,500 - 1,000
    assert _balance(biz, "customer", shop["id"]) == Decimal("3500.00")
    cash = {"id": str(uuid.uuid4()), "direction": "in", "party_id": shop["id"], "amount": "500", "method": "cash"}
    assert biz.post("/api/v1/payments", json=cash).json()["number"] == "RV-000001"
    assert biz.post("/api/v1/payments", json=cash).json()["duplicate"] is True
    cheque = {
        "id": str(uuid.uuid4()),
        "direction": "in",
        "party_id": shop["id"],
        "amount": "2000",
        "method": "cheque",
        "cheque_no": "00012345",
        "cheque_bank": "HBL",
    }
    chq = biz.post("/api/v1/payments", json=cheque).json()
    assert chq["cheque_status"] == "pending" and _balance(biz, "customer", shop["id"]) == Decimal("1000.00")
    assert biz.post(f"/api/v1/payments/{chq['id']}/cheque-status", json={"status": "bounced"}).status_code == 200
    assert _balance(biz, "customer", shop["id"]) == Decimal("3000.00")  # bounced cheque is owed again
    assert biz.post(f"/api/v1/payments/{chq['id']}/cheque-status", json={"status": "cleared"}).status_code == 409
    kinds = [e["kind"] for e in biz.get(f"/api/v1/ledger/customer/{shop['id']}").json()["entries"]]
    assert kinds == ["invoice", "receipt", "receipt", "receipt", "cheque_bounced"]
    # Desktops receive the ledger balance on pull (used for offline credit checks).
    pulled = biz.get("/api/v1/sync/pull", headers=biz.dev_headers(dev)).json()
    assert pulled["customer_balances"][shop["id"]] == "3000.00"


def test_desktop_receipts_sync_idempotently_with_series(make_biz):
    biz = make_biz()
    dev, _, _, customers, _ = _setup(biz)
    shop = customers["101"]
    rec = {
        "id": str(uuid.uuid4()),
        "number": "T1R-000001",
        "customer_id": shop["id"],
        "amount": "750",
        "received_at": datetime.now(UTC).isoformat(),
    }
    bad = {**rec, "id": str(uuid.uuid4()), "number": "T9R-000001"}
    r = biz.post("/api/v1/sync/push", json={"receipts": [rec, bad]}, headers=biz.dev_headers(dev)).json()
    assert [x["status"] for x in r["receipts"]] == ["accepted", "rejected"]
    r2 = biz.post("/api/v1/sync/push", json={"receipts": [rec]}, headers=biz.dev_headers(dev)).json()
    assert r2["receipts"][0]["status"] == "duplicate"
    assert _balance(biz, "customer", shop["id"]) == Decimal("-750.00")  # advance


def test_sales_return_restocks_and_credits(make_biz):
    biz = make_biz()
    dev, products, batches, customers, _ = _setup(biz)
    shop, pn, b = customers["101"], products["P001"], batches["PN2407"]
    _sell(biz, dev, "T1-000001", pn, b, "4", shop)
    before = _stock(biz, pn["id"], "PN2407")
    cn = {
        "id": str(uuid.uuid4()),
        "customer_id": shop["id"],
        "reason": "damaged boxes returned",
        "lines": [
            {"product_id": pn["id"], "batch_id": b["id"], "quantity": "2", "unit_price": "450"},
            {"product_id": pn["id"], "batch_id": b["id"], "quantity": "1", "unit_price": "450", "restock": False},
        ],
    }
    r = biz.post("/api/v1/credit-notes", json=cn).json()
    assert r["number"] == "CN-000001" and r["total"] == "1350.00"
    assert _stock(biz, pn["id"], "PN2407") == before + 2  # damaged unit not restocked
    assert _balance(biz, "customer", shop["id"]) == Decimal("450.00")
    assert biz.post("/api/v1/credit-notes", json=cn).json()["duplicate"] is True


def test_aging_uses_fifo(make_biz):
    biz = make_biz()
    dev, products, batches, customers, _ = _setup(biz)
    shop = customers["105"]
    _sell(biz, dev, "T1-000001", products["P001"], batches["PN2407"], "2", shop)  # 900
    biz.post("/api/v1/payments", json={"id": str(uuid.uuid4()), "direction": "in", "party_id": shop["id"], "amount": "400"})
    row = next(r for r in biz.get("/api/v1/aging/customer").json() if r["party_id"] == shop["id"])
    assert row["balance"] == "500.00" and row["0_30"] == "500.00"


def test_transfer_moves_stock_between_warehouses(make_biz):
    biz = make_biz()
    _, products, batches, _, _ = _setup(biz)
    wh2 = biz.post("/api/v1/inventory/warehouses", json={"code": "W2", "name": "Godown 2"}).json()
    main = biz.get("/api/v1/tenant").json()["warehouses"][0]
    body = {
        "id": str(uuid.uuid4()),
        "from_warehouse_id": main["id"],
        "to_warehouse_id": wh2["id"],
        "product_id": products["P001"]["id"],
        "batch_id": batches["PN2407"]["id"],
        "quantity": "20",
    }
    assert biz.post("/api/v1/inventory/transfers", json=body).status_code == 201
    assert biz.post("/api/v1/inventory/transfers", json=body).json()["duplicate"] is True
    bal = {(b["warehouse_id"], b["batch_id"]): Decimal(b["on_hand"]) for b in biz.get("/api/v1/inventory/balances").json()}
    assert bal[(wh2["id"], batches["PN2407"]["id"])] == 20
    assert bal[(main["id"], batches["PN2407"]["id"])] == 100
    too_much = {**body, "id": str(uuid.uuid4()), "quantity": "999"}
    assert biz.post("/api/v1/inventory/transfers", json=too_much).status_code == 409


def test_reports_dashboard_low_stock_expiry_profit(make_biz):
    biz = make_biz()
    dev, products, batches, customers, _ = _setup(biz)
    _sell(biz, dev, "T1-000001", products["P003"], batches["BF2311"], "10", customers["101"])  # 3,800 (near-expiry batch)
    _sell(biz, dev, "T1-000002", products["P007"], batches["AR2406"], "25", customers["101"])  # 17,250; leaves 5 <= 20
    biz.post("/api/v1/expenses", json={"id": str(uuid.uuid4()), "category": "Electricity", "amount": "1000"})
    d = biz.get("/api/v1/reports/dashboard").json()
    assert d["month"]["invoices"] == 2 and d["receivable"] == "21050.00"
    assert d["low_stock_products"] >= 1 and d["near_expiry_batches"] >= 1 and len(d["trend"]) == 14
    low = {r["code"]: r for r in biz.get("/api/v1/reports/low-stock").json()}
    assert "P010" not in low and low["P007"]["on_hand"] == "5.000"
    assert Decimal(low["P007"]["suggested_order"]) == 35  # 2 x reorder level (40) - on hand (5) > 30-day sales - on hand
    exp = biz.get("/api/v1/reports/expiry?days=90").json()
    assert all(r["days_left"] <= 90 for r in exp) and any(r["batch_no"] == "BF2311" for r in exp)
    pk_today = datetime.now(ZoneInfo("Asia/Karachi")).date()  # business date, not the server's UTC date
    prof = biz.get(f"/api/v1/reports/profit?start={pk_today.replace(day=1).isoformat()}&end={pk_today.isoformat()}").json()
    # cost = 85% of price in the demo seed: 3,230.00 + 14,662.50
    assert prof["sales"] == "21050.00" and prof["cost_of_goods"] == "17892.50" and prof["net_profit"] == "2157.50"


def test_staff_needs_finance_permissions(make_biz):
    biz = make_biz()
    _, _, _, customers, _ = _setup(biz)
    biz.post(
        "/api/v1/users",
        json={"username": "cashier", "full_name": "C", "password": PASSWORD, "permissions": ["invoice.create", "customer.view"]},
    )
    h = {"Authorization": f"Bearer {biz.login('cashier')}"}
    pay = {"id": str(uuid.uuid4()), "direction": "in", "party_id": customers["101"]["id"], "amount": "10"}
    assert biz.post("/api/v1/payments", json=pay, headers=h).status_code == 403
    assert biz.get("/api/v1/reports/dashboard", headers=h).status_code == 403


def test_backup_exports_everything_but_secrets(make_biz):
    biz = make_biz()
    dev, products, batches, customers, _ = _setup(biz)
    _sell(biz, dev, "T1-000001", products["P001"], batches["PN2407"], "2", customers["101"])
    b = biz.get("/api/v1/backup/export").json()
    assert "tables" in b, b
    t = b["tables"]
    assert b["format"] == "ordexa-backup/1" and len(t["products"]) == 10 and len(t["invoices"]) == 1
    assert len(t["invoice_lines"]) == 1 and t["ledger_entries"]
    assert "password_hash" not in t["users"][0]
    other = make_biz("other-pharma", "Other")
    r = other.get("/api/v1/backup/export").json()
    assert r["tables"]["invoices"] == [], r


def test_bill_just_after_midnight_in_pakistan_gets_the_pakistan_date(make_biz):
    """00:30 in Karachi is 19:30 UTC the previous day: the ledger and reports must use the Karachi date."""
    biz = make_biz()
    dev, products, batches, customers, _ = _setup(biz)
    shop = customers["101"]
    inv = {
        "id": str(uuid.uuid4()),
        "number": "T1-000001",
        "customer_id": shop["id"],
        "issued_at": "2026-11-23T19:30:00+00:00",
        "lines": [
            {
                "line_no": 1,
                "product_id": products["P001"]["id"],
                "batch_id": batches["PN2407"]["id"],
                "quantity": "1",
                "unit_price": products["P001"]["sale_price"],
            }
        ],
    }
    assert (
        biz.post("/api/v1/sync/push", json={"invoices": [inv]}, headers=biz.dev_headers(dev)).json()["results"][0]["status"]
        == "accepted"
    )
    entry = biz.get(f"/api/v1/ledger/customer/{shop['id']}").json()["entries"][0]
    assert entry["date"] == "2026-11-24"
    day = biz.get("/api/v1/reports/sales-by-day?start=2026-11-24&end=2026-11-24").json()
    assert day[0]["invoices"] == 1
