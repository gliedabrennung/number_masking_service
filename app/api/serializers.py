"""Model to schema conversion.

This is the only place that decrypts a stored phone number, and it masks the
result immediately: no API response ever carries a full subscriber number.
"""

from __future__ import annotations

import datetime
import uuid

from app.api import schemas
from app.core import config, crypto, phone
from app.db import models


def session_out(
    session: models.Session,
    *,
    settings: config.Settings,
    promoted: list[uuid.UUID] | None = None,
) -> schemas.SessionOut:
    """Renders a session for the API.

    Args:
        session: Session with its number and parties already loaded.
        settings: Application settings holding the encryption key.
        promoted: Sessions that received a PIN because of this allocation.

    Returns:
        The response model, with both party numbers masked.
    """
    key = settings.encryption_key_bytes
    parties = [
        schemas.PartyOut(
            role=party.role,
            number_masked=phone.mask_e164(
                crypto.decrypt_e164(party.party_e164_enc, key)
            ),
        )
        for party in sorted(session.parties, key=lambda item: item.role)
    ]
    return schemas.SessionOut(
        session_id=session.id,
        proxy_number=session.number.e164,
        extension_code=session.ext_code,
        status=session.status,
        external_id=session.external_id,
        max_calls=session.max_calls,
        created_at=session.created_at,
        expires_at=session.expires_at,
        closed_at=session.closed_at,
        parties=parties,
        promoted_session_ids=promoted or [],
    )


def call_out(call: models.Call) -> schemas.CallOut:
    """Renders one journal entry for the API."""
    return schemas.CallOut(
        id=call.id,
        session_id=call.session_id,
        direction=call.direction,
        proxy_number=call.proxy_e164,
        started_at=call.started_at,
        answered_at=call.answered_at,
        ended_at=call.ended_at,
        duration_sec=call.duration_sec,
        status=call.status,
        hangup_cause=call.hangup_cause,
    )


def number_out(
    number: models.Number,
    active_sessions: int,
    *,
    settings: config.Settings,
) -> schemas.NumberOut:
    """Renders one pool number, resolving whether it is in cooldown.

    Args:
        number: The pool row.
        active_sessions: How many active sessions live on this number.
        settings: Application settings holding the cooldown window.

    Returns:
        The response model.
    """
    cooldown_edge = datetime.datetime.now(datetime.UTC) - datetime.timedelta(
        hours=settings.number_cooldown_hours
    )
    in_cooldown = (
        active_sessions == 0
        and number.released_at is not None
        and number.released_at > cooldown_edge
    )
    return schemas.NumberOut(
        e164=number.e164,
        status=number.status,
        provider=number.provider,
        active_sessions=active_sessions,
        released_at=number.released_at,
        in_cooldown=in_cooldown,
    )
