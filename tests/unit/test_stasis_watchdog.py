from __future__ import annotations

import asyncio
import base64
import os

import pytest

from app.ari import client as ari_client
from app.ari import stasis
from app.core import config


class _StubClient:
    def __init__(self, registered: bool | Exception) -> None:
        self.connected = True
        self.registered = registered
        self.checks = 0
        self.reconnects = 0

    async def application_registered(self, app: str) -> bool:
        del app
        self.checks += 1
        if isinstance(self.registered, Exception):
            raise self.registered
        return self.registered

    async def force_reconnect(self) -> None:
        self.reconnects += 1

    async def aclose(self) -> None:
        return None


def _app(client: _StubClient) -> stasis.MaskingStasisApp:
    settings = config.Settings(
        _env_file=None,
        ari_app_check_seconds=0.01,
        party_hash_secret="x" * 40,
        encryption_key=base64.b64encode(os.urandom(32)).decode(),
    )
    return stasis.MaskingStasisApp(settings, client=client)


async def _run_watchdog(app: stasis.MaskingStasisApp, seconds: float) -> None:
    task = asyncio.create_task(app._registration_watchdog())
    await asyncio.sleep(seconds)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_reconnects_when_the_application_is_gone() -> None:
    client = _StubClient(registered=False)
    await _run_watchdog(_app(client), 0.08)
    assert client.checks >= 1
    assert client.reconnects >= 1


async def test_stays_quiet_while_the_application_is_registered() -> None:
    client = _StubClient(registered=True)
    await _run_watchdog(_app(client), 0.08)
    assert client.checks >= 1
    assert client.reconnects == 0


async def test_an_unreachable_interface_is_not_treated_as_a_loss() -> None:
    client = _StubClient(registered=ari_client.ARIError(0, "connection lost"))
    await _run_watchdog(_app(client), 0.08)
    assert client.checks >= 1
    assert client.reconnects == 0


async def test_a_disconnected_socket_is_not_polled() -> None:
    client = _StubClient(registered=False)
    client.connected = False
    await _run_watchdog(_app(client), 0.08)
    assert client.checks == 0
    assert client.reconnects == 0


async def test_application_replaced_is_reported() -> None:
    client = _StubClient(registered=True)
    app = _app(client)
    await app.handle_event({"type": "ApplicationReplaced", "application": "x"})
