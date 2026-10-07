"""Test fixtures.

Database tests need a PostgreSQL server: set TEST_DATABASE_URL to a database
URL whose database may be dropped and recreated (e.g.
postgresql+psycopg://graintime:pg@127.0.0.1:15432/graintime_test). Tests that
need it are skipped when it is not set.
"""

from __future__ import annotations

import os

import pytest
from cryptography.fernet import Fernet

TEST_DB = os.environ.get("TEST_DATABASE_URL")
if TEST_DB:
    os.environ["GRAINTIME_DATABASE_URL"] = TEST_DB
os.environ.setdefault("GRAINTIME_ENCRYPTION_KEY", Fernet.generate_key().decode())

needs_db = pytest.mark.skipif(not TEST_DB, reason="TEST_DATABASE_URL not set")


@pytest.fixture(scope="session")
def migrated_db():
    if not TEST_DB:
        pytest.skip("TEST_DATABASE_URL not set")
    from sqlalchemy import create_engine, text
    from sqlalchemy.engine import make_url

    url = make_url(TEST_DB)
    admin = create_engine(url.set(database="postgres"), isolation_level="AUTOCOMMIT")
    with admin.connect() as c:
        c.execute(text(f'DROP DATABASE IF EXISTS "{url.database}" WITH (FORCE)'))
        c.execute(text(f'CREATE DATABASE "{url.database}"'))
    admin.dispose()

    from alembic import command
    from alembic.config import Config

    here = os.path.dirname(os.path.dirname(__file__))
    cfg = Config(os.path.join(here, "alembic.ini"))
    cfg.set_main_option("script_location", os.path.join(here, "migrations"))
    command.upgrade(cfg, "head")
    yield


@pytest.fixture
def db_clean(migrated_db):
    from sqlalchemy import text

    from graintime.common.db import get_engine

    import json

    from graintime.common.profiles import COMPUWEIGH_GMS
    with get_engine().begin() as c:
        c.execute(text("TRUNCATE dashboards, audit_log, collector_jobs, sessions, tickets, site_collector_state, "
                       "sites, mapping_profiles, users, app_settings RESTART IDENTITY CASCADE"))
        c.execute(text("INSERT INTO mapping_profiles (name, description, config) "
                       "VALUES ('CompuWeigh GMS', 'seeded', CAST(:cfg AS jsonb))"),
                  {"cfg": json.dumps(COMPUWEIGH_GMS)})
    from graintime.api.security import throttle
    throttle.failures.clear()
    yield


@pytest.fixture
def client(db_clean):
    from fastapi.testclient import TestClient

    from graintime.api.main import app

    with TestClient(app, headers={"X-GrainTime": "1"}) as c:
        yield c


@pytest.fixture
def admin_client(client):
    r = client.post("/api/setup/admin", json={"username": "admin", "display_name": "Admin",
                                              "password": "correct-horse-battery"})
    assert r.status_code == 200, r.text
    return client


@pytest.fixture
def make_user(db_clean):
    from graintime.api.security import hash_password
    from graintime.common.db import get_sessionmaker
    from graintime.common.models import User

    def _make(username, role, password="viewer-password-123"):
        with get_sessionmaker()() as db:
            db.add(User(username=username, display_name=username, role=role, auth_source="local",
                        password_hash=hash_password(password), is_active=True))
            db.commit()
        return password
    return _make


SITE = {"name": "Celina", "code": "celina", "host": "10.20.30.40", "port": 1433,
        "database": "CompuWeigh", "username": "graintime_ro", "password": "S3cret-site-pw!",
        "encrypt": "yes", "trust_server_certificate": True}
