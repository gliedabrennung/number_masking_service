"""Thin async ARI client: REST over httpx, events over websockets.

Deliberately hand-written and small. The published Python ARI wrappers
(``ari-py``, ``aioari``) are unmaintained and pull in a Swagger stack, while the
surface this service needs is a dozen endpoints.

Nothing in here knows about sessions or masking: it is a transport.

Typical usage example:

    ari = client.ARIClient(settings)
    await ari.answer(channel_id)
    async for event in ari.events():
        ...
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator
from typing import Any

import httpx
import websockets

from app.core import config, logging_config

log = logging_config.get_logger(__name__)

_HTTP_ERROR_THRESHOLD = 400
_HTTP_NO_CONTENT = 204
_HTTP_NOT_FOUND = 404
_REQUEST_TIMEOUT_SECONDS = 10.0
_CONNECT_TIMEOUT_SECONDS = 5.0
_WS_PING_SECONDS = 20
_ERROR_BODY_LIMIT = 500


class ARIError(RuntimeError):
    """An ARI request failed.

    Attributes:
        status_code: HTTP status returned by Asterisk, or 0 for a transport
            failure.
    """

    def __init__(self, status_code: int, message: str) -> None:
        """Initializes the error from an HTTP status and a body excerpt."""
        super().__init__(f"ARI {status_code}: {message}")
        self.status_code = status_code


class ARIClient:
    """REST and websocket client for one Asterisk instance.

    Attributes:
        connected: Whether the event websocket is currently established.
    """

    def __init__(self, settings: config.Settings) -> None:
        """Initializes the client without opening any connection yet.

        Args:
            settings: Application settings holding the ARI URL and credentials.
        """
        self._settings = settings
        self._base = settings.ari_url.rstrip("/") + "/ari"
        self._client = httpx.AsyncClient(
            auth=(settings.ari_user, settings.ari_password),
            timeout=httpx.Timeout(
                _REQUEST_TIMEOUT_SECONDS, connect=_CONNECT_TIMEOUT_SECONDS
            ),
            base_url=self._base,
        )
        self.connected = False
        self._socket: Any = None

    async def aclose(self) -> None:
        """Closes the underlying HTTP connection pool."""
        await self._client.aclose()

    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        """Performs one ARI request.

        Args:
            method: HTTP method.
            path: Path below ``/ari``.
            **kwargs: Passed through to httpx.

        Returns:
            The decoded JSON body, or None when the response carries no body.

        Raises:
            ARIError: Asterisk answered with an error status, or the request
                could not be delivered at all.
        """
        try:
            response = await self._client.request(method, path, **kwargs)
        except httpx.HTTPError as http_error:
            raise ARIError(0, str(http_error)) from http_error
        if response.status_code >= _HTTP_ERROR_THRESHOLD:
            raise ARIError(
                response.status_code, response.text[:_ERROR_BODY_LIMIT]
            )
        if response.status_code == _HTTP_NO_CONTENT or not response.content:
            return None
        try:
            return response.json()
        except json.JSONDecodeError:
            return None

    async def answer(self, channel_id: str) -> None:
        """Answers the channel, so audio can flow."""
        await self._request("POST", f"/channels/{channel_id}/answer")

    async def ring(self, channel_id: str) -> None:
        """Sends 180 Ringing towards the caller (local ringback tone)."""
        await self._request("POST", f"/channels/{channel_id}/ring")

    async def ring_stop(self, channel_id: str) -> None:
        """Stops the ringing indication started by :meth:`ring`."""
        await self._request("DELETE", f"/channels/{channel_id}/ring")

    async def hangup(
        self, channel_id: str, *, reason_code: int | None = None
    ) -> None:
        """Hangs the channel up.

        Args:
            channel_id: Channel to release.
            reason_code: Q.850 cause to signal, for example 21 for call
                rejected.
        """
        params = (
            {"reason_code": str(reason_code)}
            if reason_code is not None
            else None
        )
        await self._request("DELETE", f"/channels/{channel_id}", params=params)

    async def play(
        self, channel_id: str, media: str, *, lang: str = "ru"
    ) -> str | None:
        """Plays a sound file to the channel.

        Args:
            channel_id: Channel to play to.
            media: Sound name, for example ``custom/session-expired``.
            lang: Language subdirectory of the sounds tree.

        Returns:
            The playback identifier, or None when Asterisk reported none.
        """
        result = await self._request(
            "POST",
            f"/channels/{channel_id}/play",
            params={"media": f"sound:{media}", "lang": lang},
        )
        return result.get("id") if isinstance(result, dict) else None

    async def stop_playback(self, playback_id: str) -> None:
        """Stops a playback started by :meth:`play`."""
        await self._request("DELETE", f"/playbacks/{playback_id}")

    async def get_channel(self, channel_id: str) -> dict | None:
        """Returns the channel state, or None when it no longer exists."""
        try:
            return await self._request("GET", f"/channels/{channel_id}")
        except ARIError as ari_error:
            if ari_error.status_code == _HTTP_NOT_FOUND:
                return None
            raise

    async def set_variable(
        self, channel_id: str, name: str, value: str
    ) -> None:
        """Sets a channel variable."""
        await self._request(
            "POST",
            f"/channels/{channel_id}/variable",
            params={"variable": name, "value": value},
        )

    async def subscribe_channel(self, app: str, channel_id: str) -> None:
        """Subscribes the application to one channel explicitly.

        Without this, ``subscribeAll=false`` stops delivering events for a
        channel the moment it leaves Stasis — including ``ChannelDestroyed``,
        the only event that carries the Q.850 hangup cause.

        Args:
            app: Stasis application name.
            channel_id: Channel to keep receiving events for.
        """
        await self._request(
            "POST",
            f"/applications/{app}/subscription",
            params={"eventSource": f"channel:{channel_id}"},
        )

    async def originate(
        self,
        *,
        endpoint: str,
        app: str,
        app_args: str,
        caller_id: str,
        timeout: int,  # noqa: ASYNC109
        originator: str | None = None,
        channel_id: str | None = None,
        variables: dict[str, str] | None = None,
    ) -> dict:
        """Creates the outbound leg of a call.

        Args:
            endpoint: PJSIP endpoint to dial.
            app: Stasis application the new channel enters on answer.
            app_args: Comma-separated arguments passed to that application.
            caller_id: Caller identity presented to the callee. Must always be
                the proxy number, never the real number of the other party.
            timeout: Seconds to wait for an answer before giving up.
            originator: Inbound channel to inherit codecs and linkedid from,
                which keeps the CDR correlated.
            channel_id: Identifier to assign to the new channel.
            variables: Channel variables to set before dialing.

        Returns:
            The created channel as reported by Asterisk.

        Raises:
            ARIError: Asterisk refused to create the channel.
        """
        body: dict[str, Any] = {
            "endpoint": endpoint,
            "app": app,
            "appArgs": app_args,
            "callerId": caller_id,
            "timeout": timeout,
        }
        if originator:
            body["originator"] = originator
        if channel_id:
            body["channelId"] = channel_id
        if variables:
            body["variables"] = variables
        return await self._request("POST", "/channels", json=body)

    async def create_bridge(
        self, bridge_id: str, *, name: str | None = None
    ) -> dict:
        """Creates a mixing bridge with the given identifier."""
        return await self._request(
            "POST",
            "/bridges",
            params={
                "type": "mixing",
                "bridgeId": bridge_id,
                "name": name or bridge_id,
            },
        )

    async def add_to_bridge(self, bridge_id: str, *channel_ids: str) -> None:
        """Adds one or more channels to a bridge."""
        await self._request(
            "POST",
            f"/bridges/{bridge_id}/addChannel",
            params={"channel": ",".join(channel_ids)},
        )

    async def destroy_bridge(self, bridge_id: str) -> None:
        """Destroys a bridge, ignoring one that is already gone."""
        try:
            await self._request("DELETE", f"/bridges/{bridge_id}")
        except ARIError as ari_error:
            if ari_error.status_code != _HTTP_NOT_FOUND:
                raise

    async def ping(self) -> bool:
        """Returns True when the ARI REST interface answers."""
        try:
            await self._request("GET", "/asterisk/info")
        except ARIError:
            return False
        return True

    async def application_registered(self, app: str) -> bool:
        """Returns True while Asterisk still knows the Stasis application.

        Asterisk tears an application down when the websocket that registered
        it goes away — including the case where a second, short-lived consumer
        took it over and then left. The socket of the original consumer stays
        open and silent, so this is the only way to notice.

        Raises:
            ARIError: The interface could not be reached at all.
        """
        try:
            await self._request("GET", f"/applications/{app}")
        except ARIError as ari_error:
            if ari_error.status_code == _HTTP_NOT_FOUND:
                return False
            raise
        return True

    async def force_reconnect(self) -> None:
        """Drops the event websocket so :meth:`events` opens a fresh one."""
        socket = self._socket
        self._socket = None
        if socket is not None:
            with contextlib.suppress(Exception):
                await socket.close()

    async def events(self) -> AsyncIterator[dict]:
        """Yields Stasis events, reconnecting with exponential backoff.

        The iterator never ends on its own: a dropped websocket is retried
        forever, because the service is useless without the event stream.

        Yields:
            One decoded ARI event per websocket frame.
        """
        settings = self._settings
        url = (
            f"{settings.ari_ws_url}"
            f"?api_key={settings.ari_user}:{settings.ari_password}"
            f"&app={settings.ari_app}&subscribeAll=false"
        )
        delay = settings.ari_reconnect_min_seconds
        while True:
            try:
                async with websockets.connect(
                    url,
                    ping_interval=_WS_PING_SECONDS,
                    ping_timeout=_WS_PING_SECONDS,
                ) as socket:
                    self._socket = socket
                    self.connected = True
                    delay = settings.ari_reconnect_min_seconds
                    log.info("ari.ws_connected", app=settings.ari_app)
                    async for raw in socket:
                        try:
                            yield json.loads(raw)
                        except json.JSONDecodeError:
                            log.warning("ari.ws_bad_frame")
            except asyncio.CancelledError:
                self.connected = False
                self._socket = None
                raise
            except Exception as exc:
                self.connected = False
                self._socket = None
                log.warning(
                    "ari.ws_disconnected", error=str(exc), retry_in=delay
                )
                await asyncio.sleep(delay)
                delay = min(delay * 2, settings.ari_reconnect_max_seconds)
            else:
                self.connected = False
                self._socket = None
                log.warning("ari.ws_closed", retry_in=delay)
                await asyncio.sleep(delay)
