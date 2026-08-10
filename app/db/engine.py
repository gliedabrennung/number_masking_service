"""Async engine and session factory shared by the API and the ARI application.

The engine is process-wide mutable state, which the style guide asks to
justify: a connection pool is exactly the kind of resource that must be created
once and shared, and both entry points (:mod:`app.main` and
:mod:`app.ari_main`) need the same pool. Access goes through the functions
below, never through the module attributes directly.
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator

from sqlalchemy.ext import asyncio as sa_asyncio

from app.core import config

_POOL_SIZE = 10
_MAX_OVERFLOW = 10

_engine: sa_asyncio.AsyncEngine | None = None
_sessionmaker: sa_asyncio.async_sessionmaker[sa_asyncio.AsyncSession] | None = (
    None
)


def init_engine(settings: config.Settings) -> sa_asyncio.AsyncEngine:
    """Creates the engine and session factory once per process.

    Args:
        settings: Application settings holding ``database_url``.

    Returns:
        The process-wide engine, existing or freshly created.
    """
    global _engine, _sessionmaker
    if _engine is None:
        _engine = sa_asyncio.create_async_engine(
            settings.database_url,
            pool_size=_POOL_SIZE,
            max_overflow=_MAX_OVERFLOW,
            pool_pre_ping=True,
            echo=False,
        )
        _sessionmaker = sa_asyncio.async_sessionmaker(
            _engine, expire_on_commit=False
        )
    return _engine


def get_sessionmaker() -> sa_asyncio.async_sessionmaker[
    sa_asyncio.AsyncSession
]:
    """Returns the session factory.

    Raises:
        RuntimeError: :func:`init_engine` has not been called yet.
    """
    if _sessionmaker is None:
        raise RuntimeError(
            "database engine is not initialised; call init_engine() first"
        )
    return _sessionmaker


@contextlib.asynccontextmanager
async def session_scope() -> AsyncIterator[sa_asyncio.AsyncSession]:
    """Yields a session that commits on success and rolls back on error."""
    factory = get_sessionmaker()
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def dispose_engine() -> None:
    """Closes every pooled connection and forgets the engine."""
    global _engine, _sessionmaker
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _sessionmaker = None
