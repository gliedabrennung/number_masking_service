"""The proxy number pool under ``/api/v1/numbers``."""

from __future__ import annotations

import fastapi
from sqlalchemy.ext import asyncio as sa_asyncio

from app.api import deps, schemas, serializers
from app.core import config
from app.services import numbers as numbers_service

router = fastapi.APIRouter(
    prefix="/numbers",
    tags=["numbers"],
    dependencies=[fastapi.Depends(deps.require_api_key)],
)


@router.get("", response_model=schemas.PoolOut)
async def get_pool(
    db: sa_asyncio.AsyncSession = fastapi.Depends(deps.db_session),
    settings: config.Settings = fastapi.Depends(deps.settings_dep),
) -> schemas.PoolOut:
    """Returns the pool: totals plus one entry per number."""
    stats = await numbers_service.pool_stats(db, settings=settings)
    rows = await numbers_service.list_numbers(db)
    return schemas.PoolOut(
        total=stats.total,
        enabled=stats.enabled,
        disabled=stats.disabled,
        in_use=stats.in_use,
        in_cooldown=stats.in_cooldown,
        free=stats.free,
        active_sessions=stats.active_sessions,
        numbers=[
            serializers.number_out(number, count, settings=settings)
            for number, count in rows
        ],
    )


@router.post(
    "",
    response_model=schemas.NumberOut,
    status_code=fastapi.status.HTTP_201_CREATED,
)
async def add_number(
    payload: schemas.NumberCreate,
    db: sa_asyncio.AsyncSession = fastapi.Depends(deps.db_session),
    settings: config.Settings = fastapi.Depends(deps.settings_dep),
) -> schemas.NumberOut:
    """Adds a number to the pool."""
    number = await numbers_service.add_number(
        db, e164=payload.e164, provider=payload.provider, status=payload.status
    )
    return serializers.number_out(number, 0, settings=settings)


@router.patch("/{e164}", response_model=schemas.NumberOut)
async def update_number(
    e164: str,
    payload: schemas.NumberStatusUpdate,
    db: sa_asyncio.AsyncSession = fastapi.Depends(deps.db_session),
    settings: config.Settings = fastapi.Depends(deps.settings_dep),
) -> schemas.NumberOut:
    """Enables or disables a number.

    Disabling never breaks live sessions; it only removes the number from
    future allocations.
    """
    number = await numbers_service.set_number_status(
        db, e164=e164, status=payload.status
    )
    counts = {
        row.id: count for row, count in await numbers_service.list_numbers(db)
    }
    return serializers.number_out(
        number, counts.get(number.id, 0), settings=settings
    )
