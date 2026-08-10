"""Redis: hot lookup cache, API rate limiting.

Redis is an accelerator only. Every value here is reconstructible from
PostgreSQL, so an outage degrades latency, not correctness — which is why every
function in this module fails open and merely logs.

The client is process-wide mutable state for the same reason as the database
engine: it owns a connection pool that must be shared.
"""

from __future__ import annotations

import json
from typing import Any

import redis.asyncio as redis

from app.core import config, logging_config

log = logging_config.get_logger(__name__)

LOOKUP_TTL_SECONDS = 300
NEGATIVE_TTL_SECONDS = 15
_SCAN_BATCH = 500

_client: redis.Redis | None = None


def init_cache(settings: config.Settings) -> redis.Redis:
    """Creates the Redis client once per process.

    Args:
        settings: Application settings holding ``redis_url``.

    Returns:
        The process-wide client, existing or freshly created.
    """
    global _client
    if _client is None:
        _client = redis.from_url(settings.redis_url, decode_responses=True)
    return _client


def get_cache() -> redis.Redis:
    """Returns the Redis client.

    Raises:
        RuntimeError: :func:`init_cache` has not been called yet.
    """
    if _client is None:
        raise RuntimeError(
            "redis client is not initialised; call init_cache() first"
        )
    return _client


async def close_cache() -> None:
    """Closes the Redis connection pool."""
    global _client
    if _client is not None:
        await _client.aclose()
    _client = None


def lookup_key(proxy_e164: str, caller_hash: str) -> str:
    """Returns the cache key of a routing decision."""
    return f"lookup:{proxy_e164}:{caller_hash}"


async def cache_get_json(key: str) -> Any | None:
    """Returns the decoded value, or None when absent or unreadable."""
    try:
        raw = await get_cache().get(key)
    except Exception as exc:
        log.warning("cache.get_failed", error=str(exc))
        return None
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


async def cache_set_json(
    key: str, value: Any, ttl: int = LOOKUP_TTL_SECONDS
) -> None:
    """Stores a JSON-encodable value with a time to live in seconds."""
    try:
        await get_cache().set(key, json.dumps(value), ex=ttl)
    except Exception as exc:
        log.warning("cache.set_failed", error=str(exc))


async def cache_delete(*keys: str) -> None:
    """Deletes the given keys, ignoring the ones that do not exist."""
    if not keys:
        return
    try:
        await get_cache().delete(*keys)
    except Exception as exc:
        log.warning("cache.delete_failed", error=str(exc))


async def invalidate_number(proxy_e164: str) -> None:
    """Drops every cached routing decision for a proxy number."""
    pattern = f"lookup:{proxy_e164}:*"
    try:
        client = get_cache()
        async for key in client.scan_iter(match=pattern, count=_SCAN_BATCH):
            await client.delete(key)
    except Exception as exc:
        log.warning("cache.invalidate_failed", error=str(exc))


async def rate_limit_hit(
    api_key_id: str, limit: int, window_seconds: int = 60
) -> tuple[bool, int]:
    """Counts one request against a fixed window.

    Args:
        api_key_id: Opaque identifier of the API key, never the key itself.
        limit: Maximum number of requests allowed inside the window.
        window_seconds: Window length in seconds.

    Returns:
        A tuple (allowed, count), where allowed says whether the request may
        proceed and count is the number of requests seen in this window. Fails
        open with (True, 0) when Redis is unreachable: an outage of the cache
        must not take the control plane down.
    """
    key = f"ratelimit:{api_key_id}:{window_seconds}"
    try:
        client = get_cache()
        pipe = client.pipeline()
        pipe.incr(key)
        pipe.expire(key, window_seconds, nx=True)
        count, _ = await pipe.execute()
        return int(count) <= limit, int(count)
    except Exception as exc:
        log.warning("ratelimit.unavailable", error=str(exc))
        return True, 0
