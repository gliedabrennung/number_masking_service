"""End-to-end Stasis flow driven by synthetic ARI events.

Asterisk is replaced by a recording double, so the assertions are about what the
application *tells* Asterisk to do — in particular that the outbound leg always
presents the proxy number, never the real caller.
"""

from __future__ import annotations

import asyncio
import datetime
from typing import Any

import pytest
import sqlalchemy as sa

from app.ari import stasis
from app.core import logging_config
from app.db import models
from app.services import numbers as numbers_service
from app.services import routing
from app.services import sessions as sessions_service

pytestmark = pytest.mark.integration

A = "+77011230001"
B = "+77011230002"
C = "+77011230003"
PROXY = "+77172000101"
CH_A = "ch-a"
CH_B = "ch-a-b"


class FakeARI:
    """Records every ARI call the application makes."""

    def __init__(self) -> None:
        """Starts with an empty recording and a connected websocket."""
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.connected = True

    def _record(self, method: str, /, **kwargs: Any) -> None:
        self.calls.append((method, kwargs))

    def names(self) -> list[str]:
        return [name for name, _ in self.calls]

    def arguments(self, name: str) -> dict[str, Any]:
        for call_name, kwargs in self.calls:
            if call_name == name:
                return kwargs
        raise AssertionError(f"{name} was never called; got {self.names()}")

    async def answer(self, channel_id: str) -> None:
        self._record("answer", channel_id=channel_id)

    async def ring(self, channel_id: str) -> None:
        self._record("ring", channel_id=channel_id)

    async def ring_stop(self, channel_id: str) -> None:
        self._record("ring_stop", channel_id=channel_id)

    async def set_variable(
        self, channel_id: str, name: str, value: str
    ) -> None:
        self._record(
            "set_variable", channel_id=channel_id, name=name, value=value
        )

    async def subscribe_channel(self, app: str, channel_id: str) -> None:
        self._record("subscribe_channel", app=app, channel_id=channel_id)

    async def hangup(
        self, channel_id: str, *, reason_code: int | None = None
    ) -> None:
        self._record("hangup", channel_id=channel_id, reason_code=reason_code)

    async def play(
        self, channel_id: str, media: str, *, lang: str = "ru"
    ) -> str | None:
        self._record("play", channel_id=channel_id, media=media)
        return None

    async def create_bridge(
        self, bridge_id: str, *, name: str | None = None
    ) -> dict:
        self._record("create_bridge", bridge_id=bridge_id, name=name)
        return {"id": bridge_id}

    async def add_to_bridge(self, bridge_id: str, *channel_ids: str) -> None:
        self._record(
            "add_to_bridge", bridge_id=bridge_id, channels=list(channel_ids)
        )

    async def destroy_bridge(self, bridge_id: str) -> None:
        self._record("destroy_bridge", bridge_id=bridge_id)

    async def originate(self, **kwargs: Any) -> dict:
        self._record("originate", **kwargs)
        return {"id": kwargs.get("channel_id", CH_B)}

    async def aclose(self) -> None:
        self._record("aclose")


def inbound_start(
    caller: str = A, proxy: str = PROXY, channel_id: str = CH_A
) -> dict:
    return {
        "type": "StasisStart",
        "args": ["inbound", caller, proxy],
        "channel": {
            "id": channel_id,
            "caller": {"number": caller},
            "dialplan": {"exten": proxy},
        },
    }


def outbound_start(channel_id: str = CH_B, inbound_id: str = CH_A) -> dict:
    return {
        "type": "StasisStart",
        "args": ["outbound", inbound_id],
        "channel": {"id": channel_id},
    }


def destroyed(channel_id: str, cause: int = 16) -> dict:
    return {
        "type": "ChannelDestroyed",
        "channel": {"id": channel_id},
        "cause": cause,
    }


def dtmf(digit: str, channel_id: str = CH_A) -> dict:
    return {
        "type": "ChannelDtmfReceived",
        "channel": {"id": channel_id},
        "digit": digit,
    }


@pytest.fixture
async def stasis_app(db, test_settings):
    fake = FakeARI()
    app = stasis.MaskingStasisApp(test_settings, client=fake)  # type: ignore
    yield app, fake
    await app.stop()


async def _settle() -> None:
    for _ in range(20):
        await asyncio.sleep(0.01)


async def _seed_session(db, settings, *, a: str = A, b: str = B, **kwargs):
    await numbers_service.add_number(db, e164=PROXY)
    created = await sessions_service.create_session(
        db, party_a=a, party_b=b, settings=settings, **kwargs
    )
    await db.commit()
    return created


async def test_full_call_a_to_b(db, test_settings, stasis_app) -> None:
    """The main scenario: A dials the proxy and B sees the proxy number."""
    app, fake = stasis_app
    created = await _seed_session(db, test_settings)

    await app.handle_event(inbound_start())
    await _settle()

    originate = fake.arguments("originate")
    assert originate["caller_id"] == f'"{PROXY}" <{PROXY}>'
    assert A.lstrip("+") not in str(originate)
    assert originate["endpoint"] == "PJSIP/77011230002"
    # The caller's display name is scrubbed before the second leg can
    # inherit it: a softphone usually puts the subscriber's own number there.
    assert fake.arguments("set_variable") == {
        "channel_id": CH_A,
        "name": "CALLERID(name)",
        "value": "",
    }
    assert originate["timeout"] == test_settings.originate_timeout
    assert originate["originator"] == CH_A
    assert "ring" in fake.names()

    await app.handle_event(outbound_start())
    await _settle()

    assert "add_to_bridge" in fake.names()
    assert fake.names().count("add_to_bridge") == 2

    await app.handle_event(destroyed(CH_A, cause=16))
    await _settle()

    row = (
        await db.execute(
            sa.select(models.Call).where(models.Call.channel_id == CH_A)
        )
    ).scalar_one()
    assert row.status == "answered"
    assert row.direction == "a2b"
    assert row.session_id == created.session.id
    assert row.answered_at is not None
    assert row.duration_sec is not None and row.duration_sec >= 0
    assert row.hangup_cause == "16"


async def test_reverse_direction_is_symmetric(
    db, test_settings, stasis_app
) -> None:
    """The reverse direction needs no code of its own."""
    app, fake = stasis_app
    await _seed_session(db, test_settings)

    await app.handle_event(inbound_start(caller=B))
    await _settle()

    originate = fake.arguments("originate")
    assert originate["endpoint"] == "PJSIP/77011230001"
    assert originate["caller_id"] == f'"{PROXY}" <{PROXY}>'

    await app.handle_event(outbound_start())
    await app.handle_event(destroyed(CH_A))
    await _settle()

    row = (
        await db.execute(
            sa.select(models.Call).where(models.Call.channel_id == CH_A)
        )
    ).scalar_one()
    assert row.direction == "b2a"


async def test_callee_does_not_answer(db, test_settings, stasis_app) -> None:
    """The callee never picks up: the caller is released, no answer logged."""
    app, fake = stasis_app
    await _seed_session(db, test_settings)

    await app.handle_event(inbound_start())
    await _settle()
    await app.handle_event(destroyed(CH_B, cause=19))
    await _settle()

    row = (
        await db.execute(
            sa.select(models.Call).where(models.Call.channel_id == CH_A)
        )
    ).scalar_one()
    assert row.status == "no_answer"
    assert row.answered_at is None
    assert row.duration_sec is None
    assert ("hangup", {"channel_id": CH_A, "reason_code": 19}) in fake.calls


async def test_busy_callee_is_journalled_as_busy(
    db, test_settings, stasis_app
) -> None:
    app, _ = stasis_app
    await _seed_session(db, test_settings)

    await app.handle_event(inbound_start())
    await _settle()
    await app.handle_event(destroyed(CH_B, cause=17))
    await _settle()

    row = (
        await db.execute(
            sa.select(models.Call).where(models.Call.channel_id == CH_A)
        )
    ).scalar_one()
    assert row.status == "busy"


async def test_expired_session_plays_a_prompt_and_never_bridges(
    db, test_settings, stasis_app
) -> None:
    """An expired session is announced and never bridged."""
    app, fake = stasis_app
    created = await _seed_session(db, test_settings, ttl_seconds=60)
    await db.execute(
        sa.text("UPDATE sessions SET expires_at = :past WHERE id = :id"),
        {
            "past": datetime.datetime.now(datetime.UTC)
            - datetime.timedelta(minutes=1),
            "id": created.session.id,
        },
    )
    await db.commit()

    await app.handle_event(inbound_start())
    await _settle()

    assert "originate" not in fake.names()
    assert "create_bridge" not in fake.names()
    assert fake.arguments("play")["media"] == test_settings.sound_expired
    assert fake.arguments("hangup")["reason_code"] == 21

    row = (
        await db.execute(
            sa.select(models.Call).where(models.Call.channel_id == CH_A)
        )
    ).scalar_one()
    assert row.status == "expired"


async def test_unknown_caller_is_hung_up(db, test_settings, stasis_app) -> None:
    """A caller with no session is announced and released."""
    app, fake = stasis_app
    await _seed_session(db, test_settings)

    await app.handle_event(inbound_start(caller="+77019990000"))
    await _settle()

    assert "originate" not in fake.names()
    assert fake.arguments("play")["media"] == test_settings.sound_unknown

    row = (
        await db.execute(
            sa.select(models.Call).where(models.Call.channel_id == CH_A)
        )
    ).scalar_one()
    assert row.status == "unknown_caller"
    assert row.session_id is None


async def test_extension_code_connects_the_right_session(
    db, test_settings, stasis_app
) -> None:
    """The PIN selects which of two sessions to bridge."""
    app, fake = stasis_app
    await numbers_service.add_number(db, e164=PROXY)
    await sessions_service.create_session(
        db, party_a=A, party_b=B, settings=test_settings
    )
    second = await sessions_service.create_session(
        db, party_a=A, party_b=C, settings=test_settings
    )
    await db.commit()
    await db.refresh(second.session)
    code = second.session.ext_code
    assert code is not None

    await app.handle_event(inbound_start())
    await _settle()

    assert fake.arguments("play")["media"] == test_settings.sound_enter_code
    for digit in code:
        await app.handle_event(dtmf(digit))
    await _settle()

    originate = fake.arguments("originate")
    assert originate["endpoint"] == "PJSIP/77011230003"
    assert originate["caller_id"] == f'"{PROXY}" <{PROXY}>'

    row = (
        await db.execute(
            sa.select(models.Call).where(models.Call.channel_id == CH_A)
        )
    ).scalar_one()
    assert row.session_id == second.session.id


async def test_wrong_extension_code_hangs_up_after_three_attempts(
    db, test_settings, stasis_app
) -> None:
    app, fake = stasis_app
    await numbers_service.add_number(db, e164=PROXY)
    await sessions_service.create_session(
        db, party_a=A, party_b=B, settings=test_settings
    )
    await sessions_service.create_session(
        db, party_a=A, party_b=C, settings=test_settings
    )
    await db.commit()

    await app.handle_event(inbound_start())
    await _settle()

    for _ in range(test_settings.dtmf_max_attempts):
        for digit in "0000":
            await app.handle_event(dtmf(digit))
        await _settle()

    assert "originate" not in fake.names()
    played = [kwargs["media"] for name, kwargs in fake.calls if name == "play"]
    assert (
        played.count(test_settings.sound_wrong_code)
        >= test_settings.dtmf_max_attempts - 1
    )

    row = (
        await db.execute(
            sa.select(models.Call).where(models.Call.channel_id == CH_A)
        )
    ).scalar_one()
    assert row.status == "rejected"


async def test_max_calls_limit_blocks_further_calls(
    db, test_settings, stasis_app
) -> None:
    app, fake = stasis_app
    created = await _seed_session(db, test_settings, max_calls=1)

    await app.handle_event(inbound_start())
    await _settle()
    await app.handle_event(outbound_start())
    await _settle()
    await app.handle_event(destroyed(CH_A))
    await _settle()

    fake.calls.clear()
    await app.handle_event(inbound_start(channel_id="ch-a2"))
    await _settle()

    assert "originate" not in fake.names()
    rows = (
        (
            await db.execute(
                sa.select(models.Call)
                .where(models.Call.session_id == created.session.id)
                .order_by(models.Call.id)
            )
        )
        .scalars()
        .all()
    )
    assert [row.status for row in rows] == ["answered", "rejected"]


async def test_database_outage_does_not_strand_the_caller(
    db, test_settings, stasis_app, monkeypatch
) -> None:
    """A failed lookup plays the error prompt and releases the caller."""
    app, fake = stasis_app
    await _seed_session(db, test_settings)

    from app.db import engine as engine_module

    def _broken_scope():
        raise RuntimeError("database is down")

    monkeypatch.setattr(engine_module, "session_scope", _broken_scope)

    await app.handle_event(inbound_start())
    await _settle()

    assert "originate" not in fake.names()
    assert fake.arguments("play")["media"] == test_settings.sound_error
    assert fake.arguments("hangup")["reason_code"] == 41


async def test_reject_does_not_block_the_event_pump(
    db, test_settings, stasis_app
) -> None:
    """A refused call must not stall the handling of other calls.

    The reject path waits for PlaybackFinished, and that event can only be
    delivered by the very loop that dispatches StasisStart. Running the reject
    inline therefore deadlocks the application for the whole playback timeout.
    """
    app, fake = stasis_app
    await _seed_session(db, test_settings)

    await app.handle_event(inbound_start(caller="+77019990000"))

    # The handler returned immediately, before the prompt finished playing.
    assert app.active_calls == 1
    await _settle()
    assert fake.arguments("play")["media"] == test_settings.sound_unknown


async def test_hangup_cause_arrives_after_stasis_end(
    db, test_settings, stasis_app
) -> None:
    """StasisEnd carries no cause, ChannelDestroyed does.

    The application subscribes to the channel explicitly at StasisStart, which
    is what keeps ChannelDestroyed coming after the channel left Stasis.
    """
    app, fake = stasis_app
    await _seed_session(db, test_settings)

    await app.handle_event(inbound_start())
    await _settle()
    assert fake.arguments("subscribe_channel")["channel_id"] == CH_A

    await app.handle_event(outbound_start())
    await _settle()
    await app.handle_event({"type": "StasisEnd", "channel": {"id": CH_A}})
    await _settle()

    row = (
        await db.execute(
            sa.select(models.Call).where(models.Call.channel_id == CH_A)
        )
    ).scalar_one()
    assert row.status == "answered"
    assert row.hangup_cause is None

    await app.handle_event(destroyed(CH_A, cause=16))
    await _settle()

    await db.refresh(row)
    assert row.hangup_cause == "16"


async def test_call_adopts_the_trace_of_its_session(
    db, test_settings, stasis_app
) -> None:
    """End-to-end trace: the call logs under the trace of POST /sessions."""
    app, _ = stasis_app
    logging_config.bind_trace_id("trace-from-post-sessions")
    created = await _seed_session(db, test_settings)
    assert created.session.trace_id == "trace-from-post-sessions"

    decision = await routing.resolve_call(
        db, proxy_e164=PROXY, caller=A, settings=test_settings
    )
    assert decision.candidate.trace_id == "trace-from-post-sessions"

    logging_config.bind_trace_id("some-other-request")
    await app.handle_event(inbound_start())
    await _settle()
    await app.handle_event(outbound_start())
    await _settle()

    # Every handler of this call logs under the trace of its session, even
    # though each one runs in the context of the event pump.
    assert logging_config.get_trace_id() == "trace-from-post-sessions"
    await app.handle_event(destroyed(CH_A, cause=16))
    await _settle()
    assert logging_config.get_trace_id() == "trace-from-post-sessions"
