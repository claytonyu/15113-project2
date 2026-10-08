"""Fixtures for the Google integration tests.

These tests run the real backend (in-process) against a real Postgres database, with Google replaced
by `FakeGoogle`. They are OFF by default because they write to the database in DATABASE_URL (each test
uses its own throwaway user, whose google_id starts with "itest-", and removes it afterwards).

    RUN_INTEGRATION_TESTS=1 python -m pytest tests/integration

Point DATABASE_URL at a development database (a Neon dev branch), not production.
"""
from __future__ import annotations

import hashlib
import os
import sys
import uuid
from pathlib import Path
from urllib.parse import urlsplit

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).parent))  # lets tests `import fake_google` / `import frontend`

from fake_google import CLIENT_ID, CLIENT_SECRET, FakeGoogle  # noqa: E402
from frontend import Frontend  # noqa: E402

ENABLED = os.environ.get("RUN_INTEGRATION_TESTS") == "1"
SUB_PREFIX = "itest-"
BACKEND_URL = "http://localhost:8000"
FRONTEND_ORIGIN = "http://localhost:5500"


def pytest_report_header(config):
    if not ENABLED:
        return "integration tests: skipped (set RUN_INTEGRATION_TESTS=1 to run them)"
    try:
        from dotenv import dotenv_values

        url = os.environ.get("DATABASE_URL") or dotenv_values(".env").get("DATABASE_URL") or ""
        return f"integration tests: ON, writing to database host {urlsplit(url).hostname or '(none)'}"
    except Exception:  # pragma: no cover
        return "integration tests: ON"


def _purge(conn) -> None:
    conn.execute("DELETE FROM users WHERE google_id LIKE %s", (SUB_PREFIX + "%",))


@pytest.fixture(scope="session", autouse=True)
def integration_env():
    if not ENABLED:
        pytest.skip("integration tests are off; set RUN_INTEGRATION_TESTS=1 (they write to the database)")
    from cryptography.fernet import Fernet

    patch = pytest.MonkeyPatch()
    overrides = {
        "ENVIRONMENT": "local",
        "BACKEND_URL": BACKEND_URL,  # http, so the login cookie is not marked Secure
        "FRONTEND_ORIGIN": FRONTEND_ORIGIN,
        "GOOGLE_CLIENT_ID": CLIENT_ID,
        "GOOGLE_CLIENT_SECRET": CLIENT_SECRET,
        "TOKEN_ENCRYPTION_KEY": Fernet.generate_key().decode(),
        "AUTH_RATE_LIMIT": "100000/minute",
        "SCHEDULE_RATE_LIMIT": "100000/minute",
        "TRUSTED_PROXY_HOPS": "0",
    }
    for key, value in overrides.items():
        patch.setenv(key, value)
    patch.delenv("GOOGLE_REDIRECT_URI", raising=False)

    from app import db, db_init
    from app.config import get_config

    get_config.cache_clear()
    cfg = get_config()
    if cfg.is_production or not cfg.database_url:
        reason = "refusing to run with ENVIRONMENT=production" if cfg.is_production else "DATABASE_URL is not set"
        patch.undo()
        get_config.cache_clear()
        pytest.skip(reason)

    db_init.main()  # create any missing tables (safe to re-run)
    with db.connect() as conn:
        _purge(conn)
    yield cfg
    with db.connect() as conn:
        _purge(conn)
    db.close_pool()
    patch.undo()
    get_config.cache_clear()


@pytest.fixture
def app(integration_env):
    from app.main import app as fastapi_app

    return fastapi_app


@pytest.fixture
def google(integration_env, monkeypatch):
    """Google is replaced by FakeGoogle for the length of one test."""
    from app import google as backend_google

    fake = FakeGoogle(redirect_uri=f"{BACKEND_URL}/auth/google/callback")
    monkeypatch.setattr(backend_google, "_client", httpx.Client(transport=httpx.MockTransport(fake.handle)))
    backend_google._access_cache.clear()
    yield fake
    backend_google._access_cache.clear()
    assert fake.violations == [], f"the backend misbehaved towards Google: {fake.violations}"


@pytest.fixture
def make_frontend(app):
    return lambda: Frontend(app, FRONTEND_ORIGIN, BACKEND_URL)


@pytest.fixture
def frontend(make_frontend):
    return make_frontend()


@pytest.fixture
def database(integration_env):
    """Tiny helper to look at what the backend stored."""
    from app import db

    class Database:
        def one(self, sql, params=()):
            with db.connect() as conn:
                return conn.execute(sql, params).fetchone()

        def all(self, sql, params=()):
            with db.connect() as conn:
                return conn.execute(sql, params).fetchall()

        def count(self, table, user_id):
            return self.one(f"SELECT count(*) AS n FROM {table} WHERE user_id = %s", (user_id,))["n"]

        def user_id(self, sub):
            row = self.one("SELECT id FROM users WHERE google_id = %s", (sub,))
            return row["id"] if row else None

    return Database()


@pytest.fixture
def signed_in(google, make_frontend, database):
    """Factory: a Google account with calendars, already logged in. Cleans up its user afterwards."""
    created: list[str] = []

    def factory(calendars=None, email=None, tz="America/New_York"):
        sub = SUB_PREFIX + uuid.uuid4().hex[:12]
        created.append(sub)
        google.add_account(
            sub,
            email or f"{sub}@example.com",
            calendars
            if calendars is not None
            else [
                {"id": "primary@test", "name": "Me", "primary": True, "tz": tz},
                {"id": "classes@test", "name": "Classes", "tz": tz},
            ],
        )
        fe = make_frontend()
        result = fe.login(google, sub)
        assert result.error is None, result.error
        fe.sub = sub
        fe.google = google
        return fe

    yield factory
    from app import db

    with db.connect() as conn:
        for sub in created:
            conn.execute("DELETE FROM users WHERE google_id = %s", (sub,))


def sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()
