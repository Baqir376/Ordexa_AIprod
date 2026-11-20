"""Platform CLI: ``ordexa create-tenant``, ``ordexa seed-demo``, ``ordexa migrate``."""

from __future__ import annotations

import argparse
import getpass
import sys
import uuid
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

from sqlalchemy import select

import ordexa.models  # noqa: F401  (register every table so foreign keys resolve)
from ordexa.modules.catalogue.models import Batch, Product
from ordexa.modules.customers.models import Customer
from ordexa.modules.identity.models import User
from ordexa.modules.identity.service import Principal, effective_permissions
from ordexa.modules.inventory import service as inventory
from ordexa.modules.purchasing.models import Supplier
from ordexa.modules.tenants.models import Tenant, Warehouse
from ordexa.modules.tenants.service import create_tenant
from ordexa.shared.db import get_sessionmaker
from ordexa.shared.errors import OrdexaError

# Synthetic pharma demo catalogue: (code, name, generic, manufacturer, pack, unit, price, [(batch, months_to_expiry, qty)])
DEMO_PRODUCTS = [
    ("P001", "Panadol 500mg Tablets", "Paracetamol", "GSK", "20x10", "box", "450.00", [("PN2401", 4, 40), ("PN2407", 14, 120)]),
    ("P002", "Panadol Extra Tablets", "Paracetamol + Caffeine", "GSK", "20x10", "box", "620.00", [("PX2403", 10, 60)]),
    ("P003", "Brufen 400mg Tablets", "Ibuprofen", "Abbott", "10x10", "box", "380.00", [("BF2311", 2, 15), ("BF2405", 18, 80)]),
    ("P004", "Augmentin 625mg Tablets", "Co-amoxiclav", "GSK", "6 tabs", "pack", "780.00", [("AU2402", 9, 50)]),
    ("P005", "Risek 20mg Capsules", "Omeprazole", "Getz", "14 caps", "pack", "510.00", [("RS2404", 12, 70)]),
    ("P006", "Calpol Syrup 120ml", "Paracetamol", "GSK", "120ml", "bottle", "145.00", [("CP2402", 7, 100)]),
    ("P007", "Arinac Forte Tablets", "Ibuprofen + Pseudoephedrine", "Abbott", "10x10", "box", "690.00", [("AR2406", 16, 30)]),
    ("P008", "Flagyl 400mg Tablets", "Metronidazole", "Sanofi", "20x10", "box", "560.00", [("FG2405", 15, 45)]),
    ("P009", "ORS Sachet", "Oral rehydration salts", "Searle", "20 sachets", "box", "300.00", [("OR2408", 20, 90)]),
    ("P010", "Surgical Mask (50)", None, "Generic", "50 pcs", "box", "350.00", []),
]
DEMO_SUPPLIERS = [
    ("S01", "Al-Habib Pharma Distributors", "042-111000001"),
    ("S02", "Premier Medical Agencies", "042-111000002"),
]
DEMO_CUSTOMERS = [
    ("101", "Madina Medical Store", "0300-1110101", "50000.00"),
    ("102", "Shifa Pharmacy", "0300-1110102", "80000.00"),
    ("103", "Al-Rehman Chemist", "0300-1110103", "30000.00"),
    ("104", "City Care Pharmacy", "0300-1110104", None),
    ("105", "Fazal Din Medicos", "0300-1110105", "100000.00"),
]


def admin_principal(session, tenant: Tenant) -> Principal:
    u = session.get(User, tenant.owner_user_id)
    return Principal(u.id, tenant.id, u.username, u.full_name, u.role, effective_permissions(session, u))


def seed_demo(session, tenant: Tenant) -> dict:
    """Idempotent: adds only products/customers that do not exist yet."""
    wh = session.scalar(select(Warehouse).where(Warehouse.tenant_id == tenant.id).order_by(Warehouse.created_at))
    added_p = added_c = 0
    for code, name, generic, mfr, pack, unit, price, batches in DEMO_PRODUCTS:
        if session.scalar(select(Product).where(Product.tenant_id == tenant.id, Product.code == code)):
            continue
        prod = Product(
            reorder_level=Decimal(20),
            tenant_id=tenant.id,
            code=code,
            name=name,
            generic_name=generic,
            manufacturer=mfr,
            pack_size=pack,
            sale_unit=unit,
            sale_price=Decimal(price),
            track_batches=bool(batches),
        )
        session.add(prod)
        session.flush()
        added_p += 1
        for batch_no, months, q in batches:
            b = Batch(
                tenant_id=tenant.id,
                product_id=prod.id,
                batch_no=batch_no,
                expiry_date=date.today() + timedelta(days=30 * months),
                cost_price=(Decimal(price) * Decimal("0.85")).quantize(Decimal("0.01")),
            )
            session.add(b)
            session.flush()
            inventory.post(
                session,
                tenant_id=tenant.id,
                warehouse_id=wh.id,
                product_id=prod.id,
                batch_id=b.id,
                qty_delta=Decimal(q),
                kind="opening",
                source_type="seed",
                source_id=uuid.uuid4(),
                reason="demo opening stock",
            )
        if not batches:
            inventory.post(
                session,
                tenant_id=tenant.id,
                warehouse_id=wh.id,
                product_id=prod.id,
                batch_id=None,
                qty_delta=Decimal(200),
                kind="opening",
                source_type="seed",
                source_id=uuid.uuid4(),
                reason="demo opening stock",
            )
    for code, name, phone, limit in DEMO_CUSTOMERS:
        if session.scalar(select(Customer).where(Customer.tenant_id == tenant.id, Customer.code == code)):
            continue
        session.add(
            Customer(tenant_id=tenant.id, code=code, name=name, phone=phone, credit_limit=Decimal(limit) if limit else None)
        )
        added_c += 1
    for code, name, phone in DEMO_SUPPLIERS:
        if not session.scalar(select(Supplier).where(Supplier.tenant_id == tenant.id, Supplier.code == code)):
            session.add(Supplier(tenant_id=tenant.id, code=code, name=name, phone=phone))
    return {"products": added_p, "customers": added_c}


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="ordexa")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("migrate", help="apply database migrations (alembic upgrade head)")
    ct = sub.add_parser("create-tenant", help="create a business with its owner Administrator")
    ct.add_argument("--code", required=True)
    ct.add_argument("--name", required=True)
    ct.add_argument("--admin", default="admin")
    ct.add_argument("--password", help="omit to be prompted")
    ct.add_argument("--plan", default="basic", choices=["basic", "advance", "plus"])
    ct.add_argument("--devices", type=int, default=2)
    sd = sub.add_parser("seed-demo", help="load synthetic pharma demo data into a business")
    sd.add_argument("--code", required=True)
    args = parser.parse_args(argv)

    if args.cmd == "migrate":
        from alembic import command
        from alembic.config import Config

        root = Path.cwd() if (Path.cwd() / "alembic.ini").exists() else Path(__file__).resolve().parents[2]
        cfg = Config(str(root / "alembic.ini"))
        cfg.set_main_option("script_location", str(root / "migrations"))
        command.upgrade(cfg, "head")
        print("database is up to date")
        return

    with get_sessionmaker()() as session:
        try:
            if args.cmd == "create-tenant":
                password = args.password or getpass.getpass("Administrator password: ")
                t = create_tenant(
                    session,
                    code=args.code,
                    name=args.name,
                    admin_username=args.admin,
                    admin_password=password,
                    license_plan=args.plan,
                    max_devices=args.devices,
                )
                session.commit()
                print(f"created business '{t.code}' ({t.name}); administrator '{args.admin}'")
            elif args.cmd == "seed-demo":
                t = session.scalar(select(Tenant).where(Tenant.code == args.code))
                if not t:
                    sys.exit(f"unknown business code '{args.code}'")
                counts = seed_demo(session, t)
                session.commit()
                print(f"seeded {counts['products']} products and {counts['customers']} customers into '{t.code}'")
        except OrdexaError as e:
            sys.exit(f"error: {e.message}")


if __name__ == "__main__":
    main()
