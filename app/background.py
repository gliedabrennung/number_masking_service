"""Periodic maintenance: session expiry and retention purge."""

from __future__ import annotations

import asyncio

from app.core import config, logging_config
from app.db import engine
from app.services import sessions as sessions_service
from app.services import webhooks

log = logging_config.get_logger(__name__)


async def expiry_loop(settings: config.Settings) -> None:
    """Flips sessions to ``expired`` shortly after their TTL elapses.

    Routing already refuses a call once the expiry has passed, so this loop is
    about releasing numbers and emitting events, not about correctness. Runs
    until cancelled.

    Args:
        settings: Application settings holding the scan interval.
    """
    while True:
        try:
            async with engine.session_scope() as db:
                expired = await sessions_service.expire_due_sessions(db)
                events = [
                    {
                        "session_id": str(session.id),
                        "proxy_number": session.number.e164,
                    }
                    for session in expired
                ]
            for payload in events:
                webhooks.emit("session.expired", payload, settings=settings)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.error("expiry_loop.failed", error=str(exc))
        await asyncio.sleep(settings.expiry_scan_interval_seconds)


async def retention_loop(settings: config.Settings) -> None:
    """Deletes call rows and closed sessions past their retention window.

    Runs until cancelled.

    Args:
        settings: Application settings holding the cleanup interval.
    """
    while True:
        try:
            async with engine.session_scope() as db:
                await sessions_service.purge_old_records(db, settings=settings)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.error("retention_loop.failed", error=str(exc))
        await asyncio.sleep(settings.cleanup_interval_seconds)
