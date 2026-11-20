"""Tests run against a real PostgreSQL database (``ordexa_test``) created through the Alembic migrations,
so the schema under test is exactly the one production gets."""

from __future__ import annotations

import os

BASE_URL = os.environ.get("ORDEXA_TEST_DATABASE_URL", "postgresql+psycopg://ordexa:ordexa@localhost:5432/ordexa_test")
os.environ["ORDEXA_DATABASE_URL"] = BASE_URL
os.environ["ORDEXA_AI_PROVIDER"] = "rules"  # tests never call a live LLM
os.environ["ORDEXA_WHATSAPP_TOKEN"] = ""  # outbound WhatsApp is simulated

from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select, text
from sqlalchemy.engine import make_url

from ordexa.apps.api import create_app
from ordexa.cli import seed_demo
from ordexa.modules.tenants.models import Tenant
from ordexa.modules.tenants.service import create_tenant
from ordexa.shared.db import Base, get_sessionmaker

ROOT = Path(__file__).resolve().parents[1]
PASSWORD = "secret-pass-1"


@pytest.fixture(scope="session", autouse=True)
def database():
    url = make_url(BASE_URL)
    admin = create_engine(url.set(database="postgres"), isolation_level="AUTOCOMMIT")
    with admin.connect() as c:
        if not c.scalar(text("SELECT 1 FROM pg_database WHERE datname = :n"), {"n": url.database}):
            c.execute(text(f'CREATE DATABASE "{url.database}"'))
    admin.dispose()
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    command.upgrade(cfg, "head")
    yield


@pytest.fixture(autouse=True)
def clean(database):
    yield
    import ordexa.models  # noqa: F401

    names = ", ".join(t.name for t in Base.metadata.sorted_tables)
    with get_sessionmaker()() as s:
        s.execute(text(f"TRUNCATE {names} CASCADE"))
        s.commit()


@pytest.fixture
def client():
    return TestClient(create_app())


class Biz:
    """A seeded test business with helpers for authenticated calls."""

    def __init__(self, client: TestClient, code: str):
        self.client, self.code = client, code
        self.token = self.login("admin")
        self.headers = {"Authorization": f"Bearer {self.token}"}
        self.device_headers: dict[str, dict] = {}

    def login(self, username: str, password: str = PASSWORD) -> str:
        r = self.client.post("/api/v1/auth/login", json={"tenant_code": self.code, "username": username, "password": password})
        assert r.status_code == 200, r.text
        return r.json()["access_token"]

    def get(self, path, **kw):
        return self.client.get(path, headers=kw.pop("headers", self.headers), **kw)

    def post(self, path, **kw):
        return self.client.post(path, headers=kw.pop("headers", self.headers), **kw)

    def put(self, path, **kw):
        return self.client.put(path, headers=kw.pop("headers", self.headers), **kw)

    def activate(self, name: str) -> dict:
        r = self.post("/api/v1/devices/activate", json={"name": name})
        assert r.status_code == 201, r.text
        return r.json()

    def dev_headers(self, device: dict) -> dict:
        return {**self.headers, "X-Device-Id": device["id"]}


@pytest.fixture
def make_biz(client):
    def _make(code: str = "ali-chemist", name: str = "Ali Chemist", devices: int = 3, seed: bool = True) -> Biz:
        with get_sessionmaker()() as s:
            create_tenant(s, code=code, name=name, admin_username="admin", admin_password=PASSWORD, max_devices=devices)
            if seed:
                seed_demo(s, s.scalar(select(Tenant).where(Tenant.code == code)))
            s.commit()
        return Biz(client, code)

    return _make
