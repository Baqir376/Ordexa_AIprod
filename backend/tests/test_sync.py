"""Phase 0 exit criterion: invoices made offline on two PCs sync with no duplicates, unique numbers,
and stock reduced exactly once."""

import uuid
from decimal import Decimal

from tests.conftest import PASSWORD


def _catalog(biz, device):
    data = biz.get("/api/v1/sync/pull", headers=biz.dev_headers(device)).json()
    products = {p["code"]: p for p in data["products"]}
    batches = {b["batch_no"]: b for b in data["batches"]}
    customers = {c["code"]: c for c in data["customers"]}
    return data, products, batches, customers


def _invoice(number, product, batch, qty="2", price=None, customer=None, bonus="0"):
    price = price or product["sale_price"]
    return {
        "id": str(uuid.uuid4()),
        "number": number,
        "customer_id": customer["id"] if customer else None,
        "issued_at": "2026-11-17T10:00:00+05:00",
        "total": str(Decimal(qty) * Decimal(price)),
        "lines": [
            {
                "line_no": 1,
                "product_id": product["id"],
                "batch_id": batch["id"] if batch else None,
                "quantity": qty,
                "bonus_quantity": bonus,
                "unit_price": price,
            }
        ],
    }


def _stock(biz, device, batch_id):
    data = biz.get("/api/v1/sync/pull", headers=biz.dev_headers(device)).json()
    return Decimal(next(s["on_hand"] for s in data["stock"] if s["batch_id"] == batch_id))


def test_activation_gives_each_pc_its_own_series_and_enforces_licence(make_biz):
    biz = make_biz(devices=2)
    assert biz.activate("Counter PC")["terminal_code"] == "T1"
    assert biz.activate("Back office")["terminal_code"] == "T2"
    r = biz.post("/api/v1/devices/activate", json={"name": "Third"})
    assert r.status_code == 402


def test_initial_pull_has_catalogue_stock_branding(make_biz):
    biz = make_biz()
    d = biz.activate("PC1")
    data, products, batches, customers = _catalog(biz, d)
    assert len(products) == 10 and "PN2401" in batches and "101" in customers
    assert data["branding"]["license_text"] == "Licensed to Ali Chemist"
    assert data["cursor"] > 0 and data["stock"]


def test_two_pcs_offline_then_sync_no_duplicates(make_biz):
    biz = make_biz()
    t1, t2 = biz.activate("PC1"), biz.activate("PC2")
    _, products, batches, customers = _catalog(biz, t1)
    pn, batch = products["P001"], batches["PN2407"]
    before = _stock(biz, t1, batch["id"])

    pc1 = [_invoice(f"T1-{n:06d}", pn, batch, customer=customers["101"]) for n in (1, 2, 3)]
    pc2 = [_invoice(f"T2-{n:06d}", pn, batch) for n in (1, 2)]

    r1 = biz.post("/api/v1/sync/push", json={"invoices": pc1}, headers=biz.dev_headers(t1)).json()
    r2 = biz.post("/api/v1/sync/push", json={"invoices": pc2}, headers=biz.dev_headers(t2)).json()
    assert [x["status"] for x in r1["results"] + r2["results"]] == ["accepted"] * 5

    # Network dropped before PC1 saw the response: it pushes everything again.
    again = biz.post("/api/v1/sync/push", json={"invoices": pc1}, headers=biz.dev_headers(t1)).json()
    assert [x["status"] for x in again["results"]] == ["duplicate"] * 3

    invoices = biz.get("/api/v1/invoices").json()
    assert len(invoices) == 5
    assert len({i["number"] for i in invoices}) == 5
    assert _stock(biz, t1, batch["id"]) == before - 10  # 5 invoices x 2, posted once


def test_same_id_different_content_is_a_conflict(make_biz):
    biz = make_biz()
    t1 = biz.activate("PC1")
    _, products, batches, _ = _catalog(biz, t1)
    inv = _invoice("T1-000001", products["P001"], batches["PN2407"])
    biz.post("/api/v1/sync/push", json={"invoices": [inv]}, headers=biz.dev_headers(t1))
    inv["lines"][0]["quantity"] = "9"
    r = biz.post("/api/v1/sync/push", json={"invoices": [inv]}, headers=biz.dev_headers(t1)).json()
    assert r["results"][0]["status"] == "conflict"


def test_pc_cannot_use_another_pcs_series(make_biz):
    biz = make_biz()
    t1, _ = biz.activate("PC1"), biz.activate("PC2")
    _, products, batches, _ = _catalog(biz, t1)
    r = biz.post(
        "/api/v1/sync/push",
        json={"invoices": [_invoice("T2-000001", products["P001"], batches["PN2407"])]},
        headers=biz.dev_headers(t1),
    ).json()
    assert r["results"][0]["status"] == "rejected"


def test_offline_business_breaches_are_flagged_not_lost(make_biz):
    biz = make_biz()
    t1 = biz.activate("PC1")
    _, products, batches, customers = _catalog(biz, t1)
    # Brufen batch BF2311 has 15 on hand; sell 20 (oversold) to Al-Rehman (limit 30,000) at an old price.
    inv = _invoice("T1-000001", products["P003"], batches["BF2311"], qty="20", price="350.00", customer=customers["103"])
    big = _invoice("T1-000002", products["P004"], batches["AU2402"], qty="40", customer=customers["103"])
    r = biz.post("/api/v1/sync/push", json={"invoices": [inv, big]}, headers=biz.dev_headers(t1)).json()
    assert [x["status"] for x in r["results"]] == ["accepted", "accepted"]
    codes1 = {f["code"] for f in r["results"][0]["flags"]}
    codes2 = {f["code"] for f in r["results"][1]["flags"]}
    assert {"oversold", "price_changed"} <= codes1
    assert "credit_exceeded" in codes2
    assert len(biz.get("/api/v1/invoices?flagged=true").json()) == 2


def test_bonus_quantity_leaves_stock_but_is_not_charged(make_biz):
    biz = make_biz()
    t1 = biz.activate("PC1")
    _, products, batches, _ = _catalog(biz, t1)
    before = _stock(biz, t1, batches["PN2407"]["id"])
    inv = _invoice("T1-000001", products["P001"], batches["PN2407"], qty="10", bonus="1")
    r = biz.post("/api/v1/sync/push", json={"invoices": [inv]}, headers=biz.dev_headers(t1)).json()
    assert r["results"][0]["total"] == "4500.00"
    assert _stock(biz, t1, batches["PN2407"]["id"]) == before - 11


def test_incremental_pull_returns_only_changes(make_biz):
    biz = make_biz()
    t1 = biz.activate("PC1")
    first = biz.get("/api/v1/sync/pull", headers=biz.dev_headers(t1)).json()
    nothing = biz.get(f"/api/v1/sync/pull?cursor={first['cursor']}", headers=biz.dev_headers(t1)).json()
    assert nothing["products"] == [] and nothing["branding"] is None and nothing["cursor"] == first["cursor"]
    biz.post("/api/v1/products", json={"code": "P099", "name": "Disprin", "sale_price": "90.00"})
    biz.put("/api/v1/tenant/branding", json={"primary_color": "#1D4ED8"})
    delta = biz.get(f"/api/v1/sync/pull?cursor={first['cursor']}", headers=biz.dev_headers(t1)).json()
    assert [p["code"] for p in delta["products"]] == ["P099"]
    assert delta["branding"]["primary_color"] == "#1D4ED8"
    assert delta["cursor"] > first["cursor"]


def test_deactivated_pc_cannot_sync(make_biz):
    biz = make_biz()
    t1 = biz.activate("PC1")
    biz.post(f"/api/v1/devices/{t1['id']}/deactivate")
    assert biz.get("/api/v1/sync/pull", headers=biz.dev_headers(t1)).status_code == 403


def test_staff_without_invoice_permission_cannot_push(make_biz):
    biz = make_biz()
    t1 = biz.activate("PC1")
    biz.post(
        "/api/v1/users", json={"username": "viewer", "full_name": "V", "password": PASSWORD, "permissions": ["product.view"]}
    )
    h = {"Authorization": f"Bearer {biz.login('viewer')}", "X-Device-Id": t1["id"]}
    _, products, batches, _ = _catalog(biz, t1)
    r = biz.post("/api/v1/sync/push", headers=h, json={"invoices": [_invoice("T1-000001", products["P001"], batches["PN2407"])]})
    assert r.status_code == 403
