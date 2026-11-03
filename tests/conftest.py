"""Shared test fixtures.

Tests run against real PostgreSQL. If TEST_DATABASE_URL is set it is used
(e.g. in CI or against the compose db); otherwise an embedded Postgres 16
is started via the `pgserver` dev dependency.
"""

from __future__ import annotations

import os
import tempfile
import uuid
from collections.abc import Iterator

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

# Settings must be valid before any app import.
os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("LLM_BACKEND", "fake")
os.environ.setdefault("OWNER_WHATSAPP_NUMBER", "+18045550100")
os.environ.setdefault("WHATSAPP_APP_SECRET", "test-app-secret")
os.environ.setdefault("WHATSAPP_VERIFY_TOKEN", "test-verify-token")
os.environ.setdefault("MESSAGING_BACKEND", "console")
os.environ.setdefault("FILE_STORAGE_DIR", tempfile.mkdtemp(prefix="kfiles"))
os.environ.setdefault("KINGS_TESTS", "1")
os.environ.setdefault("GMAIL_BACKEND", "fake")
os.environ.setdefault("AMAZON_BACKEND", "fake")
os.environ.setdefault("ADS_BACKEND", "fake")
os.environ.setdefault("AMAZON_SELLER_ID", "A2TESTSELLER1")
os.environ.setdefault("PUBLIC_MEDIA_BACKEND", "fake")
os.environ.setdefault("TOKEN_ENCRYPTION_KEY", "hWgOkg6xqyH0Z0tXL7JZ0l0aBv3b8b3sXq2m5m2Yk0I=")


@pytest.fixture(scope="session")
def pg_admin_url() -> Iterator[str]:
    url = os.environ.get("TEST_DATABASE_URL")
    if url:
        yield url
        return
    import logging

    import pgserver

    # pgserver logs from an atexit hook after pytest closes its streams.
    pg_log = logging.getLogger("pgserver")
    pg_log.setLevel(logging.WARNING)
    pg_log.propagate = False

    srv = pgserver.get_server(tempfile.mkdtemp(prefix="kpg"), cleanup_mode="stop")
    yield srv.get_uri().replace("postgresql://", "postgresql+psycopg://", 1)


@pytest.fixture(scope="session")
def database_url(pg_admin_url: str) -> Iterator[str]:
    """A fresh, fully migrated database for the test session."""
    name = f"test_{uuid.uuid4().hex[:10]}"
    admin = create_engine(pg_admin_url, isolation_level="AUTOCOMMIT")
    with admin.connect() as c:
        c.execute(text(f'CREATE DATABASE "{name}"'))
    url = make_url(pg_admin_url).set(database=name).render_as_string(hide_password=False)
    os.environ["DATABASE_URL"] = url

    from app.config import get_settings
    from app.db.session import get_engine

    get_settings.cache_clear()
    get_engine.cache_clear()
    run_migrations(url)
    # Quiet hours off by default: otherwise every test that expects a normal (non-urgent) message
    # fails when the suite happens to run in the evening. Tests that exercise quiet hours set them.
    with create_engine(url).begin() as c:
        c.execute(text("UPDATE settings SET value = 'null'::jsonb WHERE key = 'quiet_hours'"))
    yield url

    get_engine().dispose()
    get_engine.cache_clear()
    with admin.connect() as c:
        c.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
    admin.dispose()


def run_migrations(url: str, target: str = "head") -> None:
    from alembic import command
    from alembic.config import Config

    cfg = Config("alembic.ini")
    cfg.attributes["database_url"] = url
    cfg.attributes["skip_logging"] = True
    command.upgrade(cfg, target)


_KEEP_TABLES = {"agents", "settings", "alembic_version", "email_alert_rules"}


def _reseed_amazon(conn) -> None:
    """Brands and the default ship-from address come from migration 0008; TRUNCATE removes them."""
    from app.db.models import Brand, ShipFromAddress
    from app.db.seed_data import BRANDS, SHIP_FROM_ADDRESSES

    conn.execute(Brand.__table__.insert(), BRANDS)
    conn.execute(ShipFromAddress.__table__.insert(), SHIP_FROM_ADDRESSES)


@pytest.fixture
def db(database_url):
    """A session on the migrated test DB. All data is wiped after each test."""
    from app.db.models import Agent, Base, Setting
    from app.db.seed_data import AGENTS, DEFAULT_SETTINGS
    from app.db.session import get_engine, get_sessionmaker

    session = get_sessionmaker()()
    try:
        yield session
    finally:
        session.rollback()
        session.close()
        tables = [t for t in Base.metadata.tables if t not in _KEEP_TABLES]
        with get_engine().begin() as conn:
            conn.execute(text(f"TRUNCATE {', '.join(tables)} RESTART IDENTITY CASCADE"))
            for seq in ("task_ref_seq", "approval_ref_seq", "error_ref_seq"):
                conn.execute(text(f"ALTER SEQUENCE {seq} RESTART WITH 1"))
            conn.execute(text("DELETE FROM email_alert_rules WHERE created_by <> 'system'"))
            conn.execute(text("UPDATE email_alert_rules SET is_active = true"))
            for seq in ("email_thread_ref_seq", "email_draft_ref_seq", "amazon_listing_ref_seq",
                        "amazon_change_ref_seq", "amazon_alert_ref_seq", "fba_request_ref_seq"):
                conn.execute(text(f"ALTER SEQUENCE {seq} RESTART WITH 1"))
            _reseed_amazon(conn)
            for a in AGENTS:
                conn.execute(Agent.__table__.update().where(Agent.key == a["key"]).values(status=a["status"]))
            conn.execute(Setting.__table__.delete().where(Setting.key.not_in(list(DEFAULT_SETTINGS))))
            for k, v in DEFAULT_SETTINGS.items():
                conn.execute(Setting.__table__.update().where(Setting.key == k)
                             .values(value=None if k == "quiet_hours" else v))


@pytest.fixture
def owner(db):
    from app.services.users import ensure_owner

    user = ensure_owner(db)
    db.commit()
    return user


@pytest.fixture(autouse=True)
def _reset_fakes():
    from app.amazon.drive import FakeDriveClient
    from app.amazon.fake import FakeAmazonClient
    from app.services.media import FakeMediaStore
    from app.channels.console import ConsoleChannel
    from app.email.fake_gmail import FakeGmailClient
    from app.llm.fake import FakeLLMProvider
    from app.ppc.fake import FakeAdsClient

    from app.amazon import fees as amazon_fees

    for fake in (FakeLLMProvider, ConsoleChannel, FakeGmailClient, FakeAmazonClient, FakeMediaStore,
                 FakeDriveClient, FakeAdsClient):
        fake.reset()
    # The fee estimate's own limit is 0.5 requests a second, which adds twenty seconds of real
    # sleeping to one test file. The general throttles are left alone: tests assert on those.
    amazon_fees.FEE_THROTTLE.interval = 0
    yield
    for fake in (FakeLLMProvider, ConsoleChannel, FakeGmailClient, FakeAmazonClient, FakeMediaStore,
                 FakeDriveClient, FakeAdsClient):
        fake.reset()


@pytest.fixture
def sent():
    """Messages recorded by the console channel."""
    from app.channels.console import ConsoleChannel

    return ConsoleChannel.sent


@pytest.fixture
def no_quiet_hours(db):
    from app.services import settings_store
    from app.services.common import SYSTEM

    settings_store.set_value(db, "quiet_hours", None, SYSTEM)
    db.commit()


def run_jobs(types=None, uses_llm=None, limit=50) -> int:
    """Run ready jobs (optionally only some types). Returns how many ran."""
    from app.jobs.worker import Worker

    w, n = Worker(), 0
    while n < limit and w.run_one(uses_llm=uses_llm, only_types=types):
        n += 1
    return n
