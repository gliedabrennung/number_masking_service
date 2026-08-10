"""Request and response models.

Real phone numbers are accepted in any common notation and normalized on the
way in; they are never returned unmasked on the way out.
"""

from __future__ import annotations

import datetime
import uuid
from typing import Annotated, Literal

import pydantic

from app.core import phone

_MAX_TTL_SECONDS = 2_592_000
_MAX_CALLS = 10_000
_MAX_E164_LENGTH = 20
_MAX_EXTERNAL_ID_LENGTH = 128
_MAX_PROVIDER_LENGTH = 64
_MAX_PAGE_SIZE = 500

E164 = Annotated[
    str,
    pydantic.Field(examples=["+77011234567"], max_length=_MAX_E164_LENGTH),
]


def _validate_e164(value: str) -> str:
    """Normalizes a phone number field.

    Raises:
        ValueError: The value is not a phone number; pydantic turns this into
            a 422 response that never echoes the submitted digits.
    """
    try:
        return phone.normalize_e164(value)
    except phone.InvalidPhoneNumberError as invalid:
        raise ValueError(str(invalid)) from invalid


class SessionCreate(pydantic.BaseModel):
    """Body of ``POST /sessions``."""

    model_config = pydantic.ConfigDict(extra="forbid")

    party_a: E164
    party_b: E164
    ttl_seconds: int | None = pydantic.Field(
        default=None, ge=1, le=_MAX_TTL_SECONDS
    )
    external_id: str | None = pydantic.Field(
        default=None, max_length=_MAX_EXTERNAL_ID_LENGTH
    )
    max_calls: int | None = pydantic.Field(default=None, ge=1, le=_MAX_CALLS)
    allow_extension_code: bool = True

    _normalize = pydantic.field_validator("party_a", "party_b")(_validate_e164)


class SessionUpdate(pydantic.BaseModel):
    """Body of ``PATCH /sessions/{id}``."""

    model_config = pydantic.ConfigDict(extra="forbid")

    ttl_seconds: int | None = pydantic.Field(
        default=None, ge=1, le=_MAX_TTL_SECONDS
    )
    expires_at: datetime.datetime | None = None


class PartyOut(pydantic.BaseModel):
    """One side of a session, with its number masked."""

    role: Literal["a", "b"]
    number_masked: str


class SessionOut(pydantic.BaseModel):
    """A masking session as returned by the API."""

    session_id: uuid.UUID
    proxy_number: str
    extension_code: str | None
    status: Literal["active", "expired", "closed"]
    external_id: str | None
    max_calls: int | None
    created_at: datetime.datetime
    expires_at: datetime.datetime
    closed_at: datetime.datetime | None
    parties: list[PartyOut]
    promoted_session_ids: list[uuid.UUID] = pydantic.Field(default_factory=list)


class NumberCreate(pydantic.BaseModel):
    """Body of ``POST /numbers``."""

    model_config = pydantic.ConfigDict(extra="forbid")

    e164: E164
    provider: str | None = pydantic.Field(
        default=None, max_length=_MAX_PROVIDER_LENGTH
    )
    status: Literal["enabled", "disabled"] = "enabled"

    _normalize = pydantic.field_validator("e164")(_validate_e164)


class NumberStatusUpdate(pydantic.BaseModel):
    """Body of ``PATCH /numbers/{e164}``."""

    model_config = pydantic.ConfigDict(extra="forbid")

    status: Literal["enabled", "disabled"]


class NumberOut(pydantic.BaseModel):
    """One proxy number and its current occupancy."""

    e164: str
    status: str
    provider: str | None
    active_sessions: int
    released_at: datetime.datetime | None
    in_cooldown: bool


class PoolOut(pydantic.BaseModel):
    """The whole proxy number pool with aggregate counters."""

    total: int
    enabled: int
    disabled: int
    in_use: int
    in_cooldown: int
    free: int
    active_sessions: int
    numbers: list[NumberOut]


class CallOut(pydantic.BaseModel):
    """One journal entry. Carries no real phone number."""

    id: int
    session_id: uuid.UUID | None
    direction: str | None
    proxy_number: str
    started_at: datetime.datetime
    answered_at: datetime.datetime | None
    ended_at: datetime.datetime | None
    duration_sec: int | None
    status: str
    hangup_cause: str | None


class CallListOut(pydantic.BaseModel):
    """A page of the call journal."""

    total: int
    limit: int
    offset: int
    items: list[CallOut]


class ErrorOut(pydantic.BaseModel):
    """The body of every error response."""

    error: str
    message: str
    trace_id: str | None = None


class HealthOut(pydantic.BaseModel):
    """Liveness response."""

    status: str
    version: str


class ReadyOut(pydantic.BaseModel):
    """Readiness response, one flag per dependency."""

    status: str
    database: bool
    redis: bool
    ari: bool
    ari_ws_connected: bool
