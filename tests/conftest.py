"""Shared fixtures.

Unit tests need nothing external. Integration tests need PostgreSQL; they run
against a separate ``*_test`` database that is created and re-created on
demand, so the development database is never touched. Redis is optional: the
cache layer fails open, and the tests only flush it when it is reachable.
"""

from __future__ import annotations

import base64
import os
import secrets
from collections.abc import AsyncIterator
from urllib import parse

import httpx
import pytest
import redis.asyncio as redis
import sqlalchemy as sa
from sqlalchemy.ext import asyncio as sa_asyncio

from app.core import config
from app.db import models

DEFAULT_DB_URL = "postgresql+asyncpg://masking:masking@127.0.0.1:5432/masking"
_TRUNCATE_SQL = (
    "TRUNCATE calls, session_parties, sessions, numbers "
    "RESTART IDENTITY CASCADE"
)


def _test_database_url() -> str:
    """Returns the URL of the throwaway test database."""
    url = os.environ.get("TEST_DATABASE_URL")
    if url:
        return url
    base = os.environ.get("DATABASE_URL", DEFAULT_DB_URL)
    parts = parse.urlsplit(base)
    name = parts.path.lstrip("/") or "masking"
    if not name.endswith("_test"):
        name = f"{name}_test"
    return parse.urlunsplit(
        (parts.scheme, parts.netloc, f"/{name}", parts.query, parts.fragment)
    )


@pytest.fixture(scope="session")
def test_settings() -> config.Settings:
    """Settings pinned to the test database, ignoring any developer .env."""
    return config.Settings(
        _env_file=None,
        database_url=_test_database_url(),
        redis_url=os.environ.get("REDIS_URL", "redis://127.0.0.1:6379/15"),
        party_hash_secret=secrets.token_urlsafe(48),
        encryption_key=base64.b64encode(os.urandom(32)).decode(),
        api_keys="test-key",
        number_cooldown_hours=24,
        default_ttl_seconds=3600,
        run_ari_in_api=False,
        log_json=False,
    )


async def _ensure_database(url: str) -> bool:
    """Creates the test database if needed; False when PostgreSQL is down."""
    parts = parse.urlsplit(url)
    db_name = parts.path.lstrip("/")
    admin_url = parse.urlunsplit(
        (parts.scheme, parts.netloc, "/postgres", "", "")
    )

    engine = sa_asyncio.create_async_engine(
        admin_url, isolation_level="AUTOCOMMIT"
    )
    try:
        async with engine.connect() as conn:
            exists = await conn.scalar(
                sa.text("SELECT 1 FROM pg_database WHERE datname = :name"),
                {"name": db_name},
            )
            if not exists:
                await conn.execute(sa.text(f'CREATE DATABASE "{db_name}"'))
        return True
    except Exception:
        return False
    finally:
        await engine.dispose()


async def _flush_cache(settings: config.Settings) -> None:
    """Truncating the database behind the cache would leave stale routes."""
    client = redis.from_url(settings.redis_url, decode_responses=True)
    try:
        await client.flushdb()
    except Exception:
        pass
    finally:
        await client.aclose()


async def _reset_state(engine, settings: config.Settings) -> None:
    """Empties every table and the cache before a test runs."""
    async with engine.begin() as conn:
        await conn.execute(sa.text(_TRUNCATE_SQL))
    await _flush_cache(settings)


@pytest.fixture(scope="session")
async def db_engine(test_settings: config.Settings):
    """Creates the schema once per session and yields the engine."""
    from app.db import triggers

    if not await _ensure_database(test_settings.database_url):
        pytest.skip("PostgreSQL is not reachable — integration tests skipped")

    engine = sa_asyncio.create_async_engine(test_settings.database_url)
    async with engine.begin() as conn:
        await conn.execute(sa.text("CREATE EXTENSION IF NOT EXISTS pgcrypto"))
        await conn.execute(sa.text(triggers.DROP_TRIGGER))
        await conn.run_sync(models.Base.metadata.drop_all)
        await conn.run_sync(models.Base.metadata.create_all)
        await conn.execute(sa.text(triggers.SYNC_FUNCTION))
        await conn.execute(sa.text(triggers.SYNC_TRIGGER))
    yield engine
    await engine.dispose()


@pytest.fixture
async def db(
    db_engine, test_settings: config.Settings
) -> AsyncIterator[sa_asyncio.AsyncSession]:
    """Yields a session against a freshly emptied database."""
    from app.db import cache
    from app.db import engine as engine_module

    await _reset_state(db_engine, test_settings)

    factory = sa_asyncio.async_sessionmaker(db_engine, expire_on_commit=False)
    engine_module._engine = db_engine
    engine_module._sessionmaker = factory
    cache.init_cache(test_settings)

    async with factory() as session:
        yield session
        await session.rollback()


@pytest.fixture
async def api_client(db_engine, test_settings: config.Settings, monkeypatch):
    """Yields an HTTP client bound to the app and the test database."""
    from app.core import config as config_module
    from app.db import cache
    from app.db import engine as engine_module

    await _reset_state(db_engine, test_settings)

    monkeypatch.setattr(config_module, "get_settings", lambda: test_settings)

    from app import main as main_module

    engine_module._engine = db_engine
    engine_module._sessionmaker = sa_asyncio.async_sessionmaker(
        db_engine, expire_on_commit=False
    )
    cache.init_cache(test_settings)

    api = main_module.create_app()

    transport = httpx.ASGITransport(app=api)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://testserver",
        headers={"X-API-Key": "test-key"},
    ) as client:
        yield client
