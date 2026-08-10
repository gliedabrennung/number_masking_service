"""The aggregated call journal under ``/api/v1/calls``."""

from __future__ import annotations

import datetime
import uuid

import fastapi
from sqlalchemy.ext import asyncio as sa_asyncio

from app.api import deps, schemas, serializers
from app.services import calls as calls_service

_DEFAULT_PAGE_SIZE = 100
_MAX_PAGE_SIZE = 500

router = fastapi.APIRouter(
    prefix="/calls",
    tags=["calls"],
    dependencies=[fastapi.Depends(deps.require_api_key)],
)


@router.get("", response_model=schemas.CallListOut)
async def list_calls(
    date_from: datetime.datetime | None = fastapi.Query(
        default=None, alias="from"
    ),
    date_to: datetime.datetime | None = fastapi.Query(default=None, alias="to"),
    call_status: str | None = fastapi.Query(default=None, alias="status"),
    session_id: uuid.UUID | None = None,
    limit: int = fastapi.Query(
        default=_DEFAULT_PAGE_SIZE, ge=1, le=_MAX_PAGE_SIZE
    ),
    offset: int = fastapi.Query(default=0, ge=0),
    db: sa_asyncio.AsyncSession = fastapi.Depends(deps.db_session),
) -> schemas.CallListOut:
    """Returns a page of the journal across all sessions, newest first."""
    items, total = await calls_service.list_calls(
        db,
        session_id=session_id,
        status=call_status,
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
