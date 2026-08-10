"""Liveness, readiness and Prometheus metrics. No authentication."""

from __future__ import annotations

import fastapi
import sqlalchemy as sa
from sqlalchemy.ext import asyncio as sa_asyncio

import app as app_package
from app.api import deps, schemas
from app.core import config
from app.db import cache, engine, models
from app.services import numbers as numbers_service

_PROMETHEUS_CONTENT_TYPE = "text/plain; version=0.0.4"
# Upper bounds, in seconds, of the call setup histogram.
_SETUP_BUCKETS = (0.5, 1.0, 2.0, 5.0, 10.0, 30.0)

router = fastapi.APIRouter(tags=["ops"])


@router.get("/health", response_model=schemas.HealthOut)
async def health() -> schemas.HealthOut:
    """Returns liveness: the process is up and serving."""
    return schemas.HealthOut(status="ok", version=app_package.__version__)


@router.get("/ready", response_model=schemas.ReadyOut)
async def ready(
    request: fastapi.Request, response: fastapi.Response
) -> schemas.ReadyOut:
    """Returns readiness, one flag per dependency, 503 when not ready.

    The database session is opened by hand rather than injected: a dependency
    that fails to connect would abort the request with a 500 and hide which
    component is broken.
    """
    db_ok = True
    try:
        factory = engine.get_sessionmaker()
        async with factory() as session:
            await session.execute(sa.text("SELECT 1"))
    except Exception:
        db_ok = False

    redis_ok = True
    try:
        await cache.get_cache().ping()
    except Exception:
        redis_ok = False

    ari_client = getattr(request.app.state, "ari_client", None)
    ari_ok = await ari_client.ping() if ari_client is not None else False
    stasis = getattr(request.app.state, "stasis_app", None)
    ws_ok = bool(stasis.ws_connected) if stasis is not None else False

    ready_now = db_ok and redis_ok and ari_ok
    if not ready_now:
        response.status_code = fastapi.status.HTTP_503_SERVICE_UNAVAILABLE
    return schemas.ReadyOut(
        status="ready" if ready_now else "degraded",
        database=db_ok,
        redis=redis_ok,
        ari=ari_ok,
        ari_ws_connected=ws_ok,
    )


@router.get("/metrics", response_class=fastapi.Response)
async def metrics(
    request: fastapi.Request,
    db: sa_asyncio.AsyncSession = fastapi.Depends(deps.db_session),
    settings: config.Settings = fastapi.Depends(deps.settings_dep),
) -> fastapi.Response:
    """Returns application metrics in the Prometheus text format.

    Asterisk exposes its own channel and bridge metrics separately, through
    ``res_prometheus`` on the ARI HTTP socket.
    """
    active_sessions = int(
        await db.scalar(
            sa.select(sa.func.count())
            .select_from(models.Session)
            .where(models.Session.status == "active")
        )
        or 0
    )
    pool = await numbers_service.pool_stats(db, settings=settings)
    call_rows = (
        await db.execute(
            sa.select(models.Call.status, sa.func.count()).group_by(
                models.Call.status
            )
        )
    ).all()

    setup_seconds = sa.func.extract(
        "epoch", models.Call.answered_at - models.Call.started_at
    )
    setup_rows = (
        await db.execute(
            sa.select(
                sa.func.count(),
                sa.func.coalesce(sa.func.sum(setup_seconds), 0.0),
                *[
                    sa.func.count().filter(setup_seconds <= bound)
                    for bound in _SETUP_BUCKETS
                ],
            ).where(models.Call.answered_at.is_not(None))
        )
    ).one()

    stasis = getattr(request.app.state, "stasis_app", None)
    ws_connected = 1 if (stasis is not None and stasis.ws_connected) else 0
    active_calls = stasis.active_calls if stasis is not None else 0

    lines = [
        "# HELP masking_sessions_active Active masking sessions",
        "# TYPE masking_sessions_active gauge",
        f"masking_sessions_active {active_sessions}",
        "# HELP masking_numbers_free Pool numbers free for a new pair",
        "# TYPE masking_numbers_free gauge",
        f"masking_numbers_free {pool.free}",
        "# HELP masking_numbers_cooldown Pool numbers in cooldown",
        "# TYPE masking_numbers_cooldown gauge",
        f"masking_numbers_cooldown {pool.in_cooldown}",
        "# HELP masking_calls_total Calls by final status",
        "# TYPE masking_calls_total counter",
    ]
    lines.extend(
        f'masking_calls_total{{status="{call_status}"}} {int(count)}'
        for call_status, count in call_rows
    )
    setup_count = int(setup_rows[0])
    setup_sum = float(setup_rows[1])
    lines += [
        "# HELP masking_call_setup_duration_seconds Time from the inbound call"
        " to the moment the callee answered",
        "# TYPE masking_call_setup_duration_seconds histogram",
    ]
    lines.extend(
        f'masking_call_setup_duration_seconds_bucket{{le="{bound}"}} '
        f"{int(setup_rows[2 + index])}"
        for index, bound in enumerate(_SETUP_BUCKETS)
    )
    lines += [
        f'masking_call_setup_duration_seconds_bucket{{le="+Inf"}} '
        f"{setup_count}",
        f"masking_call_setup_duration_seconds_sum {setup_sum:.3f}",
        f"masking_call_setup_duration_seconds_count {setup_count}",
        "# HELP masking_calls_in_flight Calls handled by the Stasis app",
        "# TYPE masking_calls_in_flight gauge",
        f"masking_calls_in_flight {active_calls}",
        "# HELP masking_ari_ws_connected ARI websocket connection state",
        "# TYPE masking_ari_ws_connected gauge",
        f"masking_ari_ws_connected {ws_connected}",
    ]
    return fastapi.Response(
        "\n".join(lines) + "\n", media_type=_PROMETHEUS_CONTENT_TYPE
    )
