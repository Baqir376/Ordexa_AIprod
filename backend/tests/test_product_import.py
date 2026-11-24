"""Excel product import: header found anywhere, units/prices guessed from the form, duplicates and totals skipped."""

from decimal import Decimal

import openpyxl
from sqlalchemy import func, select

from ordexa.modules.catalogue.importer import guess_form, import_products
from ordexa.modules.catalogue.models import Product
from ordexa.modules.inventory.models import InventoryTransaction
from ordexa.modules.tenants.models import Tenant
from ordexa.shared.db import get_sessionmaker


def test_guess_form():
    assert guess_form("2SUM 2G INJ") == ("vial", "300")
    assert guess_form("BLUDOL 200MGTAB")[0] == "box"
    assert guess_form("BRUFEN DS SYP")[0] == "bottle"
    assert guess_form("ACTIMOX EYE DROPS")[0] == "bottle"
    assert guess_form("FUCORT CREAM")[0] == "tube"
    assert guess_form("ANSELL gloves7 no")[0] == "piece"


def test_import_from_excel(make_biz, tmp_path):
    make_biz()
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append([None, None])
    ws.append([None, "Item Description"])
    for name in ["2SUM 2G INJ", "BRUFEN DS SYP", "brufen  ds syp", "Panadol 500mg Tablets", "Gross Amt."]:
        ws.append([None, name])
    path = tmp_path / "medicine_data.xlsx"
    wb.save(path)
    with get_sessionmaker()() as s:
        t = s.scalar(select(Tenant).where(Tenant.code == "ali-chemist"))
        r = import_products(s, t, path, default_stock=Decimal(750))
        s.commit()
        assert r.added == 2 and r.placeholder_price == 2
        assert r.duplicates_in_file == ["brufen ds syp"] and r.skipped_existing == ["Panadol 500mg Tablets"]
        p = s.scalar(select(Product).where(Product.tenant_id == t.id, Product.name == "2SUM 2G INJ"))
        assert p.sale_unit == "vial" and p.sale_price == Decimal(300) and p.code.startswith("M")
        stock = s.scalar(select(func.sum(InventoryTransaction.qty_delta)).where(InventoryTransaction.product_id == p.id))
        assert stock == Decimal(750)
