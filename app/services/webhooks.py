"""Outbound webhooks with HMAC-SHA256 signatures and exponential retry.

Events: ``call.started``, ``call.answered``, ``call.ended``,
``session.expired`` and ``session.updated``. Delivery is fire-and-forget from
the caller's point of view, because a slow customer endpoint must never delay a
call. The payload carries masked numbers only.
"""

from __future__ import annotations

import asyncio
import datetime
import json
from typing import Any

import httpx

from app.core import config, crypto, logging_config

log = logging_config.get_logger(__name__)

_INITIAL_RETRY_DELAY_SECONDS = 1.0
_MAX_RETRY_DELAY_SECONDS = 60.0
_HTTP_ERROR_THRESHOLD = 400

_tasks: set[asyncio.Task] = set()


def emit(
    event: str, payload: dict[str, Any], *, settings: config.Settings
) -> None:
    """Schedules a webhook delivery.

    Does nothing when no webhook URL and secret are configured.

    Args:
        event: Event name, for example ``call.ended``.
        payload: JSON-encodable body, already free of personal data.
        settings: Application settings holding the endpoint and the secret.
    """
    if not settings.webhooks_enabled:
        return
    task = asyncio.create_task(_deliver(event, payload, settings))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


async def drain(max_wait: float = 5.0) -> None:
    """Awaits in-flight deliveries during shutdown, up to ``max_wait``."""
    if not _tasks:
        return
    await asyncio.wait(set(_tasks), timeout=max_wait)


async def _deliver(
    event: str, payload: dict[str, Any], settings: config.Settings
) -> None:
    """Posts one event, retrying with exponential backoff."""
    body = json.dumps(
        {
            "event": event,
            "sent_at": datetime.datetime.now(datetime.UTC).isoformat(),
            "trace_id": logging_config.get_trace_id(),
            "data": payload,
        },
        separators=(",", ":"),
    ).encode()
    headers = {
        "Content-Type": "application/json",
        "X-Masking-Event": event,
        "X-Masking-Signature": crypto.webhook_signature(
            body, settings.webhook_secret
        ),
    }

    delay = _INITIAL_RETRY_DELAY_SECONDS
    async with httpx.AsyncClient(
        timeout=settings.webhook_timeout_seconds
    ) as client:
        for attempt in range(1, settings.webhook_max_attempts + 1):
            try:
                response = await client.post(
                    settings.webhook_url, content=body, headers=headers
                )
                if response.status_code < _HTTP_ERROR_THRESHOLD:
                    log.info(
                        "webhook.delivered",
                        webhook_event=event,
                        attempt=attempt,
                    )
                    return
                log.warning(
                    "webhook.rejected",
                    webhook_event=event,
                    attempt=attempt,
                    status_code=response.status_code,
                )
            except Exception as exc:
                log.warning(
                    "webhook.failed",
                    webhook_event=event,
                    attempt=attempt,
                    error=str(exc),
                )

            if attempt == settings.webhook_max_attempts:
                break
            await asyncio.sleep(delay)
            delay = min(delay * 2, _MAX_RETRY_DELAY_SECONDS)

    log.error(
        "webhook.giving_up",
        webhook_event=event,
        attempts=settings.webhook_max_attempts,
    )
