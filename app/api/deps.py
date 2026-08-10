"""FastAPI dependencies: authentication, rate limiting, database session."""

from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator

import fastapi
from sqlalchemy.ext import asyncio as sa_asyncio

from app.core import config, crypto, errors
from app.db import cache, engine

_KEY_ID_LENGTH = 16


def settings_dep() -> config.Settings:
    """Returns the application settings, as a FastAPI dependency."""
    return config.get_settings()


async def db_session() -> AsyncIterator[sa_asyncio.AsyncSession]:
    """Yields one transaction per request: commit on success, roll back else."""
    factory = engine.get_sessionmaker()
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def require_api_key(
    request: fastapi.Request,
    x_api_key: str | None = fastapi.Header(default=None, alias="X-API-Key"),
    settings: config.Settings = fastapi.Depends(settings_dep),
) -> str:
    """Authenticates the caller and counts the request against its quota.

    Args:
        request: Incoming request; the key identifier is stashed on its state.
        x_api_key: Value of the ``X-API-Key`` header.
        settings: Application settings holding the accepted keys.

    Returns:
        An opaque identifier of the key, safe to log.

    Raises:
        UnauthorizedError: The header is missing or the key is unknown.
        RateLimitedError: The key exceeded its per-minute quota.
    """
    if not x_api_key:
        raise errors.UnauthorizedError("X-API-Key header is required")
    if not any(
        crypto.constant_time_equals(x_api_key, known)
        for known in settings.api_key_set
    ):
        raise errors.UnauthorizedError("unknown API key")

    key_id = hashlib.sha256(x_api_key.encode()).hexdigest()[:_KEY_ID_LENGTH]
    request.state.api_key_id = key_id

    allowed, count = await cache.rate_limit_hit(
        key_id, settings.api_rate_limit_per_minute
    )
    if not allowed:
        raise errors.RateLimitedError(
            f"rate limit of {settings.api_rate_limit_per_minute} req/min"
            " exceeded"
        )
    request.state.rate_count = count
    return key_id
