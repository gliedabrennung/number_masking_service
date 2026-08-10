"""The Stasis application: everything that happens once a call hits Asterisk.

Flow of the main scenario:

    StasisStart(inbound)
      -> resolve_call()                   routing decision from the database
      -> ring(A)                          the caller hears ringback
      -> create a mixing bridge, add A
      -> originate(B, callerId = proxy)   the real number of A never leaves
      -> StasisStart(outbound)
      -> answer(A), add B to the bridge   conversation
      -> either leg hangs up
      -> tear the other leg down, write the journal entry

The reverse direction (B to A) uses the same code path: the lookup is by
``(proxy, hash(caller))`` regardless of role, and the direction comes from the
role that matched.
"""

from __future__ import annotations

import asyncio
import collections
import contextlib
import dataclasses
import uuid
from typing import Any

from app.ari import client as ari_client
from app.ari import dtmf
from app.core import config, logging_config
from app.db import engine
from app.services import calls as calls_service
from app.services import routing, webhooks
from app.services import sessions as sessions_service

log = logging_config.get_logger(__name__)

CAUSE_TO_STATUS = {
    16: "answered",
    17: "busy",
    18: "no_answer",
    19: "no_answer",
    21: "rejected",
    34: "busy",
    38: "failed",
    41: "failed",
    42: "failed",
    47: "failed",
}

REJECT_CAUSE = 21
_TEMPORARY_FAILURE_CAUSE = 41
_NO_ANSWER_CAUSE = 19
_ORIGINATE_GRACE_SECONDS = 5
_PLAYBACK_MAX_WAIT_SECONDS = 15.0
_PENDING_CAUSE_LIMIT = 512


@dataclasses.dataclass(slots=True)
class CallState:
    """Everything the application tracks for one inbound call.

    Attributes:
        channel_id: Asterisk channel of the caller.
        caller_hash: Keyed hash of the calling number.
        proxy_e164: The dialled proxy number.
        trace_id: Trace to log this call under, taken from the session.
        call_id: Journal row of this call.
        session_id: Session the call was routed to, once known.
        direction: ``a2b`` or ``b2a``, once known.
        callee_e164: Real number of the callee, once known.
        bridge_id: Mixing bridge holding both legs.
        outbound_channel_id: Asterisk channel of the callee.
        collector: DTMF collector, present only while a PIN is being typed.
        answered: Whether the callee picked up.
        finishing: Guard so teardown runs exactly once.
        final_status: Journal status chosen by the caller of :meth:`_finish`.
        tasks: Background tasks owned by this call.
    """

    channel_id: str
    caller_hash: str
    proxy_e164: str
    trace_id: str | None = None
    call_id: int | None = None
    session_id: uuid.UUID | None = None
    direction: str | None = None
    callee_e164: str | None = None
    bridge_id: str | None = None
    outbound_channel_id: str | None = None
    collector: dtmf.DigitCollector | None = None
    answered: bool = False
    finishing: bool = False
    final_status: str | None = None
    tasks: set[asyncio.Task] = dataclasses.field(default_factory=set)

    def spawn(self, coro: Any) -> asyncio.Task:
        """Starts a task owned by this call and returns it."""
        task = asyncio.create_task(coro)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return task

    def cancel_tasks(self) -> None:
        """Cancels every task owned by this call."""
        for task in list(self.tasks):
            task.cancel()


class MaskingStasisApp:
    """Handles ARI events for the masking Stasis application."""

    def __init__(
        self,
        settings: config.Settings,
        client: ari_client.ARIClient | None = None,
    ) -> None:
        """Initializes the application.

        Args:
            settings: Application settings.
            client: ARI client to use; a new one is created when omitted.
        """
        self.settings = settings
        self.client = client or ari_client.ARIClient(settings)
        self._calls: dict[str, CallState] = {}
        self._outbound_to_inbound: dict[str, str] = {}
        self._playbacks: dict[str, asyncio.Future[None]] = {}
        self._detached: set[asyncio.Task] = set()
        self._pending_cause: collections.OrderedDict[str, int] = (
            collections.OrderedDict()
        )
        self._stopping = asyncio.Event()

    async def run(self) -> None:
        """Consumes the ARI event stream until cancelled."""
        log.info("stasis.starting", app=self.settings.ari_app)
        try:
            async for event in self.client.events():
                if self._stopping.is_set():
                    break
                try:
                    await self.handle_event(event)
                except Exception as exc:
                    log.error(
                        "stasis.handler_error",
                        error=str(exc),
                        event_type=event.get("type"),
                        exc_info=True,
                    )
        except asyncio.CancelledError:
            log.info("stasis.stopped")
            raise

    async def stop(self) -> None:
        """Stops accepting events and releases the ARI client."""
        self._stopping.set()
        for state in list(self._calls.values()):
            state.cancel_tasks()
        for task in list(self._detached):
            task.cancel()
        await self.client.aclose()

    def _bind_context(self, state: CallState) -> None:
        """Restores the logging context of a call.

        Event handlers run in the context of the event pump, so a call being
        torn down would otherwise log under whatever the pump last bound.
        """
        logging_config.bind_trace_id(state.trace_id or state.channel_id)
        logging_config.bind_session_id(
            str(state.session_id) if state.session_id else None
        )

    def _spawn_detached(self, coro: Any) -> asyncio.Task:
        """Runs work for a channel that has no CallState of its own.

        Everything that waits on an ARI event must run outside the event pump,
        otherwise the pump cannot deliver the very event being waited for.
        """
        task = asyncio.create_task(coro)
        self._detached.add(task)
        task.add_done_callback(self._detached.discard)
        return task

    async def _fail_channel(self, channel_id: str) -> None:
        """Announces a technical failure on a channel and releases it."""
        with contextlib.suppress(ari_client.ARIError):
            await self.client.answer(channel_id)
            await self._play(channel_id, self.settings.sound_error)
            await self.client.hangup(
                channel_id, reason_code=_TEMPORARY_FAILURE_CAUSE
            )

    @property
    def ws_connected(self) -> bool:
        """Whether the ARI event websocket is established."""
        return self.client.connected

    @property
    def active_calls(self) -> int:
        """How many calls the application is currently handling."""
        return len(self._calls)

    async def handle_event(self, event: dict) -> None:
        """Routes one ARI event to its handler, ignoring the ones we skip."""
        handler = {
            "StasisStart": self._on_stasis_start,
            "StasisEnd": self._on_stasis_end,
            "ChannelDtmfReceived": self._on_dtmf,
            "ChannelDestroyed": self._on_channel_destroyed,
            "ChannelHangupRequest": self._on_hangup_request,
            "PlaybackFinished": self._on_playback_finished,
            "ChannelEnteredBridge": self._on_entered_bridge,
        }.get(event.get("type", ""))
        if handler is not None:
            await handler(event)

    async def _on_stasis_start(self, event: dict) -> None:
        """Handles a channel entering Stasis, inbound or outbound."""
        args = event.get("args") or []
        channel = event.get("channel") or {}
        channel_id = channel.get("id")
        if not channel_id:
            return

        if args and args[0] == "outbound":
            await self._on_outbound_start(channel_id, args)
            return

        caller = (channel.get("caller") or {}).get("number") or (
            args[1] if len(args) > 1 else ""
        )
        proxy = (channel.get("dialplan") or {}).get("exten") or (
            args[2] if len(args) > 2 else ""
        )
        logging_config.bind_trace_id(channel_id)
        log.info("call.inbound", channel_id=channel_id, proxy=proxy)

        # Scrub the display name at the door: the outbound leg inherits the
        # caller id of this channel, and a softphone usually puts the
        # subscriber's own number there.
        with contextlib.suppress(ari_client.ARIError):
            await self.client.set_variable(channel_id, "CALLERID(name)", "")

        # Keep receiving events for this channel after it leaves Stasis, so
        # the hangup cause of ChannelDestroyed can still be journalled.
        with contextlib.suppress(ari_client.ARIError):
            await self.client.subscribe_channel(
                self.settings.ari_app, channel_id
            )

        try:
            async with engine.session_scope() as db:
                decision = await routing.resolve_call(
                    db, proxy_e164=proxy, caller=caller, settings=self.settings
                )
                call = await calls_service.start_call(
                    db,
                    caller_hash=decision.caller_hash,
                    proxy_e164=decision.proxy_e164,
                    channel_id=channel_id,
                    session_id=(
                        decision.candidate.session_id
                        if decision.candidate
                        else None
                    ),
                    direction=(
                        decision.candidate.direction
                        if decision.candidate
                        else None
                    ),
                )
                call_id = call.id
        except Exception as exc:
            log.error(
                "call.resolve_failed", channel_id=channel_id, error=str(exc)
            )
            self._spawn_detached(self._fail_channel(channel_id))
            return

        state = CallState(
            channel_id=channel_id,
            caller_hash=decision.caller_hash,
            proxy_e164=decision.proxy_e164,
            call_id=call_id,
        )
        self._calls[channel_id] = state

        webhooks.emit(
            "call.started",
            {
                "call_id": call_id,
                "session_id": (
                    str(decision.session_id) if decision.session_id else None
                ),
                "proxy_number": decision.proxy_e164,
                "channel_id": channel_id,
            },
            settings=self.settings,
        )

        if decision.action is routing.Action.REJECT:
            state.spawn(
                self._reject(
                    state,
                    prompt=decision.prompt,
                    status=decision.reject_status,
                )
            )
            return

        if decision.action is routing.Action.ASK_CODE:
            state.collector = dtmf.DigitCollector(
                length=self.settings.ext_code_length,
                digit_timeout=self.settings.dtmf_digit_timeout_seconds,
                total_timeout=self.settings.dtmf_total_timeout_seconds,
            )
            state.spawn(self._ask_for_code(state, decision.candidates or []))
            return

        if decision.candidate is None:
            log.error("call.connect_without_candidate", channel_id=channel_id)
            await self._reject(
                state, prompt=self.settings.sound_error, status="failed"
            )
            return
        state.spawn(self._connect(state, decision.candidate))

    async def _ask_for_code(
        self, state: CallState, candidates: list[routing.Candidate]
    ) -> None:
        """Asks the caller for a PIN and connects the session it selects."""
        try:
            await self.client.answer(state.channel_id)
        except ari_client.ARIError as ari_error:
            log.warning(
                "call.answer_failed",
                channel_id=state.channel_id,
                error=str(ari_error),
            )
            await self._finish(state, status="failed")
            return

        if state.collector is None:
            log.error("call.collector_missing", channel_id=state.channel_id)
            await self._finish(state, status="failed")
            return

        for attempt in range(1, self.settings.dtmf_max_attempts + 1):
            state.collector.reset()
            media = (
                self.settings.sound_enter_code
                if attempt == 1
                else self.settings.sound_wrong_code
            )
            await self._play(state.channel_id, media)
            result = await state.collector.collect()

            log.info(
                "call.code_attempt",
                channel_id=state.channel_id,
                attempt=attempt,
                digits=len(result.digits),
                timed_out=result.timed_out,
            )

            candidate = (
                routing.select_by_code(candidates, result.digits)
                if result.digits
                else None
            )
            if candidate is not None:
                async with engine.session_scope() as db:
                    if state.call_id is not None:
                        await calls_service.attach_session(
                            db,
                            state.call_id,
                            session_id=candidate.session_id,
                            direction=candidate.direction,
                        )
                await self._connect(state, candidate, already_answered=True)
                return

        await self._play(state.channel_id, self.settings.sound_wrong_code)
        await self._finish(state, status="rejected", hangup_cause=REJECT_CAUSE)

    async def _on_dtmf(self, event: dict) -> None:
        """Feeds a received digit to the collector of that channel."""
        channel_id = (event.get("channel") or {}).get("id")
        digit = event.get("digit")
        state = self._calls.get(channel_id or "")
        if state and state.collector and digit:
            state.collector.feed(str(digit))

    async def _connect(
        self,
        state: CallState,
        candidate: routing.Candidate,
        *,
        already_answered: bool = False,
    ) -> None:
        """Bridges the caller with the callee of the chosen session.

        Args:
            state: Call being handled.
            candidate: Session to connect, as chosen by routing or by a PIN.
            already_answered: True when the channel was answered for the PIN
                prompt, so it must not be given ringback again.
        """
        # From here on the call logs under the trace of the request that
        # created the session: one trace_id from POST /sessions to the last
        # call made through it.
        state.trace_id = candidate.trace_id
        state.session_id = candidate.session_id
        self._bind_context(state)
        state.direction = candidate.direction
        state.callee_e164 = candidate.callee_e164

        if candidate.max_calls is not None:
            async with engine.session_scope() as db:
                used = await sessions_service.answered_call_count(
                    db, candidate.session_id
                )
            if used >= candidate.max_calls:
                log.info(
                    "call.limit_reached",
                    session_id=str(candidate.session_id),
                    max_calls=candidate.max_calls,
                )
                await self._reject(
                    state,
                    prompt=self.settings.sound_expired,
                    status="rejected",
                )
                return

        try:
            if not already_answered:
                await self.client.ring(state.channel_id)
            bridge_id = f"masking-{uuid.uuid4()}"
            await self.client.create_bridge(
                bridge_id, name=str(candidate.session_id)
            )
            state.bridge_id = bridge_id
            await self.client.add_to_bridge(bridge_id, state.channel_id)
        except ari_client.ARIError as ari_error:
            log.error(
                "call.bridge_setup_failed",
                channel_id=state.channel_id,
                error=str(ari_error),
            )
            await self._reject(
                state, prompt=self.settings.sound_error, status="failed"
            )
            return

        outbound_id = f"{state.channel_id}-b"
        self._outbound_to_inbound[outbound_id] = state.channel_id
        state.outbound_channel_id = outbound_id

        endpoint = self._endpoint_for(candidate.callee_e164)
        try:
            await self.client.originate(
                endpoint=endpoint,
                app=self.settings.ari_app,
                app_args=f"outbound,{state.channel_id},{candidate.session_id}",
                caller_id=f'"{state.proxy_e164}" <{state.proxy_e164}>',
                timeout=self.settings.originate_timeout,
                originator=state.channel_id,
                channel_id=outbound_id,
            )
        except ari_client.ARIError as ari_error:
            log.error(
                "call.originate_failed",
                channel_id=state.channel_id,
                endpoint=endpoint,
                error=str(ari_error),
            )
            self._outbound_to_inbound.pop(outbound_id, None)
            await self._reject(
                state, prompt=self.settings.sound_error, status="failed"
            )
            return

        log.info(
            "call.originated",
            channel_id=state.channel_id,
            other_channel_id=outbound_id,
            session_id=str(candidate.session_id),
            direction=candidate.direction,
            timeout=self.settings.originate_timeout,
        )
        state.spawn(self._originate_watchdog(state))

    def _endpoint_for(self, callee_e164: str) -> str:
        """Maps an E.164 number to a PJSIP endpoint.

        Prototype: the lab endpoints are named after the number they registered
        with. Production: ``PJSIP/<e164>@trunk-operator``. Both come from
        ``ENDPOINT_TEMPLATE``, which is the point of keeping routing out of the
        dialplan.
        """
        return self.settings.endpoint_template.format(
            number=callee_e164, digits=callee_e164.lstrip("+")
        )

    async def _originate_watchdog(self, state: CallState) -> None:
        """Releases the caller if the second leg never reports an outcome."""
        await asyncio.sleep(
            self.settings.originate_timeout + _ORIGINATE_GRACE_SECONDS
        )
        if state.answered or state.finishing:
            return
        log.warning("call.originate_timeout", channel_id=state.channel_id)
        await self._finish(
            state, status="no_answer", hangup_cause=_NO_ANSWER_CAUSE
        )

    async def _on_outbound_start(
        self, channel_id: str, args: list[str]
    ) -> None:
        """Bridges the answered second leg with the waiting caller."""
        inbound_id = self._outbound_to_inbound.get(channel_id) or (
            args[1] if len(args) > 1 else None
        )
        state = self._calls.get(inbound_id or "")
        if state is not None:
            self._bind_context(state)
        if state is None or state.bridge_id is None:
            log.warning("call.orphan_outbound", channel_id=channel_id)
            with contextlib.suppress(ari_client.ARIError):
                await self.client.hangup(channel_id)
            return

        try:
            with contextlib.suppress(ari_client.ARIError):
                await self.client.ring_stop(state.channel_id)
            await self.client.answer(state.channel_id)
            await self.client.add_to_bridge(state.bridge_id, channel_id)
        except ari_client.ARIError as ari_error:
            log.error(
                "call.bridge_join_failed",
                channel_id=channel_id,
                error=str(ari_error),
            )
            await self._finish(state, status="failed")
            return

        state.answered = True
        async with engine.session_scope() as db:
            if state.call_id is not None:
                await calls_service.mark_answered(
                    db, state.call_id, bridge_id=state.bridge_id
                )

        log.info(
            "call.answered",
            channel_id=state.channel_id,
            other_channel_id=channel_id,
            bridge_id=state.bridge_id,
            session_id=str(state.session_id or ""),
        )
        webhooks.emit(
            "call.answered",
            {
                "call_id": state.call_id,
                "session_id": (
                    str(state.session_id) if state.session_id else None
                ),
                "proxy_number": state.proxy_e164,
            },
            settings=self.settings,
        )

    async def _on_entered_bridge(self, event: dict) -> None:
        """Logs a channel joining a bridge."""
        channel_id = (event.get("channel") or {}).get("id")
        bridge_id = (event.get("bridge") or {}).get("id")
        log.debug(
            "call.entered_bridge", channel_id=channel_id, bridge_id=bridge_id
        )

    async def _on_hangup_request(self, event: dict) -> None:
        """Logs an explicit hangup request from a peer."""
        channel_id = (event.get("channel") or {}).get("id")
        log.debug("call.hangup_requested", channel_id=channel_id)

    async def _on_stasis_end(self, event: dict) -> None:
        """Tears the call down when either leg leaves Stasis."""
        channel_id = (event.get("channel") or {}).get("id") or ""
        if channel_id in self._calls:
            await self._finish(self._calls[channel_id])
        elif channel_id in self._outbound_to_inbound:
            inbound_id = self._outbound_to_inbound.pop(channel_id)
            state = self._calls.get(inbound_id)
            if state is not None:
                await self._finish(state)

    async def _on_channel_destroyed(self, event: dict) -> None:
        """Tears the call down and records the Q.850 cause."""
        channel_id = (event.get("channel") or {}).get("id") or ""
        cause = event.get("cause")

        state = self._calls.get(channel_id)
        if state is not None:
            await self._finish(state, hangup_cause=cause)
            return

        call_id = self._pending_cause.pop(channel_id, None)
        if call_id is not None and cause is not None:
            async with engine.session_scope() as db:
                await calls_service.set_hangup_cause(db, call_id, cause)
            return

        inbound_id = self._outbound_to_inbound.get(channel_id)
        if inbound_id is None:
            return
        state = self._calls.get(inbound_id)
        if state is None:
            self._outbound_to_inbound.pop(channel_id, None)
            return

        if state.answered:
            await self._finish(state, hangup_cause=cause)
            return

        status = CAUSE_TO_STATUS.get(int(cause or 0), "no_answer")
        if status == "answered":
            status = "no_answer"
        log.info(
            "call.callee_unavailable",
            channel_id=state.channel_id,
            status=status,
            hangup_cause=str(cause),
        )
        await self._finish(state, status=status, hangup_cause=cause)

    async def _reject(
        self, state: CallState, *, prompt: str | None, status: str
    ) -> None:
        """Answers, explains and hangs up. A rejected call is never bridged."""
        try:
            await self.client.answer(state.channel_id)
            if prompt:
                await self._play(state.channel_id, prompt)
        except ari_client.ARIError as ari_error:
            log.warning(
                "call.reject_playback_failed",
                channel_id=state.channel_id,
                error=str(ari_error),
            )
        await self._finish(state, status=status, hangup_cause=REJECT_CAUSE)

    async def _finish(
        self,
        state: CallState,
        *,
        status: str | None = None,
        hangup_cause: int | str | None = None,
    ) -> None:
        """Releases both legs, writes the journal entry and forgets the call.

        Runs at most once per call; later invocations return immediately.

        Args:
            state: Call being torn down.
            status: Journal status; inferred from the cause when omitted.
            hangup_cause: Q.850 cause reported by Asterisk.
        """
        if state.finishing:
            return
        state.finishing = True
        state.final_status = status
        self._bind_context(state)

        resolved_status = status or self._infer_status(state, hangup_cause)

        if state.outbound_channel_id:
            with contextlib.suppress(ari_client.ARIError):
                await self.client.hangup(state.outbound_channel_id)
            self._outbound_to_inbound.pop(state.outbound_channel_id, None)

        with contextlib.suppress(ari_client.ARIError):
            await self.client.hangup(
                state.channel_id,
                reason_code=(
                    int(hangup_cause) if _is_int(hangup_cause) else None
                ),
            )

        if state.bridge_id:
            with contextlib.suppress(ari_client.ARIError):
                await self.client.destroy_bridge(state.bridge_id)

        call_id = state.call_id
        if call_id is not None:
            async with engine.session_scope() as db:
                call = await calls_service.finish_call(
                    db,
                    call_id,
                    status=resolved_status,
                    hangup_cause=hangup_cause,
                    session_id=state.session_id,
                    direction=state.direction,
                )
            webhooks.emit(
                "call.ended",
                {
                    "call_id": call_id,
                    "session_id": (
                        str(state.session_id) if state.session_id else None
                    ),
                    "status": call.status if call else resolved_status,
                    "duration_sec": call.duration_sec if call else None,
                    "hangup_cause": (
                        str(hangup_cause) if hangup_cause is not None else None
                    ),
                    "proxy_number": state.proxy_e164,
                },
                settings=self.settings,
            )

        if call_id is not None and hangup_cause is None:
            # StasisEnd carries no cause. ChannelDestroyed does, and the
            # explicit subscription taken at StasisStart guarantees it still
            # reaches us, so the row is completed then.
            self._pending_cause[state.channel_id] = call_id
            while len(self._pending_cause) > _PENDING_CAUSE_LIMIT:
                self._pending_cause.popitem(last=False)

        self._calls.pop(state.channel_id, None)
        state.cancel_tasks()
        logging_config.bind_session_id(None)

    @staticmethod
    def _infer_status(state: CallState, hangup_cause: int | str | None) -> str:
        """Derives the journal status from the answer state and the cause."""
        if state.answered:
            return "answered"
        status = CAUSE_TO_STATUS.get(int(hangup_cause or 0), "failed")
        return "no_answer" if status == "answered" else status

    async def _play(
        self,
        channel_id: str,
        media: str,
        *,
        max_wait: float = _PLAYBACK_MAX_WAIT_SECONDS,
    ) -> None:
        """Plays a prompt and waits for it to end, bounded by max_wait."""
        try:
            playback_id = await self.client.play(channel_id, media)
        except ari_client.ARIError as ari_error:
            log.warning(
                "call.play_failed",
                channel_id=channel_id,
                error=str(ari_error),
            )
            return
        if not playback_id:
            return
        future: asyncio.Future[None] = (
            asyncio.get_running_loop().create_future()
        )
        self._playbacks[playback_id] = future
        try:
            await asyncio.wait_for(future, timeout=max_wait)
        except TimeoutError:
            log.warning("call.play_timeout", channel_id=channel_id)
        finally:
            self._playbacks.pop(playback_id, None)

    async def _on_playback_finished(self, event: dict) -> None:
        """Wakes up whoever is waiting for this playback to end."""
        playback_id = (event.get("playback") or {}).get("id")
        future = self._playbacks.get(playback_id or "")
        if future is not None and not future.done():
            future.set_result(None)


def _is_int(value: Any) -> bool:
    """Returns True when the value can be read as an integer."""
    try:
        int(value)
    except (TypeError, ValueError):
        return False
    return True
