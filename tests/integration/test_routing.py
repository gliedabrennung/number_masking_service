"""Routing decisions: both directions, expiry, unknown caller, PIN selection."""

from __future__ import annotations

import datetime

import pytest
import sqlalchemy as sa

from app.services import numbers as numbers_service
from app.services import routing
from app.services import sessions as sessions_service

pytestmark = pytest.mark.integration

A = "+77011230001"
B = "+77011230002"
C = "+77011230003"
D = "+77011230004"
STRANGER = "+77019990000"
PROXY = "+77172000101"


async def _session(db, settings, a=A, b=B, **kwargs):
    return await sessions_service.create_session(
        db, party_a=a, party_b=b, settings=settings, **kwargs
    )


async def test_a_to_b_resolves_to_b(db, test_settings) -> None:
    await numbers_service.add_number(db, e164=PROXY)
    await _session(db, test_settings)

    decision = await routing.resolve_call(
        db, proxy_e164=PROXY, caller=A, settings=test_settings
    )

    assert decision.action is routing.Action.CONNECT
    assert decision.candidate is not None
    assert decision.candidate.callee_e164 == B
    assert decision.candidate.direction == "a2b"


async def test_b_to_a_is_symmetric(db, test_settings) -> None:
    await numbers_service.add_number(db, e164=PROXY)
    await _session(db, test_settings)

    decision = await routing.resolve_call(
        db, proxy_e164=PROXY, caller=B, settings=test_settings
    )

    assert decision.action is routing.Action.CONNECT
    assert decision.candidate.callee_e164 == A
    assert decision.candidate.direction == "b2a"


async def test_caller_in_local_notation_still_matches(
    db, test_settings
) -> None:
    await numbers_service.add_number(db, e164=PROXY)
    await _session(db, test_settings)

    decision = await routing.resolve_call(
        db, proxy_e164=PROXY, caller="87011230001", settings=test_settings
    )
    assert decision.action is routing.Action.CONNECT


async def test_unknown_caller_is_rejected(db, test_settings) -> None:
    await numbers_service.add_number(db, e164=PROXY)
    await _session(db, test_settings)

    decision = await routing.resolve_call(
        db, proxy_e164=PROXY, caller=STRANGER, settings=test_settings
    )

    assert decision.action is routing.Action.REJECT
    assert decision.reject_status == "unknown_caller"
    assert decision.prompt == test_settings.sound_unknown


async def test_expired_session_is_rejected_with_its_own_prompt(
    db, test_settings
) -> None:
    """After the TTL the call must not be connected."""
    await numbers_service.add_number(db, e164=PROXY)
    created = await _session(db, test_settings, ttl_seconds=60)

    await db.execute(
        sa.text("UPDATE sessions SET expires_at = :past WHERE id = :id"),
        {
            "past": datetime.datetime.now(datetime.UTC)
            - datetime.timedelta(minutes=1),
            "id": created.session.id,
        },
    )

    decision = await routing.resolve_call(
        db, proxy_e164=PROXY, caller=A, settings=test_settings
    )

    assert decision.action is routing.Action.REJECT
    assert decision.reject_status == "expired"
    assert decision.prompt == test_settings.sound_expired


async def test_closed_session_is_rejected(db, test_settings) -> None:
    """A closed session refuses calls immediately."""
    await numbers_service.add_number(db, e164=PROXY)
    created = await _session(db, test_settings)
    await sessions_service.close_session(db, created.session.id)

    decision = await routing.resolve_call(
        db, proxy_e164=PROXY, caller=A, settings=test_settings
    )

    assert decision.action is routing.Action.REJECT
    assert decision.reject_status == "expired"


async def test_shared_number_asks_for_a_code_and_the_code_picks_the_session(
    db, test_settings
) -> None:
    """A shared number asks for a PIN, and the PIN picks the session."""
    await numbers_service.add_number(db, e164=PROXY)
    first = await _session(db, test_settings, a=A, b=B)
    second = await _session(db, test_settings, a=A, b=C)

    decision = await routing.resolve_call(
        db, proxy_e164=PROXY, caller=A, settings=test_settings
    )
    assert decision.action is routing.Action.ASK_CODE
    assert decision.candidates is not None
    assert len(decision.candidates) == 2

    await db.refresh(second.session)
    chosen = routing.select_by_code(
        decision.candidates, second.session.ext_code
    )
    assert chosen is not None
    assert chosen.callee_e164 == C

    await db.refresh(first.session)
    other = routing.select_by_code(decision.candidates, first.session.ext_code)
    assert other is not None
    assert other.callee_e164 == B


async def test_the_counterpart_of_a_shared_session_still_connects_directly(
    db, test_settings
) -> None:
    """B is in one session only, so B never has to type a PIN."""
    await numbers_service.add_number(db, e164=PROXY)
    await _session(db, test_settings, a=A, b=B)
    await _session(db, test_settings, a=A, b=C)

    decision = await routing.resolve_call(
        db, proxy_e164=PROXY, caller=C, settings=test_settings
    )
    assert decision.action is routing.Action.CONNECT
    assert decision.candidate.callee_e164 == A


async def test_wrong_number_does_not_reveal_other_sessions(
    db, test_settings
) -> None:
    await numbers_service.add_number(db, e164=PROXY)
    await numbers_service.add_number(db, e164="+77172000102")
    await _session(db, test_settings, a=A, b=B)

    decision = await routing.resolve_call(
        db, proxy_e164="+77172000102", caller=A, settings=test_settings
    )

    assert decision.action is routing.Action.REJECT
    assert decision.reject_status == "unknown_caller"
    assert decision.candidates is None


async def test_non_e164_caller_is_rejected_without_a_database_lookup(
    db, test_settings
) -> None:
    decision = await routing.resolve_call(
        db, proxy_e164=PROXY, caller="anonymous", settings=test_settings
    )
    assert decision.action is routing.Action.REJECT
    assert decision.reject_status == "unknown_caller"
    assert decision.caller_hash == ""
