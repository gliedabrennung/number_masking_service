"""Proxy number pool management and pool statistics."""

from __future__ import annotations

import dataclasses
import datetime

import sqlalchemy as sa
from sqlalchemy import exc as sa_exc
from sqlalchemy.ext import asyncio as sa_asyncio

from app.core import config, errors, logging_config, phone
from app.db import cache, models

log = logging_config.get_logger(__name__)


@dataclasses.dataclass(slots=True)
class PoolStats:
    """A snapshot of the proxy number pool.

    Attributes:
        total: Every number in the pool.
        enabled: Numbers eligible for allocation.
        disabled: Numbers taken out of service.
        in_use: Numbers carrying at least one active session.
        in_cooldown: Idle numbers still inside their quarantine window.
        free: Idle numbers available to a new pair right now.
        active_sessions: Active sessions across the whole pool.
    """

    total: int
    enabled: int
    disabled: int
    in_use: int
    in_cooldown: int
    free: int
    active_sessions: int


async def add_number(
    db: sa_asyncio.AsyncSession,
    *,
    e164: str,
    provider: str | None = None,
    status: str = "enabled",
) -> models.Number:
    """Adds a number to the pool.

    Args:
        db: Open database session, inside a transaction.
        e164: The number, in any accepted notation.
        provider: Free-form origin marker.
        status: ``enabled`` or ``disabled``.

    Returns:
        The stored number.

    Raises:
        InvalidPhoneNumberError: The value is not a valid phone number.
        NumberAlreadyExistsError: The number is already in the pool.
    """
    normalized = phone.normalize_e164(e164)
    number = models.Number(e164=normalized, provider=provider, status=status)
    db.add(number)
    try:
        await db.flush()
    except sa_exc.IntegrityError as integrity_error:
        raise errors.NumberAlreadyExistsError(
            f"number {normalized} is already in the pool"
        ) from integrity_error
    log.info("number.added", proxy=normalized, provider=provider)
    return number


async def set_number_status(
    db: sa_asyncio.AsyncSession, *, e164: str, status: str
) -> models.Number:
    """Enables or disables a number.

    Disabling never breaks live sessions; it only removes the number from
    future allocations.

    Args:
        db: Open database session, inside a transaction.
        e164: The number, in any accepted notation.
        status: ``enabled`` or ``disabled``.

    Returns:
        The updated number.

    Raises:
        NumberNotFoundError: The number is not in the pool.
    """
    normalized = phone.normalize_e164(e164)
    number = await db.scalar(
        sa.select(models.Number).where(models.Number.e164 == normalized)
    )
    if number is None:
        raise errors.NumberNotFoundError(
            f"number {normalized} is not in the pool"
        )
    number.status = status
    await db.flush()
    await cache.invalidate_number(normalized)
    log.info("number.status_changed", proxy=normalized, status=status)
    return number


async def list_numbers(
    db: sa_asyncio.AsyncSession,
) -> list[tuple[models.Number, int]]:
    """Returns every pool number with its count of active sessions."""
    active = (
        sa.select(
            models.Session.number_id,
            sa.func.count().label("active_count"),
        )
        .where(models.Session.status == "active")
        .group_by(models.Session.number_id)
        .subquery()
    )
    statement = (
        sa.select(models.Number, sa.func.coalesce(active.c.active_count, 0))
        .outerjoin(active, active.c.number_id == models.Number.id)
        .order_by(models.Number.e164)
    )
    rows = (await db.execute(statement)).all()
    return [(row[0], int(row[1])) for row in rows]


async def pool_stats(
    db: sa_asyncio.AsyncSession, *, settings: config.Settings
) -> PoolStats:
    """Returns the pool snapshot used by ``GET /numbers`` and the metrics."""
    rows = await list_numbers(db)
    cooldown_edge = datetime.datetime.now(datetime.UTC) - datetime.timedelta(
        hours=settings.number_cooldown_hours
    )

    total = len(rows)
    enabled = sum(1 for number, _ in rows if number.status == "enabled")
    in_use = sum(1 for _, count in rows if count > 0)
    active_sessions = sum(count for _, count in rows)
    in_cooldown = sum(
        1
        for number, count in rows
        if number.status == "enabled"
        and count == 0
        and number.released_at is not None
        and number.released_at > cooldown_edge
    )
    free = sum(
        1
        for number, count in rows
        if number.status == "enabled"
        and count == 0
        and (number.released_at is None or number.released_at <= cooldown_edge)
    )
    return PoolStats(
        total=total,
        enabled=enabled,
        disabled=total - enabled,
        in_use=in_use,
        in_cooldown=in_cooldown,
        free=free,
        active_sessions=active_sessions,
    )
