from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import os

import httpx
import pytest
import respx

from app.core import config, logging_config
from app.services import webhooks

_URL = "https://customer.test/hook"
_SECRET = "webhook-secret"


def _settings(**kwargs: object) -> config.Settings:
    base = {
        "webhook_url": _URL,
        "webhook_secret": _SECRET,
        "webhook_max_attempts": 3,
        "webhook_timeout_seconds": 1.0,
        "party_hash_secret": "x" * 40,
        "encryption_key": base64.b64encode(os.urandom(32)).decode(),
    }
    base.update(kwargs)
    return config.Settings(_env_file=None, **base)


@pytest.fixture(autouse=True)
def _no_backoff_sleep(monkeypatch):
    """Runs the retry backoff instantly instead of waiting seconds."""
    real_sleep = asyncio.sleep

    async def instant(delay: float) -> None:
        del delay
        await real_sleep(0)

    monkeypatch.setattr(webhooks.asyncio, "sleep", instant)


async def test_disabled_when_url_or_secret_is_missing() -> None:
    with respx.mock:
        route = respx.post(_URL).mock(return_value=httpx.Response(200))
        webhooks.emit("call.ended", {}, settings=_settings(webhook_url=""))
        webhooks.emit("call.ended", {}, settings=_settings(webhook_secret=""))
        await webhooks.drain()
    assert not route.called


async def test_delivery_is_signed_over_the_exact_body() -> None:
    with respx.mock:
        route = respx.post(_URL).mock(return_value=httpx.Response(200))
        logging_config.bind_trace_id("trace-42")
        webhooks.emit(
            "call.ended",
            {"call_id": 7, "status": "answered"},
            settings=_settings(),
        )
        await webhooks.drain()

    assert route.call_count == 1
    request = route.calls[0].request
    body = request.content
    expected = hmac.new(_SECRET.encode(), body, hashlib.sha256).hexdigest()
    assert request.headers["X-Masking-Signature"] == f"sha256={expected}"
    assert request.headers["X-Masking-Event"] == "call.ended"
    assert request.headers["Content-Type"] == "application/json"

    payload = json.loads(body)
    assert payload["event"] == "call.ended"
    assert payload["trace_id"] == "trace-42"
    assert payload["data"] == {"call_id": 7, "status": "answered"}
    assert payload["sent_at"]


async def test_a_wrong_secret_does_not_validate() -> None:
    with respx.mock:
        route = respx.post(_URL).mock(return_value=httpx.Response(200))
        webhooks.emit("call.started", {}, settings=_settings())
        await webhooks.drain()

    request = route.calls[0].request
    forged = hmac.new(
        b"another-secret", request.content, hashlib.sha256
    ).hexdigest()
    assert request.headers["X-Masking-Signature"] != f"sha256={forged}"


async def test_retries_until_the_endpoint_accepts() -> None:
    responses = [
        httpx.Response(500),
        httpx.Response(502),
        httpx.Response(200),
    ]
    with respx.mock:
        route = respx.post(_URL).mock(side_effect=responses)
        webhooks.emit("call.answered", {}, settings=_settings())
        await webhooks.drain()

    assert route.call_count == 3


async def test_gives_up_after_the_configured_attempts() -> None:
    with respx.mock:
        route = respx.post(_URL).mock(return_value=httpx.Response(503))
        webhooks.emit(
            "session.expired", {}, settings=_settings(webhook_max_attempts=2)
        )
        await webhooks.drain()

    assert route.call_count == 2


async def test_a_transport_failure_is_retried_too() -> None:
    with respx.mock:
        route = respx.post(_URL).mock(
            side_effect=[httpx.ConnectError("down"), httpx.Response(200)]
        )
        webhooks.emit("call.started", {}, settings=_settings())
        await webhooks.drain()

    assert route.call_count == 2


async def test_a_slow_endpoint_never_blocks_the_caller() -> None:
    async def slow(request: httpx.Request) -> httpx.Response:
        del request
        await asyncio.sleep(0.2)
        return httpx.Response(200)

    with respx.mock:
        route = respx.post(_URL).mock(side_effect=slow)
        webhooks.emit("call.ended", {}, settings=_settings())
        assert not route.called
        await webhooks.drain()

    assert route.call_count == 1
