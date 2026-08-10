"""Proxy number allocation.

Two modes, tried in order:

``exclusive``
    Neither party holds any active session on the number, and the number is out
    of cooldown. Routing by ``(proxy, caller_hash)`` then yields exactly one
    session, so the call connects straight through. This is the normal path.

``extension``
    Fallback when no conflict-free number is left. The number is shared and the
    session gets a DTMF PIN. Because a caller can then match more than one
    session on the same number, every *other* active session of that caller on
    that number must also carry a PIN — otherwise it would become unreachable.
    Such sessions are promoted inside the same transaction, and the promotion
    is reported to the API caller and over the ``session.updated`` webhook.

``EXT_CODE_MODE`` selects the policy: ``auto`` (default), ``always`` (every
session gets a PIN, so no promotion ever happens) or ``never`` (409 instead of
a shared number).
"""

from __future__ import annotations

import dataclasses
import uuid

import sqlalchemy as sa
from sqlalchemy.ext import asyncio as sa_asyncio

from app.core import config, crypto, errors, logging_config
from app.db import models

log = logging_config.get_logger(__name__)

EXT_CODE_GENERATION_ATTEMPTS = 20
_MAX_CANDIDATES = 20


@dataclasses.dataclass(slots=True)
class Allocation:
    """The outcome of allocating a proxy number to a pair.

    Attributes:
        number_id: Primary key of the allocated number.
        proxy_e164: The allocated number in E.164 form.
        ext_code: DTMF PIN issued to the new session, or None.
        mode: ``exclusive`` or ``extension``.
        promoted_session_ids: Sessions that received a PIN as a side effect of
            this allocation.
    """

    number_id: int
    proxy_e164: str
    ext_code: str | None
    mode: str
    promoted_session_ids: list[uuid.UUID] = dataclasses.field(
        default_factory=list
    )


_CANDIDATE_PREDICATE = """
    n.status = 'enabled'
    AND (
        NOT :number_scope_cooldown
        OR n.released_at IS NULL
        OR n.released_at < now() - make_interval(hours => :cooldown_hours)
    )
    AND NOT EXISTS (
        SELECT 1 FROM session_parties sp
        WHERE sp.number_id = n.id
          AND sp.party_hash = ANY(:hashes)
          AND NOT sp.is_active
          AND sp.released_at IS NOT NULL
          AND sp.released_at > now() - make_interval(hours => :cooldown_hours)
    )
"""

_EXCLUSIVE_SQL = sa.text(
    f"""
    SELECT n.id, n.e164
    FROM numbers n
    WHERE {_CANDIDATE_PREDICATE}
      AND NOT EXISTS (
          SELECT 1 FROM session_parties sp
          WHERE sp.number_id = n.id
            AND sp.party_hash = ANY(:hashes)
            AND sp.is_active
      )
    ORDER BY n.released_at NULLS FIRST, n.id
    LIMIT 1
    FOR UPDATE OF n SKIP LOCKED
    """
)

_EXTENSION_CANDIDATES_SQL = sa.text(
    f"""
    SELECT n.id, n.e164,
           (SELECT count(*) FROM sessions s
             WHERE s.number_id = n.id AND s.status = 'active') AS active_count
    FROM numbers n
    WHERE {_CANDIDATE_PREDICATE}
    ORDER BY active_count ASC, n.id
    LIMIT {_MAX_CANDIDATES}
    """
)

_LOCK_NUMBER_SQL = sa.text(
    "SELECT id, e164 FROM numbers WHERE id = :number_id FOR UPDATE SKIP LOCKED"
)

_ACTIVE_EXT_CODES_SQL = sa.text(
    "SELECT ext_code FROM sessions "
    "WHERE number_id = :number_id AND status = 'active' "
    "AND ext_code IS NOT NULL"
)

_SESSIONS_TO_PROMOTE_SQL = sa.text(
    """
    SELECT s.id
    FROM sessions s
    WHERE s.number_id = :number_id
      AND s.status = 'active'
      AND s.ext_code IS NULL
      AND EXISTS (
          SELECT 1 FROM session_parties sp
          WHERE sp.session_id = s.id AND sp.party_hash = ANY(:hashes)
      )
    FOR UPDATE OF s
    """
)


async def allocate_number(
    db: sa_asyncio.AsyncSession,
    *,
    hashes: list[str],
    settings: config.Settings,
    allow_extension_code: bool = True,
) -> Allocation:
    """Picks and row-locks a proxy number for a new session.

    Must be called inside an open transaction. The chosen number stays locked
    (``FOR UPDATE``) until the caller commits, which is what makes concurrent
    ``POST /sessions`` safe.

    Args:
        db: Open database session, inside a transaction.
        hashes: Party hashes of both sides of the new session.
        settings: Application settings; cooldown and PIN policy come from here.
        allow_extension_code: When False, refuse rather than share a number.

    Returns:
        The allocation, including any sessions promoted to carry a PIN.

    Raises:
        NoNumberAvailableError: No number can serve this pair.
    """
    params = {
        "hashes": hashes,
        "cooldown_hours": settings.number_cooldown_hours,
        "number_scope_cooldown": settings.cooldown_scope == "number",
    }

    if settings.ext_code_mode != "always":
        row = (await db.execute(_EXCLUSIVE_SQL, params)).first()
        if row is not None:
            return Allocation(
                number_id=row.id,
                proxy_e164=row.e164,
                ext_code=None,
                mode="exclusive",
            )

    if settings.ext_code_mode == "never" or not allow_extension_code:
        raise errors.NoNumberAvailableError(
            "no proxy number is free for this pair and extension codes are"
            " disabled"
        )

    return await _allocate_with_extension_code(
        db, hashes=hashes, params=params, settings=settings
    )


async def _allocate_with_extension_code(
    db: sa_asyncio.AsyncSession,
    *,
    hashes: list[str],
    params: dict[str, object],
    settings: config.Settings,
) -> Allocation:
    """Allocates a shared number, promoting conflicting sessions to PINs.

    Raises:
        NoNumberAvailableError: Every candidate is locked, full or out of PINs.
    """
    candidates = (await db.execute(_EXTENSION_CANDIDATES_SQL, params)).all()
    for candidate in candidates:
        if candidate.active_count >= settings.ext_code_max_per_number:
            continue
        locked = (
            await db.execute(_LOCK_NUMBER_SQL, {"number_id": candidate.id})
        ).first()
        if locked is None:
            continue

        used = {
            row.ext_code
            for row in (
                await db.execute(
                    _ACTIVE_EXT_CODES_SQL, {"number_id": locked.id}
                )
            ).all()
        }
        code = _fresh_code(used, settings.ext_code_length)
        if code is None:
            continue
        used.add(code)

        promoted = await _promote_conflicting_sessions(
            db,
            number_id=locked.id,
            hashes=hashes,
            used_codes=used,
            settings=settings,
        )
        if promoted is None:
            continue

        log.info(
            "allocation.extension",
            proxy=locked.e164,
            promoted=len(promoted),
            active_count=candidate.active_count,
        )
        return Allocation(
            number_id=locked.id,
            proxy_e164=locked.e164,
            ext_code=code,
            mode="extension",
            promoted_session_ids=promoted,
        )

    raise errors.NoNumberAvailableError("proxy number pool is exhausted")


async def _promote_conflicting_sessions(
    db: sa_asyncio.AsyncSession,
    *,
    number_id: int,
    hashes: list[str],
    used_codes: set[str],
    settings: config.Settings,
) -> list[uuid.UUID] | None:
    """Issues a PIN to every PIN-less active session of these parties.

    Args:
        db: Open database session, inside a transaction.
        number_id: The number about to be shared.
        hashes: Party hashes of the new session.
        used_codes: PINs already taken on this number; extended in place.
        settings: Application settings; the PIN length comes from here.

    Returns:
        The identifiers of the promoted sessions, or None when the PIN space of
        this number ran out before every session could be promoted.
    """
    promoted: list[uuid.UUID] = []
    rows = (
        await db.execute(
            _SESSIONS_TO_PROMOTE_SQL,
            {"number_id": number_id, "hashes": hashes},
        )
    ).all()
    for row in rows:
        code = _fresh_code(used_codes, settings.ext_code_length)
        if code is None:
            return None
        used_codes.add(code)
        await db.execute(
            sa.update(models.Session)
            .where(models.Session.id == row.id)
            .values(ext_code=code)
            .execution_options(synchronize_session="fetch")
        )
        promoted.append(row.id)
    return promoted


def _fresh_code(used: set[str], length: int) -> str | None:
    """Returns a PIN that is not in ``used``, or None after enough tries."""
    for _ in range(EXT_CODE_GENERATION_ATTEMPTS):
        code = crypto.generate_ext_code(length)
        if code not in used:
            return code
    return None
