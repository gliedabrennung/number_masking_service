"""Session CRUD under ``/api/v1/sessions``."""

from __future__ import annotations

import datetime
import uuid

import fastapi
import sqlalchemy as sa
from sqlalchemy.ext import asyncio as sa_asyncio

from app.api import deps, schemas, serializers
from app.core import config
from app.db import models
from app.services import calls as calls_service
from app.services import sessions as sessions_service
from app.services import webhooks

_DEFAULT_PAGE_SIZE = 50
_MAX_PAGE_SIZE = 500
_DEFAULT_CALL_PAGE_SIZE = 100

router = fastapi.APIRouter(
    prefix="/sessions",
    tags=["sessions"],
    dependencies=[fastapi.Depends(deps.require_api_key)],
)


@router.post(
    "",
    response_model=schemas.SessionOut,
    status_code=fastapi.status.HTTP_201_CREATED,
)
async def create_session(
    payload: schemas.SessionCreate,
    db: sa_asyncio.AsyncSession = fastapi.Depends(deps.db_session),
    settings: config.Settings = fastapi.Depends(deps.settings_dep),
) -> schemas.SessionOut:
    """Creates a session and returns the proxy number to dial."""
    created = await sessions_service.create_session(
        db,
        party_a=payload.party_a,
        party_b=payload.party_b,
        settings=settings,
        ttl_seconds=payload.ttl_seconds,
        external_id=payload.external_id,
        max_calls=payload.max_calls,
        allow_extension_code=payload.allow_extension_code,
    )
    session = created.session
    await db.refresh(session, ["number", "parties"])

    for promoted_id in created.allocation.promoted_session_ids:
        webhooks.emit(
            "session.updated",
            {
                "session_id": str(promoted_id),
                "reason": "extension_code_assigned",
            },
            settings=settings,
        )

    return serializers.session_out(
        session,
        settings=settings,
        promoted=created.allocation.promoted_session_ids,
    )


@router.get("/{session_id}", response_model=schemas.SessionOut)
async def get_session(
    session_id: uuid.UUID,
    db: sa_asyncio.AsyncSession = fastapi.Depends(deps.db_session),
    settings: config.Settings = fastapi.Depends(deps.settings_dep),
) -> schemas.SessionOut:
    """Returns one session; the subscriber numbers are masked."""
    session = await sessions_service.get_session(db, session_id)
    return serializers.session_out(session, settings=settings)


@router.get("", response_model=list[schemas.SessionOut])
async def list_sessions(
    session_status: str | None = fastapi.Query(default=None, alias="status"),
    external_id: str | None = None,
    limit: int = fastapi.Query(
        default=_DEFAULT_PAGE_SIZE, ge=1, le=_MAX_PAGE_SIZE
    ),
    offset: int = fastapi.Query(default=0, ge=0),
    db: sa_asyncio.AsyncSession = fastapi.Depends(deps.db_session),
    settings: config.Settings = fastapi.Depends(deps.settings_dep),
) -> list[schemas.SessionOut]:
    """Returns a page of sessions, newest first."""
    statement = (
        sa.select(models.Session)
        .order_by(models.Session.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    if session_status:
        statement = statement.where(models.Session.status == session_status)
    if external_id:
        statement = statement.where(models.Session.external_id == external_id)
    rows = (await db.execute(statement)).unique().scalars().all()
    return [serializers.session_out(row, settings=settings) for row in rows]


@router.patch("/{session_id}", response_model=schemas.SessionOut)
async def update_session(
    session_id: uuid.UUID,
    payload: schemas.SessionUpdate,
    db: sa_asyncio.AsyncSession = fastapi.Depends(deps.db_session),
    settings: config.Settings = fastapi.Depends(deps.settings_dep),
) -> schemas.SessionOut:
    """Extends the lifetime of an active session."""
    session = await sessions_service.extend_session(
        db,
        session_id,
        settings=settings,
        ttl_seconds=payload.ttl_seconds,
        expires_at=payload.expires_at,
    )
    return serializers.session_out(session, settings=settings)


@router.delete("/{session_id}", response_model=schemas.SessionOut)
async def close_session(
    session_id: uuid.UUID,
    db: sa_asyncio.AsyncSession = fastapi.Depends(deps.db_session),
    settings: config.Settings = fastapi.Depends(deps.settings_dep),
) -> schemas.SessionOut:
    """Closes a session early.

    A conversation that is already up is not torn down; the session simply
    stops accepting new calls.
    """
    session = await sessions_service.close_session(db, session_id)
    return serializers.session_out(session, settings=settings)


@router.get("/{session_id}/calls", response_model=schemas.CallListOut)
async def session_calls(
    session_id: uuid.UUID,
    limit: int = fastapi.Query(
        default=_DEFAULT_CALL_PAGE_SIZE, ge=1, le=_MAX_PAGE_SIZE
    ),
    offset: int = fastapi.Query(default=0, ge=0),
    date_from: datetime.datetime | None = fastapi.Query(
        default=None, alias="from"
    ),
    date_to: datetime.datetime | None = fastapi.Query(default=None, alias="to"),
    db: sa_asyncio.AsyncSession = fastapi.Depends(deps.db_session),
) -> schemas.CallListOut:
    """Returns the call journal of one session."""
    await sessions_service.get_session(db, session_id)
    items, total = await calls_service.list_calls(
        db,
        session_id=session_id,
        date_from=date_from,
        date_to=date_to,
        limit=limit,
        offset=offset,
    )
    return schemas.CallListOut(
        total=total,
        limit=limit,
        offset=offset,
        items=[serializers.call_out(call) for call in items],
    )
