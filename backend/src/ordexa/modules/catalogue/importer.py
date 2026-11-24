"""Import a product list from Excel (.xlsx) or CSV.

Only a name column is required (e.g. "Item Description"). Optional columns, matched by header: code, unit,
price / rate, stock / qty, generic, manufacturer / company, pack. Missing units and prices are filled from the
product's form (TAB, INJ, SYP, CREAM ...) as **placeholders** that staff must correct in Products; such products are
reported back so nothing is silently priced. Existing products (same name, ignoring case/spaces) are skipped.
"""

from __future__ import annotations

import csv
import re
import uuid
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from ordexa.modules.catalogue.models import Batch, Product
from ordexa.modules.inventory import service as inventory
from ordexa.modules.tenants.models import Tenant, Warehouse

HEADERS = {
    "name": ("item description", "description", "item", "product", "product name", "name", "medicine", "item name"),
    "code": ("code", "item code", "product code", "sku"),
    "unit": ("unit", "sale unit", "uom"),
    "price": ("price", "rate", "sale price", "trade price", "tp", "sale rate"),
    "stock": ("stock", "qty", "quantity", "opening stock", "balance"),
    "generic": ("generic", "generic name", "formula", "salt"),
    "manufacturer": ("manufacturer", "company", "mfr", "brand owner"),
    "pack": ("pack", "pack size", "packing"),
}

# (pattern on the upper-case name, sale unit, placeholder price in Rs)
FORMS = [
    (
        r"\b(NYLON|SILK|VICRYL|CATGUT|PROLENE|SUTURE|GOWN|CANNULA|STOPER|STOPPER|SYRINGE|CATHETER|BANDAGE|"
        r"GAUZE|MASK|CUFFED|TUBE SET|DRIP SET|BLADE|NEEDLE|SPIGOT|URINE BAG|CREPE|PLASTER|LEAD|BLOOD SET)\b|GLOVE",
        "piece",
        "150",
    ),
    (r"\b(INJ|INJECTION|VIAL|AMP|AMPOULE|AMPOULES|AMPULE|AMPULES)\b|INJ\b|INJECTION", "vial", "300"),
    (r"\b(INF|INFUSION|IV)\b|\b(UNISOL|DEXTROSE|SALINE)\b", "bottle", "180"),
    (r"\b(DROP|DROPS|E/D)\b|\bEYE DROP", "bottle", "250"),
    (r"\b(SYP|SYRUP|SUSP|SUSPENSION|SOL|SOLUTION|LINCTUS|ELIXIR|NEB|NEBULISER|NEBULIZER)\b", "bottle", "200"),
    (r"\b(CREAM|GEL|OINT|OINTMENT|LOTION)\b", "tube", "220"),
    (r"\b(LIQUID|LIQD|LIQ|ENEMA|NUBULES|NEBULES|RESPULES)\b|\d+\s?ML\b", "bottle", "200"),
    (r"\b(SACHET|SACHETS)\b", "box", "300"),
    (r"(CAP|CAPS|CAPSULES?|ROTACAPS)\b", "box", "450"),
    (r"(TAB|TABS|TABLETS?)\b|\bTA$", "box", "400"),
    (r"\d+(\.\d+)?\s?(MG|MCG|G)\b|\d+/\d+\s?MG", "box", "400"),  # strength only: tablets
]
DEFAULT_FORM = ("piece", "200")
SUMMARY_ROW = re.compile(r"^((gross|net|grand|sub)\s*)?(total|amt|amount)\b|^gross\b", re.I)  # lines under the list


@dataclass
class ImportResult:
    added: int = 0
    skipped_existing: list[str] = field(default_factory=list)
    duplicates_in_file: list[str] = field(default_factory=list)
    placeholder_price: int = 0
    by_unit: dict[str, int] = field(default_factory=dict)


def guess_form(name: str) -> tuple[str, str]:
    up = name.upper()
    for pattern, unit, price in FORMS:
        if re.search(pattern, up):
            return unit, price
    return DEFAULT_FORM


def _clean(v) -> str:
    return re.sub(r"\s+", " ", str(v or "")).strip()


def _key(name: str) -> str:
    return re.sub(r"[^A-Z0-9%]+", "", name.upper())


def read_rows(path: Path) -> list[dict]:
    """Rows as dicts keyed by our field names; the header row is found anywhere in the first 20 rows."""
    if path.suffix.lower() == ".csv":
        with path.open(newline="", encoding="utf-8-sig") as f:
            raw = [list(r) for r in csv.reader(f)]
    else:
        import openpyxl

        ws = openpyxl.load_workbook(path, read_only=True, data_only=True).active
        raw = [list(r) for r in ws.iter_rows(values_only=True)]
    header_at, cols = None, {}
    for i, row in enumerate(raw[:20]):
        found = {}
        for j, cell in enumerate(row):
            h = _clean(cell).lower()
            for fld, names in HEADERS.items():
                if h in names and fld not in found:
                    found[fld] = j
        if "name" in found:
            header_at, cols = i, found
            break
    if header_at is None:
        raise ValueError("no product name column found (e.g. 'Item Description' or 'Product')")
    rows = []
    for row in raw[header_at + 1 :]:
        get = {fld: (row[j] if j < len(row) else None) for fld, j in cols.items()}
        if _clean(get.get("name")):
            rows.append(get)
    return rows


def _decimal(v) -> Decimal | None:
    try:
        d = Decimal(str(v).replace(",", "").strip())
        return d if d >= 0 else None
    except (InvalidOperation, ValueError):
        return None


def import_products(
    session: Session, tenant: Tenant, path: Path, *, default_stock: Decimal = Decimal(500), expiry_months: int = 24
) -> ImportResult:
    res = ImportResult()
    wh = session.scalar(select(Warehouse).where(Warehouse.tenant_id == tenant.id).order_by(Warehouse.created_at))
    existing = {_key(p.name): p for p in session.scalars(select(Product).where(Product.tenant_id == tenant.id))}
    codes = {p.code for p in existing.values()}
    seen: set[str] = set()
    n = 0
    batch_no = f"OPEN-{date.today():%y%m}"
    for row in read_rows(path):
        name = _clean(row.get("name"))[:200]
        if SUMMARY_ROW.match(name):
            continue  # "Gross Amt.", "Total" ...
        key = _key(name)
        if key in seen:
            res.duplicates_in_file.append(name)
            continue
        seen.add(key)
        if key in existing:
            res.skipped_existing.append(name)
            continue
        unit_guess, price_guess = guess_form(name)
        unit = _clean(row.get("unit")).lower() or unit_guess
        price = _decimal(row.get("price"))
        if price is None:
            price = Decimal(price_guess)
            res.placeholder_price += 1
        code = _clean(row.get("code"))[:40]
        if not code or code in codes:
            while True:
                n += 1
                code = f"M{n:04d}"
                if code not in codes:
                    break
        codes.add(code)
        stock = _decimal(row.get("stock"))
        stock = default_stock if stock is None else stock
        prod = Product(
            tenant_id=tenant.id,
            code=code,
            name=name,
            generic_name=_clean(row.get("generic"))[:200] or None,
            manufacturer=_clean(row.get("manufacturer"))[:200] or None,
            pack_size=_clean(row.get("pack"))[:60] or None,
            sale_unit=unit[:20],
            sale_price=price,
            track_batches=True,
            reorder_level=Decimal(20),
        )
        session.add(prod)
        session.flush()
        if stock > 0:
            b = Batch(
                tenant_id=tenant.id,
                product_id=prod.id,
                batch_no=batch_no,
                expiry_date=date.today() + timedelta(days=30 * expiry_months),
                cost_price=(price * Decimal("0.85")).quantize(Decimal("0.01")),
            )
            session.add(b)
            session.flush()
            inventory.post(
                session,
                tenant_id=tenant.id,
                warehouse_id=wh.id,
                product_id=prod.id,
                batch_id=b.id,
                qty_delta=stock,
                kind="opening",
                source_type="import",
                source_id=uuid.uuid4(),
                reason=f"opening stock from {path.name}",
            )
        existing[key] = prod
        res.added += 1
        res.by_unit[unit] = res.by_unit.get(unit, 0) + 1
    return res
