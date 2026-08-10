"""Call routing decisions.

The ARI application asks exactly one question here: a call arrived on proxy
number P from caller C, what do I do? Everything else — bridging, DTMF, media —
is mechanics. Keeping the decision in one place is what makes the service
portable to a real SIP trunk: the dialplan holds no business logic.
"""

from __future__ import annotations

import base64
import dataclasses
import datetime
import enum
import hmac
import uuid

import sqlalchemy as sa
from sqlalchemy import orm
from sqlalchemy.ext import asyncio as sa_asyncio

from app.core import config, crypto, logging_config, phone
from app.db import cache, models

log = logging_config.get_logger(__name__)


class Action(enum.StrEnum):
    """What the ARI application should do with an inbound call."""

    CONNECT = "connect"
    ASK_CODE = "ask_code"
    REJECT = "reject"


@dataclasses.dataclass(slots=True)
class Candidate:
    """One active session the caller could be routed to.

    Attributes:
        session_id: Identifier of the session.
        ext_code: DTMF PIN of the session, or None.
        callee_e164: Real number of the other party, decrypted.
        direction: ``a2b`` when the caller is party ``a``, ``b2a`` otherwise.
        max_calls: Cap on answered calls, or None.
        expires_at: When the session stops accepting calls.
        trace_id: Trace of the request that created the session, so the call
            can be correlated with it in the logs.
    """

    session_id: uuid.UUID
    ext_code: str | None
    callee_e164: str
    direction: str
    max_calls: int | None
    expires_at: datetime.datetime | None = None
    trace_id: str | None = None


@dataclasses.dataclass(slots=True)
class Decision:
    """The routing verdict for one inbound call.

    Attributes:
        action: What to do next.
        caller_hash: Keyed hash of the caller, for the journal.
        proxy_e164: The proxy number that was dialled.
        candidate: The session to bridge, set for ``CONNECT``.
        candidates: Sessions the PIN chooses between, set for ``ASK_CODE``.
        reject_status: Journal status to write, set for ``REJECT``.
        prompt: Sound file to play before hanging up or asking for a PIN.
        session_id: Convenience copy of the chosen session identifier.
    """

    action: Action
    caller_hash: str
    proxy_e164: str
    candidate: Candidate | None = None
    candidates: list[Candidate] | None = None
    reject_status: str = "unknown_caller"
    prompt: str | None = None
    session_id: uuid.UUID | None = None


async def resolve_call(
    db: sa_asyncio.AsyncSession,
    *,
    proxy_e164: str,
    caller: str,
    settings: config.Settings,
) -> Decision:
    """Resolves an inbound call to a routing decision.

    Args:
        db: Open database session.
        proxy_e164: The dialled proxy number, as reported by the dialplan.
        caller: The calling number, as reported by the dialplan.
        settings: Application settings; prompts and secrets come from here.

    Returns:
        The decision. A caller that matches no live session is always
        rejected, and the reason never reveals anything about other people's
        sessions on the same number.
    """
    try:
        proxy = phone.normalize_e164(proxy_e164)
    except phone.InvalidPhoneNumberError:
        proxy = proxy_e164
    try:
        caller_e164 = phone.normalize_e164(caller)
    except phone.InvalidPhoneNumberError:
        log.info(
            "routing.caller_not_e164",
            caller_masked=phone.mask_e164(caller),
            proxy=proxy,
        )
        return Decision(
            action=Action.REJECT,
            caller_hash="",
            proxy_e164=proxy,
            reject_status="unknown_caller",
            prompt=settings.sound_unknown,
        )

    caller_hash = crypto.party_hash(caller_e164, settings.party_hash_secret)

    cached = await cache.cache_get_json(cache.lookup_key(proxy, caller_hash))
    if cached is not None:
        decision = _decision_from_cache(cached, proxy, caller_hash, settings)
        if decision is not None:
            return decision

    candidates = await _load_candidates(
        db, proxy=proxy, caller_hash=caller_hash, settings=settings
    )

    if not candidates:
        return await _reject_without_session(
            db, proxy=proxy, caller_hash=caller_hash, settings=settings
        )

    if len(candidates) == 1:
        candidate = candidates[0]
        await _cache_single(proxy, caller_hash, candidate, settings)
        return Decision(
            action=Action.CONNECT,
            caller_hash=caller_hash,
            proxy_e164=proxy,
            candidate=candidate,
            session_id=candidate.session_id,
        )

    with_code = [item for item in candidates if item.ext_code]
    if len(with_code) != len(candidates):
        log.warning(
            "routing.ambiguous_without_code",
            proxy=proxy,
            caller_hash=caller_hash,
            total=len(candidates),
            with_code=len(with_code),
        )
    if not with_code:
        return Decision(
            action=Action.REJECT,
            caller_hash=caller_hash,
            proxy_e164=proxy,
            reject_status="failed",
            prompt=settings.sound_error,
        )

    return Decision(
        action=Action.ASK_CODE,
        caller_hash=caller_hash,
        proxy_e164=proxy,
        candidates=with_code,
        prompt=settings.sound_enter_code,
    )


def select_by_code(candidates: list[Candidate], code: str) -> Candidate | None:
    """Returns the candidate whose PIN matches, or None."""
    for candidate in candidates:
        if candidate.ext_code and hmac.compare_digest(candidate.ext_code, code):
            return candidate
    return None


async def _reject_without_session(
    db: sa_asyncio.AsyncSession,
    *,
    proxy: str,
    caller_hash: str,
    settings: config.Settings,
) -> Decision:
    """Builds the rejection for a caller with no live session on the number."""
    had_session = await _had_session(db, proxy=proxy, caller_hash=caller_hash)
    status = "expired" if had_session else "unknown_caller"
    prompt = settings.sound_expired if had_session else settings.sound_unknown
    await cache.cache_set_json(
        cache.lookup_key(proxy, caller_hash),
        {"miss": True, "status": status},
        ttl=cache.NEGATIVE_TTL_SECONDS,
    )
    log.info(
        "routing.no_session",
        proxy=proxy,
        caller_hash=caller_hash,
        reject_status=status,
    )
    return Decision(
        action=Action.REJECT,
        caller_hash=caller_hash,
        proxy_e164=proxy,
        reject_status=status,
        prompt=prompt,
    )


async def _load_candidates(
    db: sa_asyncio.AsyncSession,
    *,
    proxy: str,
    caller_hash: str,
    settings: config.Settings,
) -> list[Candidate]:
    """Returns every live session of this caller on this proxy number."""
    statement = (
        sa.select(models.Session, models.SessionParty.role)
        .join(
            models.SessionParty,
            models.SessionParty.session_id == models.Session.id,
        )
        .join(models.Number, models.Number.id == models.Session.number_id)
        .options(orm.selectinload(models.Session.parties))
        .where(
            models.Number.e164 == proxy,
            models.SessionParty.party_hash == caller_hash,
            models.SessionParty.is_active.is_(True),
            models.Session.status == "active",
            models.Session.expires_at > datetime.datetime.now(datetime.UTC),
        )
        .order_by(models.Session.created_at.desc())
    )
    rows = (await db.execute(statement)).unique().all()

    key = settings.encryption_key_bytes
    candidates: list[Candidate] = []
    for session, caller_role in rows:
        other = next(
            (party for party in session.parties if party.role != caller_role),
            None,
        )
        if other is None:
            continue
        candidates.append(
            Candidate(
                session_id=session.id,
                ext_code=session.ext_code,
                callee_e164=crypto.decrypt_e164(other.party_e164_enc, key),
                direction="a2b" if caller_role == "a" else "b2a",
                max_calls=session.max_calls,
                expires_at=session.expires_at,
                trace_id=session.trace_id,
            )
        )
    return candidates


async def _had_session(
    db: sa_asyncio.AsyncSession, *, proxy: str, caller_hash: str
) -> bool:
    """Returns True when this caller once had a session on this number.

    Drives the choice of prompt: "your connection has expired" versus "the
    number is unavailable". It only ever reveals the caller's own past.
    """
    statement = (
        sa.select(models.Session.id)
        .join(
            models.SessionParty,
            models.SessionParty.session_id == models.Session.id,
        )
        .join(models.Number, models.Number.id == models.Session.number_id)
        .where(
            models.Number.e164 == proxy,
            models.SessionParty.party_hash == caller_hash,
        )
        .limit(1)
    )
    return (await db.execute(statement)).first() is not None


async def _cache_single(
    proxy: str,
    caller_hash: str,
    candidate: Candidate,
    settings: config.Settings,
) -> None:
    """Caches an unambiguous route.

    Only the *ciphertext* of the callee number is cached: Redis never holds a
    readable phone number, and it never holds the AES key either. The entry
    must not outlive the session, so its time to live is capped by the session
    expiry — a cached route for an expired session would connect a call the
    database would have refused.
    """
    if candidate.max_calls is not None:
        return

    remaining = cache.LOOKUP_TTL_SECONDS
    if candidate.expires_at is not None:
        left = candidate.expires_at - datetime.datetime.now(datetime.UTC)
        remaining = min(int(left.total_seconds()), cache.LOOKUP_TTL_SECONDS)
    if remaining <= 0:
        return

    payload = {
        "session_id": str(candidate.session_id),
        "direction": candidate.direction,
        "callee_enc": base64.b64encode(
            crypto.encrypt_e164(
                candidate.callee_e164, settings.encryption_key_bytes
            )
        ).decode(),
        "ext_code": candidate.ext_code,
        "trace_id": candidate.trace_id,
        "expires_at": (
            candidate.expires_at.isoformat() if candidate.expires_at else None
        ),
    }
    await cache.cache_set_json(
        cache.lookup_key(proxy, caller_hash), payload, ttl=remaining
    )


def _decision_from_cache(
    payload: dict[str, object],
    proxy: str,
    caller_hash: str,
    settings: config.Settings,
) -> Decision | None:
    """Rebuilds a decision from a cache entry, or None to fall back to SQL."""
    try:
        if payload.get("miss"):
            status = str(payload.get("status", "unknown_caller"))
            prompt = (
                settings.sound_expired
                if status == "expired"
                else settings.sound_unknown
            )
            return Decision(
                action=Action.REJECT,
                caller_hash=caller_hash,
                proxy_e164=proxy,
                reject_status=status,
                prompt=prompt,
            )
        raw_expiry = payload.get("expires_at")
        expires_at = (
            datetime.datetime.fromisoformat(str(raw_expiry))
            if raw_expiry
            else None
        )
        if expires_at is not None and expires_at <= datetime.datetime.now(
            datetime.UTC
        ):
            return None

        callee = crypto.decrypt_e164(
            base64.b64decode(str(payload["callee_enc"])),
            settings.encryption_key_bytes,
        )
        ext_code = payload.get("ext_code")
        candidate = Candidate(
            session_id=uuid.UUID(str(payload["session_id"])),
            ext_code=str(ext_code) if ext_code else None,
            callee_e164=callee,
            direction=str(payload["direction"]),
            max_calls=None,
            expires_at=expires_at,
            trace_id=(
                str(payload["trace_id"]) if payload.get("trace_id") else None
            ),
        )
        return Decision(
            action=Action.CONNECT,
            caller_hash=caller_hash,
            proxy_e164=proxy,
            candidate=candidate,
            session_id=candidate.session_id,
        )
    except Exception as exc:
        log.warning("routing.cache_unusable", error=str(exc))
        return None
