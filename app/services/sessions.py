"""Session lifecycle: create, read, extend, close, expire, purge."""

from __future__ import annotations

import dataclasses
import datetime
import uuid

import sqlalchemy as sa
from sqlalchemy import exc as sa_exc
from sqlalchemy import orm
from sqlalchemy.ext import asyncio as sa_asyncio

from app.core import config, crypto, errors, logging_config, phone
from app.db import cache, models
from app.services import allocation

log = logging_config.get_logger(__name__)

_EXPIRY_SWEEP_LIMIT = 500


@dataclasses.dataclass(slots=True)
class CreatedSession:
    """A freshly created session together with how its number was allocated."""

    session: models.Session
    allocation: allocation.Allocation


def now_utc() -> datetime.datetime:
    """Returns the current time as a timezone-aware UTC value."""
    return datetime.datetime.now(datetime.UTC)


async def create_session(
    db: sa_asyncio.AsyncSession,
    *,
    party_a: str,
    party_b: str,
    settings: config.Settings,
    ttl_seconds: int | None = None,
    external_id: str | None = None,
    max_calls: int | None = None,
    allow_extension_code: bool = True,
) -> CreatedSession:
    """Allocates a proxy number and persists the session with both parties.

    Args:
        db: Open database session, inside a transaction.
        party_a: Real number of the initiating side, any accepted notation.
        party_b: Real number of the other side.
        settings: Application settings.
        ttl_seconds: Lifetime of the session; defaults to the configured TTL.
        external_id: Identifier of the order in the customer's system.
        max_calls: Cap on answered calls, or None for no cap.
        allow_extension_code: When False, refuse rather than share a number.

    Returns:
        The created session and its allocation.

    Raises:
        ValidationError: A number is not valid E.164, the two sides are equal,
            or the TTL or call cap is out of range.
        NoNumberAvailableError: No proxy number can serve this pair.
        SessionNotActiveError: A concurrent request took the allocated number
            first; the caller may retry.
    """
    number_a = phone.normalize_e164(party_a)
    number_b = phone.normalize_e164(party_b)
    if number_a == number_b:
        raise errors.ValidationError(
            "party_a and party_b must be different numbers"
        )

    ttl = ttl_seconds or settings.default_ttl_seconds
    if ttl <= 0 or ttl > settings.max_ttl_seconds:
        raise errors.ValidationError(
            f"ttl_seconds must be in 1..{settings.max_ttl_seconds}"
        )
    if max_calls is not None and max_calls <= 0:
        raise errors.ValidationError(
            "max_calls must be a positive integer or null"
        )

    hash_a = crypto.party_hash(number_a, settings.party_hash_secret)
    hash_b = crypto.party_hash(number_b, settings.party_hash_secret)

    allocated = await allocation.allocate_number(
        db,
        hashes=[hash_a, hash_b],
        settings=settings,
        allow_extension_code=allow_extension_code,
    )

    key = settings.encryption_key_bytes
    session = models.Session(
        id=uuid.uuid4(),
        number_id=allocated.number_id,
        ext_code=allocated.ext_code,
        status="active",
        external_id=external_id,
        max_calls=max_calls,
        trace_id=logging_config.get_trace_id(),
        created_at=now_utc(),
        expires_at=now_utc() + datetime.timedelta(seconds=ttl),
    )
    has_code = allocated.ext_code is not None
    session.parties = [
        models.SessionParty(
            role="a",
            number_id=allocated.number_id,
            party_e164_enc=crypto.encrypt_e164(number_a, key),
            party_hash=hash_a,
            is_active=True,
            has_ext_code=has_code,
        ),
        models.SessionParty(
            role="b",
            number_id=allocated.number_id,
            party_e164_enc=crypto.encrypt_e164(number_b, key),
            party_hash=hash_b,
            is_active=True,
            has_ext_code=has_code,
        ),
    ]
    db.add(session)
    try:
        await db.flush()
    except sa_exc.IntegrityError as integrity_error:
        raise errors.SessionNotActiveError(
            "conflicting active session for this pair on the allocated number",
            code="allocation_conflict",
        ) from integrity_error

    await cache.invalidate_number(allocated.proxy_e164)
    log.info(
        "session.created",
        session_id=str(session.id),
        proxy=allocated.proxy_e164,
        mode=allocated.mode,
        ext_code_issued=allocated.ext_code is not None,
        promoted=len(allocated.promoted_session_ids),
        party_a_masked=phone.mask_e164(number_a),
        party_b_masked=phone.mask_e164(number_b),
        external_id=external_id,
    )
    return CreatedSession(session=session, allocation=allocated)


async def get_session(
    db: sa_asyncio.AsyncSession, session_id: uuid.UUID
) -> models.Session:
    """Loads a session with its number and parties eagerly.

    Eager loading is not an optimisation here: the async ORM cannot lazy-load
    on attribute access, so anything the callers touch must be loaded up front.

    Args:
        db: Open database session.
        session_id: Identifier of the session.

    Returns:
        The session.

    Raises:
        SessionNotFoundError: No such session.
    """
    session = await db.scalar(
        sa.select(models.Session)
        .options(
            orm.joinedload(models.Session.number),
            orm.selectinload(models.Session.parties),
        )
        .where(models.Session.id == session_id)
    )
    if session is None:
        raise errors.SessionNotFoundError(f"session {session_id} not found")
    return session


async def extend_session(
    db: sa_asyncio.AsyncSession,
    session_id: uuid.UUID,
    *,
    settings: config.Settings,
    ttl_seconds: int | None = None,
    expires_at: datetime.datetime | None = None,
) -> models.Session:
    """Pushes the expiry of an active session forward.

    Args:
        db: Open database session, inside a transaction.
        session_id: Identifier of the session.
        settings: Application settings; the TTL ceiling comes from here.
        ttl_seconds: New lifetime counted from now.
        expires_at: Explicit new expiry; takes precedence over ttl_seconds.

    Returns:
        The updated session.

    Raises:
        SessionNotFoundError: No such session.
        SessionNotActiveError: The session is closed or expired.
        ValidationError: Neither bound was given, or the new expiry is in the
            past or beyond the configured ceiling.
    """
    session = await get_session(db, session_id)
    if session.status != "active":
        raise errors.SessionNotActiveError(f"session is {session.status}")

    if expires_at is not None:
        new_expiry = (
            expires_at
            if expires_at.tzinfo
            else expires_at.replace(tzinfo=datetime.UTC)
        )
    elif ttl_seconds is not None:
        if ttl_seconds <= 0 or ttl_seconds > settings.max_ttl_seconds:
            raise errors.ValidationError(
                f"ttl_seconds must be in 1..{settings.max_ttl_seconds}"
            )
        new_expiry = now_utc() + datetime.timedelta(seconds=ttl_seconds)
    else:
        raise errors.ValidationError(
            "either ttl_seconds or expires_at is required"
        )

    if new_expiry <= now_utc():
        raise errors.ValidationError("expires_at must be in the future")

    session.expires_at = new_expiry
    await db.flush()
    await cache.invalidate_number(await number_e164(db, session.number_id))
    log.info(
        "session.extended",
        session_id=str(session.id),
        expires_at=new_expiry.isoformat(),
    )
    return session


async def close_session(
    db: sa_asyncio.AsyncSession,
    session_id: uuid.UUID,
    *,
    status: str = "closed",
) -> models.Session:
    """Closes or expires a session without tearing down a live conversation.

    Dropping live audio because a TTL elapsed is worse for the subscriber than
    letting the current call finish; new calls are refused immediately.

    Args:
        db: Open database session, inside a transaction.
        session_id: Identifier of the session.
        status: Terminal status to write, ``closed`` or ``expired``.

    Returns:
        The session, unchanged when it was not active any more.

    Raises:
        SessionNotFoundError: No such session.
    """
    session = await get_session(db, session_id)
    if session.status != "active":
        return session

    session.status = status
    session.closed_at = now_utc()
    await db.flush()
    await _mark_number_released(db, session.number_id)
    await cache.invalidate_number(await number_e164(db, session.number_id))
    log.info("session.closed", session_id=str(session.id), status=status)
    return session


async def number_e164(db: sa_asyncio.AsyncSession, number_id: int) -> str:
    """Returns the proxy number of a session without lazy loading."""
    found = await db.scalar(
        sa.select(models.Number.e164).where(models.Number.id == number_id)
    )
    return str(found or "")


async def _mark_number_released(
    db: sa_asyncio.AsyncSession, number_id: int
) -> None:
    """Starts the cooldown clock once the number has no active session left."""
    remaining = await db.scalar(
        sa.select(sa.func.count())
        .select_from(models.Session)
        .where(
            models.Session.number_id == number_id,
            models.Session.status == "active",
        )
    )
    if not remaining:
        await db.execute(
            sa.update(models.Number)
            .where(models.Number.id == number_id)
            .values(released_at=now_utc())
        )


async def expire_due_sessions(
    db: sa_asyncio.AsyncSession, *, limit: int = _EXPIRY_SWEEP_LIMIT
) -> list[models.Session]:
    """Flips active sessions whose expiry has passed to ``expired``.

    Args:
        db: Open database session, inside a transaction.
        limit: Maximum number of sessions to process in one sweep.

    Returns:
        The sessions that were expired by this call.
    """
    rows = (
        await db.execute(
            sa.select(models.Session)
            .options(orm.joinedload(models.Session.number))
            .where(
                models.Session.status == "active",
                models.Session.expires_at <= now_utc(),
            )
            .order_by(models.Session.expires_at)
            .limit(limit)
        )
    ).unique()
    expired: list[models.Session] = []
    for session in rows.scalars():
        session.status = "expired"
        session.closed_at = now_utc()
        expired.append(session)
    if expired:
        await db.flush()
        for session in expired:
            await _mark_number_released(db, session.number_id)
            await cache.invalidate_number(
                await number_e164(db, session.number_id)
            )
        log.info("sessions.expired", count=len(expired))
    return expired


async def purge_old_records(
    db: sa_asyncio.AsyncSession, *, settings: config.Settings
) -> dict[str, int]:
    """Deletes call rows and closed sessions past their retention window.

    Args:
        db: Open database session, inside a transaction.
        settings: Application settings holding the retention windows.

    Returns:
        A dict with the number of deleted rows per table, keyed ``calls`` and
        ``sessions``.
    """
    call_cutoff = now_utc() - datetime.timedelta(
        days=settings.call_retention_days
    )
    session_cutoff = now_utc() - datetime.timedelta(
        days=settings.closed_session_retention_days
    )

    deleted_calls = (
        await db.execute(
            sa.text("DELETE FROM calls WHERE started_at < :cutoff"),
            {"cutoff": call_cutoff},
        )
    ).rowcount or 0
    deleted_sessions = (
        await db.execute(
            sa.text(
                "DELETE FROM sessions "
                "WHERE status <> 'active' AND closed_at IS NOT NULL "
                "AND closed_at < :cutoff"
            ),
            {"cutoff": session_cutoff},
        )
    ).rowcount or 0

    if deleted_calls or deleted_sessions:
        log.info(
            "retention.purged",
            calls=deleted_calls,
            sessions=deleted_sessions,
        )
    return {"calls": deleted_calls, "sessions": deleted_sessions}


async def answered_call_count(
    db: sa_asyncio.AsyncSession, session_id: uuid.UUID
) -> int:
    """Returns how many calls of this session were answered."""
    counted = await db.scalar(
        sa.select(sa.func.count())
        .select_from(models.Call)
        .where(
            models.Call.session_id == session_id,
            models.Call.status == "answered",
        )
    )
    return int(counted or 0)
