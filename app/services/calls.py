"""Call journal.

The journal is written by the ARI application from Stasis events and is the
source of truth for reporting; Asterisk's own CDR stays enabled as a backup for
incident forensics. It stores no real phone numbers, only the caller hash, the
proxy number and the session. Conversation content is never recorded.
"""

from __future__ import annotations

import datetime
import uuid

import sqlalchemy as sa
from sqlalchemy.ext import asyncio as sa_asyncio

from app.core import logging_config
from app.db import models

log = logging_config.get_logger(__name__)

_DEFAULT_PAGE_SIZE = 100


async def start_call(
    db: sa_asyncio.AsyncSession,
    *,
    caller_hash: str,
    proxy_e164: str,
    channel_id: str,
    session_id: uuid.UUID | None = None,
    direction: str | None = None,
    status: str = "in_progress",
) -> models.Call:
    """Opens a journal entry for an inbound call.

    Args:
        db: Open database session, inside a transaction.
        caller_hash: Keyed hash of the calling number.
        proxy_e164: The dialled proxy number.
        channel_id: Asterisk channel identifier of the inbound leg.
        session_id: Session the call belongs to, if already known.
        direction: ``a2b`` or ``b2a``, if already known.
        status: Initial status; the final one is written by
            :func:`finish_call`.

    Returns:
        The newly created journal row, flushed so its id is available.
    """
    call = models.Call(
        session_id=session_id,
        direction=direction,
        caller_hash=caller_hash,
        proxy_e164=proxy_e164,
        started_at=datetime.datetime.now(datetime.UTC),
        status=status,
        channel_id=channel_id,
    )
    db.add(call)
    await db.flush()
    log.info(
        "call.started",
        call_id=call.id,
        session_id=str(session_id) if session_id else None,
        channel_id=channel_id,
        direction=direction,
        proxy=proxy_e164,
    )
    return call


async def mark_answered(
    db: sa_asyncio.AsyncSession, call_id: int, *, bridge_id: str | None = None
) -> None:
    """Records that the second leg answered. Idempotent.

    Args:
        db: Open database session, inside a transaction.
        call_id: Journal row to update.
        bridge_id: Identifier of the mixing bridge, when known.
    """
    call = await db.get(models.Call, call_id)
    if call is None or call.answered_at is not None:
        return
    call.answered_at = datetime.datetime.now(datetime.UTC)
    call.status = "answered"
    if bridge_id:
        call.bridge_id = bridge_id
    await db.flush()
    log.info(
        "call.answered", call_id=call.id, session_id=str(call.session_id or "")
    )


async def finish_call(
    db: sa_asyncio.AsyncSession,
    call_id: int,
    *,
    status: str | None = None,
    hangup_cause: str | int | None = None,
    session_id: uuid.UUID | None = None,
    direction: str | None = None,
) -> models.Call | None:
    """Closes a journal entry. Idempotent: the first outcome wins.

    Args:
        db: Open database session, inside a transaction.
        call_id: Journal row to close.
        status: Final status; inferred from the answer state when omitted.
        hangup_cause: Q.850 cause reported by Asterisk.
        session_id: Session to attach, when it became known only at teardown.
        direction: Direction to attach, when it became known only at teardown.

    Returns:
        The closed row, or None when no such row exists.
    """
    call = await db.get(models.Call, call_id)
    if call is None:
        return None
    if call.ended_at is not None:
        return call

    call.ended_at = datetime.datetime.now(datetime.UTC)
    if call.answered_at is not None:
        talk_time = call.ended_at - call.answered_at
        call.duration_sec = max(int(talk_time.total_seconds()), 0)
        call.status = status or "answered"
    else:
        call.status = status or "failed"
    if hangup_cause is not None:
        call.hangup_cause = str(hangup_cause)
    if session_id is not None:
        call.session_id = session_id
    if direction is not None:
        call.direction = direction
    await db.flush()
    log.info(
        "call.ended",
        call_id=call.id,
        session_id=str(call.session_id or ""),
        status=call.status,
        duration_sec=call.duration_sec,
        hangup_cause=call.hangup_cause,
    )
    return call


async def attach_session(
    db: sa_asyncio.AsyncSession,
    call_id: int,
    *,
    session_id: uuid.UUID,
    direction: str,
) -> None:
    """Binds a call to the session chosen after a DTMF PIN was entered."""
    call = await db.get(models.Call, call_id)
    if call is None:
        return
    call.session_id = session_id
    call.direction = direction
    await db.flush()


async def list_calls(
    db: sa_asyncio.AsyncSession,
    *,
    session_id: uuid.UUID | None = None,
    status: str | None = None,
    date_from: datetime.datetime | None = None,
    date_to: datetime.datetime | None = None,
    limit: int = _DEFAULT_PAGE_SIZE,
    offset: int = 0,
) -> tuple[list[models.Call], int]:
    """Returns a page of the journal, newest first.

    Args:
        db: Open database session.
        session_id: Restrict to one session.
        status: Restrict to one final status.
        date_from: Only calls started at or after this moment.
        date_to: Only calls started at or before this moment.
        limit: Page size.
        offset: Number of rows to skip.

    Returns:
        A tuple (rows, total), where total counts all rows matching the
        filters, ignoring the page bounds.
    """
    statement = sa.select(models.Call).order_by(
        models.Call.started_at.desc(), models.Call.id.desc()
    )
    count_statement = sa.select(sa.func.count()).select_from(models.Call)

    conditions = []
    if session_id is not None:
        conditions.append(models.Call.session_id == session_id)
    if status is not None:
        conditions.append(models.Call.status == status)
    if date_from is not None:
        conditions.append(models.Call.started_at >= date_from)
    if date_to is not None:
        conditions.append(models.Call.started_at <= date_to)
    for condition in conditions:
        statement = statement.where(condition)
        count_statement = count_statement.where(condition)

    total = int(await db.scalar(count_statement) or 0)
    rows = (
        (await db.execute(statement.limit(limit).offset(offset)))
        .scalars()
        .all()
    )
    return list(rows), total


async def find_by_channel(
    db: sa_asyncio.AsyncSession, channel_id: str
) -> models.Call | None:
    """Returns the most recent journal row for an Asterisk channel."""
    return await db.scalar(
        sa.select(models.Call)
        .where(models.Call.channel_id == channel_id)
        .order_by(models.Call.id.desc())
        .limit(1)
    )


async def set_hangup_cause(
    db: sa_asyncio.AsyncSession, call_id: int, hangup_cause: str | int
) -> None:
    """Fills in the Q.850 cause of a call that was closed without one.

    ``StasisEnd`` carries no cause, so a normally cleared call is journalled
    before ``ChannelDestroyed`` reports why it ended. Only a missing value is
    written; an outcome already recorded is never overwritten.
    """
    call = await db.get(models.Call, call_id)
    if call is None or call.hangup_cause is not None:
        return
    call.hangup_cause = str(hangup_cause)
    await db.flush()
    log.info(
        "call.cause_recorded", call_id=call_id, hangup_cause=call.hangup_cause
    )
