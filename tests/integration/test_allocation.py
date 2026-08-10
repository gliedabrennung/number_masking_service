"""Allocation rules: reuse, the uniqueness invariant, cooldown, PIN fallback."""

from __future__ import annotations

import pytest
import sqlalchemy as sa

from app.core import crypto, errors
from app.services import numbers as numbers_service
from app.services import sessions as sessions_service

pytestmark = pytest.mark.integration

A = "+77011230001"
B = "+77011230002"
C = "+77011230003"
D = "+77011230004"


def _hash_of(e164: str, settings) -> str:
    return crypto.party_hash(e164, settings.party_hash_secret)


async def _pool(db, *e164: str) -> None:
    for number in e164:
        await numbers_service.add_number(db, e164=number, provider="test")
    await db.flush()


async def test_one_number_serves_many_disjoint_pairs(db, test_settings) -> None:
    """The pool is shared: only a repeated *subscriber* forces a new number."""
    await _pool(db, "+77172000101")

    first = await sessions_service.create_session(
        db, party_a=A, party_b=B, settings=test_settings
    )
    second = await sessions_service.create_session(
        db, party_a=C, party_b=D, settings=test_settings
    )

    assert (
        first.allocation.proxy_e164
        == second.allocation.proxy_e164
        == "+77172000101"
    )
    assert first.allocation.ext_code is None
    assert second.allocation.ext_code is None


async def test_same_subscriber_moves_to_another_number(
    db, test_settings
) -> None:
    await _pool(db, "+77172000101", "+77172000102")

    first = await sessions_service.create_session(
        db, party_a=A, party_b=B, settings=test_settings
    )
    second = await sessions_service.create_session(
        db, party_a=A, party_b=C, settings=test_settings
    )

    assert first.allocation.proxy_e164 != second.allocation.proxy_e164
    assert second.allocation.ext_code is None


async def test_exhausted_pool_falls_back_to_a_pin(db, test_settings) -> None:
    """Three sessions on two numbers with an overlapping subscriber."""
    await _pool(db, "+77172000101", "+77172000102")

    await sessions_service.create_session(
        db, party_a=A, party_b=B, settings=test_settings
    )
    await sessions_service.create_session(
        db, party_a=A, party_b=C, settings=test_settings
    )
    third = await sessions_service.create_session(
        db, party_a=A, party_b=D, settings=test_settings
    )

    assert third.allocation.mode == "extension"
    assert third.allocation.ext_code is not None
    assert len(third.allocation.ext_code) == test_settings.ext_code_length

    codes = (
        (
            await db.execute(
                sa.text(
                    "SELECT s.ext_code FROM sessions s "
                    "JOIN session_parties sp ON sp.session_id = s.id "
                    "JOIN numbers n ON n.id = s.number_id "
                    "WHERE s.status = 'active' AND n.e164 = :proxy "
                    "AND sp.party_hash = :hash"
                ),
                {
                    "proxy": third.allocation.proxy_e164,
                    "hash": _hash_of(A, test_settings),
                },
            )
        )
        .scalars()
        .all()
    )
    assert len(codes) >= 2
    assert all(code is not None for code in codes)
    assert len(set(codes)) == len(codes)


async def test_no_number_available_when_pins_are_disabled(
    db, test_settings
) -> None:
    """Refuse rather than hand out a number that belongs to another pair."""
    await _pool(db, "+77172000101")
    await sessions_service.create_session(
        db, party_a=A, party_b=B, settings=test_settings
    )

    with pytest.raises(errors.NoNumberAvailableError):
        await sessions_service.create_session(
            db,
            party_a=A,
            party_b=C,
            settings=test_settings,
            allow_extension_code=False,
        )


async def test_empty_pool_is_a_conflict(db, test_settings) -> None:
    with pytest.raises(errors.NoNumberAvailableError):
        await sessions_service.create_session(
            db, party_a=A, party_b=B, settings=test_settings
        )


async def test_cooldown_keeps_a_released_number_out_of_the_pool(
    db, test_settings
) -> None:
    """A released number stays out of circulation for the cooldown window."""
    await _pool(db, "+77172000101", "+77172000102")

    first = await sessions_service.create_session(
        db, party_a=A, party_b=B, settings=test_settings
    )
    await sessions_service.close_session(db, first.session.id)

    second = await sessions_service.create_session(
        db, party_a=C, party_b=D, settings=test_settings
    )
    assert second.allocation.proxy_e164 != first.allocation.proxy_e164


async def test_number_returns_to_the_pool_after_the_cooldown_expires(
    db, test_settings
) -> None:
    await _pool(db, "+77172000101")

    first = await sessions_service.create_session(
        db, party_a=A, party_b=B, settings=test_settings
    )
    await sessions_service.close_session(db, first.session.id)

    await db.execute(
        sa.text("UPDATE numbers SET released_at = now() - interval '25 hours'")
    )
    await db.execute(
        sa.text(
            "UPDATE session_parties SET released_at ="
            " now() - interval '25 hours'"
        )
    )

    second = await sessions_service.create_session(
        db, party_a=C, party_b=D, settings=test_settings
    )
    assert second.allocation.proxy_e164 == "+77172000101"
    assert second.allocation.ext_code is None


async def test_disabled_numbers_are_never_allocated(db, test_settings) -> None:
    await _pool(db, "+77172000101")
    await numbers_service.set_number_status(
        db, e164="+77172000101", status="disabled"
    )

    with pytest.raises(errors.NoNumberAvailableError):
        await sessions_service.create_session(
            db, party_a=A, party_b=B, settings=test_settings
        )


async def test_identical_parties_are_rejected(db, test_settings) -> None:
    await _pool(db, "+77172000101")
    with pytest.raises(errors.ValidationError):
        await sessions_service.create_session(
            db, party_a=A, party_b=A, settings=test_settings
        )
