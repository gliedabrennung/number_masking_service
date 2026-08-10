"""models.Call journal contents, retention and session expiry sweep."""

from __future__ import annotations

import datetime

import pytest
import sqlalchemy as sa

from app.db import models
from app.services import calls as calls_service
from app.services import numbers as numbers_service
from app.services import sessions as sessions_service

pytestmark = pytest.mark.integration

A = "+77011230001"
B = "+77011230002"
PROXY = "+77172000101"


async def _session(db, settings, **kwargs):
    await numbers_service.add_number(db, e164=PROXY)
    return await sessions_service.create_session(
        db, party_a=A, party_b=B, settings=settings, **kwargs
    )


async def test_journal_records_timings_and_no_audio_fields(
    db, test_settings
) -> None:
    """Times, duration and status are journalled, audio is not."""
    created = await _session(db, test_settings)

    call = await calls_service.start_call(
        db,
        caller_hash="hash-a",
        proxy_e164=PROXY,
        channel_id="ch-1",
        session_id=created.session.id,
        direction="a2b",
    )
    await calls_service.mark_answered(db, call.id, bridge_id="bridge-1")
    await calls_service.finish_call(db, call.id, hangup_cause=16)

    stored = await db.get(models.Call, call.id)
    assert stored.status == "answered"
    assert stored.started_at and stored.answered_at and stored.ended_at
    assert stored.duration_sec is not None
    assert stored.bridge_id == "bridge-1"
    assert not [
        c
        for c in models.Call.__table__.columns
        if "record" in c.name or "file" in c.name
    ]


async def test_journal_stores_no_real_numbers(db, test_settings) -> None:
    """The journal holds no real phone number, only hashes and the proxy."""
    created = await _session(db, test_settings)
    await calls_service.start_call(
        db,
        caller_hash="hash-a",
        proxy_e164=PROXY,
        channel_id="ch-1",
        session_id=created.session.id,
        direction="a2b",
    )
    await db.flush()

    rows = (await db.execute(sa.text("SELECT * FROM calls"))).mappings().all()
    dumped = str([dict(row) for row in rows])
    assert A.lstrip("+") not in dumped
    assert B.lstrip("+") not in dumped
    assert PROXY in dumped


async def test_finish_is_idempotent(db, test_settings) -> None:
    created = await _session(db, test_settings)
    call = await calls_service.start_call(
        db,
        caller_hash="hash-a",
        proxy_e164=PROXY,
        channel_id="ch-1",
        session_id=created.session.id,
    )
    await calls_service.mark_answered(db, call.id)
    first = await calls_service.finish_call(db, call.id, hangup_cause=16)
    second = await calls_service.finish_call(
        db, call.id, status="failed", hangup_cause=41
    )

    assert second.ended_at == first.ended_at
    assert second.status == "answered"


async def test_list_calls_filters_and_paginates(db, test_settings) -> None:
    created = await _session(db, test_settings)
    for index in range(5):
        call = await calls_service.start_call(
            db,
            caller_hash="hash-a",
            proxy_e164=PROXY,
            channel_id=f"ch-{index}",
            session_id=created.session.id,
            direction="a2b",
        )
        await calls_service.finish_call(
            db, call.id, status="answered" if index % 2 else "no_answer"
        )

    answered, total_answered = await calls_service.list_calls(
        db, status="answered"
    )
    assert total_answered == 2
    assert all(call.status == "answered" for call in answered)

    page, total = await calls_service.list_calls(
        db, session_id=created.session.id, limit=2
    )
    assert total == 5
    assert len(page) == 2


async def test_expiry_sweep_flips_sessions_and_releases_the_number(
    db, test_settings
) -> None:
    created = await _session(db, test_settings, ttl_seconds=60)
    await db.execute(
        sa.text("UPDATE sessions SET expires_at = :past WHERE id = :id"),
        {
            "past": datetime.datetime.now(datetime.UTC)
            - datetime.timedelta(seconds=1),
            "id": created.session.id,
        },
    )

    expired = await sessions_service.expire_due_sessions(db)

    assert [s.id for s in expired] == [created.session.id]
    refreshed = await db.get(models.Session, created.session.id)
    assert refreshed.status == "expired"
    assert refreshed.closed_at is not None

    released = await db.scalar(
        sa.text("SELECT released_at FROM numbers WHERE e164 = :e"), {"e": PROXY}
    )
    assert released is not None

    flags = (
        await db.execute(
            sa.text(
                "SELECT is_active, released_at FROM session_parties "
                "WHERE session_id = :id"
            ),
            {"id": created.session.id},
        )
    ).all()
    assert all(
        row.is_active is False and row.released_at is not None for row in flags
    )


async def test_retention_purges_old_records(db, test_settings) -> None:
    created = await _session(db, test_settings)
    call = await calls_service.start_call(
        db,
        caller_hash="hash-a",
        proxy_e164=PROXY,
        channel_id="ch-old",
        session_id=created.session.id,
    )
    await calls_service.finish_call(db, call.id, status="answered")
    await sessions_service.close_session(db, created.session.id)

    await db.execute(
        sa.text("UPDATE calls SET started_at = now() - interval '200 days'")
    )
    await db.execute(
        sa.text("UPDATE sessions SET closed_at = now() - interval '200 days'")
    )

    result = await sessions_service.purge_old_records(
        db, settings=test_settings
    )

    assert result["calls"] == 1
    assert result["sessions"] == 1
    assert (await db.execute(sa.select(models.Call))).first() is None
