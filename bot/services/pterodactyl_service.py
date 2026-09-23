"""
Pterodactyl / PebbleHost panel integration.

Pterodactyl is the open-source control panel used by most managed
Minecraft hosts (PebbleHost, BisectHosting, Apex, Shockbyte, etc.).
Its Client API exposes a WebSocket that streams the server's live
console output in real time, which is exactly what we need for the
chat/console bridge when the bot does *not* have filesystem access
to the Minecraft server.

What this module provides
-------------------------
* :class:`PterodactylClient` — a thin REST client. Currently used
  internally to fetch WebSocket credentials and (optionally) to send
  commands without RCON, but it's a public class so cogs can extend
  the integration (power actions, file operations, etc.) later.
* :class:`PterodactylLogSource` — a :class:`~bot.services.log_service.
  LogSource` implementation that connects to the WebSocket and yields
  every ``console output`` event as a log line.

WebSocket protocol overview
---------------------------
1. ``GET /api/client/servers/<id>/websocket`` returns
   ``{"data": {"token": "...", "socket": "wss://..."}}``.
2. Connect to ``socket`` and send ``{"event": "auth",
   "args": ["<token>"]}``.
3. Receive frames. The important ones:

   * ``auth success``       — authenticated, safe to consume output.
   * ``console output``     — ``args[0]`` is one raw line of output.
   * ``token expiring``     — re-auth within ~60s with a fresh token.
   * ``token expired``      — panel will drop us; reconnect.
   * ``jwt error``          — auth rejected; usually bad credentials.
     Raises :class:`PterodactylAuthError` and disables log mirroring
     until the bot is restarted or credentials are fixed.

4. Tokens are short-lived (~10–15 min). We proactively refresh on
   ``token expiring`` so we never actually have to reconnect mid-stream
   due to auth expiry.

Fatal auth failures (:class:`PterodactylAuthError`) stop the tail loop
instead of retrying forever. Transient network or panel errors still
reconnect with exponential backoff.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, AsyncIterator, Optional

import aiohttp

from bot.services.log_service import LogSource

log = logging.getLogger(__name__)

# How long an operator has to wait before reconnect attempts escalate.
# The sequence is [2s, 4s, 8s, 16s, 30s, 30s, ...] — gentle enough to
# ride out a brief panel outage, aggressive enough to come back fast
# once the panel is healthy again.
_INITIAL_BACKOFF = 2.0
_MAX_BACKOFF = 30.0

# When we reconnect, Pterodactyl replays recent console output so the
# panel UI can populate. We don't want to spam Discord with "Bob left"
# lines from five minutes ago, so we drop anything received in this
# window after (re)authentication.
_REPLAY_GRACE = 2.0

# Per-message timeout for the WebSocket read loop. Pterodactyl sends
# ``stats`` events ~every 2s, so no-activity longer than this almost
# certainly means the connection is dead — reconnect.
_WS_IDLE_TIMEOUT = 60.0


class PterodactylError(RuntimeError):
    """Raised when the Pterodactyl API returns an error we can't recover from."""


class PterodactylAuthError(PterodactylError):
    """Fatal auth failure — bad API key or rejected JWT; do not retry."""


class PterodactylClient:
    """Minimal Client API wrapper.

    Not a full SDK — only the endpoints the bot actually uses. Kept
    deliberately small so it's easy to audit and to reason about
    failure modes.

    Thread-safe? The class holds no shared mutable state beyond its
    constructor arguments; every call creates its own
    :class:`aiohttp.ClientSession`, which sidesteps the cross-event-loop
    issues you'd hit by caching a session across tasks. That costs a
    small per-request overhead (TLS handshake on the first call) which
    is acceptable for the handful of requests we make.
    """

    def __init__(self, panel_url: str, server_id: str, api_key: str):
        # Strip a trailing slash once so every endpoint-composing call
        # site can unconditionally prepend ``/api/...``. Exposed as a
        # public attribute because the WebSocket client needs it to
        # build the ``Origin`` header Wings validates against.
        self.panel_url = panel_url.rstrip("/")
        self._server_id = server_id
        self._api_key = api_key

    @property
    def _headers(self) -> dict:
        # Pterodactyl's Client API always wants these three headers.
        return {
            "Authorization": f"Bearer {self._api_key}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

    async def fetch_websocket_credentials(self) -> tuple[str, str]:
        """Return ``(token, socket_url)`` for a live-console WebSocket.

        Both values are short-lived. Tokens typically expire after
        ~10 minutes; the socket URL itself is stable per panel but
        we still ask for it each time so a panel migration doesn't
        leave us stuck pointing at the old host.
        """
        url = f"{self.panel_url}/api/client/servers/{self._server_id}/websocket"
        async with aiohttp.ClientSession(headers=self._headers) as session:
            async with session.get(url) as resp:
                if resp.status != 200:
                    if resp.status in (401, 403):
                        raise PterodactylAuthError(
                            f"panel request returned HTTP {resp.status}"
                        )
                    raise PterodactylError(
                        f"panel request returned HTTP {resp.status}"
                    )
                payload = await resp.json()
        try:
            data = payload["data"]
            return data["token"], data["socket"]
        except (KeyError, TypeError):
            raise PterodactylError("malformed websocket credentials response") from None

    async def send_command(self, command: str) -> None:
        """Send a single command to the server's console.

        Returns once the panel accepts the POST (HTTP 204). This is
        fire-and-forget: the panel doesn't relay a per-command
        response back over HTTP — command output appears on the
        WebSocket like any other console line, so treat this like
        ``print`` and observe the log stream to see the result.

        Not currently wired to any cog — the chat bridge and
        :class:`bot.services.rcon_service.RconService` both prefer
        RCON's request/response model. This exists so a future
        "no-RCON" setup can use the panel as the sole egress path.
        """
        url = f"{self.panel_url}/api/client/servers/{self._server_id}/command"
        body = {"command": command}
        async with aiohttp.ClientSession(headers=self._headers) as session:
            async with session.post(url, json=body) as resp:
                # 204 is the documented success code; 502 happens when
                # the server is offline (Pterodactyl can't forward the
                # command). Both surface as PterodactylError so the
                # caller can decide whether to retry.
                if resp.status not in (200, 204):
                    raise PterodactylError(
                        f"panel request returned HTTP {resp.status}"
                    )


class PterodactylLogSource(LogSource):
    """Stream live console output via Pterodactyl's WebSocket.

    Drop-in replacement for :class:`LocalLogTailer`. Designed to be
    resilient: if the WebSocket drops, it reconnects with exponential
    backoff; if the auth token expires it refreshes and re-authenticates
    in place without reconnecting.
    """

    def __init__(self, panel_url: str, server_id: str, api_key: str):
        self._client = PterodactylClient(panel_url, server_id, api_key)
        self._stop = asyncio.Event()

    def stop(self) -> None:
        """Signal the tailer to exit after the current iteration."""
        self._stop.set()

    async def tail(self) -> AsyncIterator[str]:
        """Yield console lines forever, reconnecting as needed.

        The outer loop owns retry/backoff state; the inner
        :py:meth:`_stream_once` handles a single WebSocket session
        and raises when it dies (either from a protocol error, the
        server restarting, or our stop signal).
        """
        backoff = _INITIAL_BACKOFF
        while not self._stop.is_set():
            try:
                async for line in self._stream_once():
                    yield line
                    # Any successful yield means the session is
                    # healthy; reset the backoff so the next
                    # failure starts fresh.
                    backoff = _INITIAL_BACKOFF
            except asyncio.CancelledError:
                raise
            except PterodactylAuthError as e:
                log.error(
                    "Pterodactyl authentication failed (%s). "
                    "Log mirroring is disabled until the bot is restarted "
                    "or credentials are fixed.",
                    e,
                )
                break
            except Exception as e:
                log.error(
                    "Pterodactyl log stream failed (%s); reconnecting in %.1fs",
                    type(e).__name__,
                    backoff,
                )
            if self._stop.is_set():
                break
            await self._sleep_or_stop(backoff)
            backoff = min(backoff * 2, _MAX_BACKOFF)

    async def _stream_once(self) -> AsyncIterator[str]:
        """Run a single WebSocket session, yielding console lines.

        Returns normally when the stop signal fires or the server
        cleanly closes the socket; re-raises any connection/protocol
        error for the outer retry loop to handle.
        """
        token, socket_url = await self._client.fetch_websocket_credentials()

        # Wings (the Pterodactyl daemon that terminates the
        # WebSocket) validates the Origin header against the panel's
        # configured URL and returns HTTP 403 on a mismatch. Browser
        # clients get this for free; server-to-server clients like
        # this one must set it explicitly.
        ws_headers = {"Origin": self._client.panel_url}

        timeout = aiohttp.ClientTimeout(total=None, sock_read=_WS_IDLE_TIMEOUT)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.ws_connect(
                socket_url, headers=ws_headers, heartbeat=30.0
            ) as ws:
                await self._send_auth(ws, token)
                # Established-at timestamp used to skip replayed
                # backlog. Set after successful auth rather than at
                # connect time so we don't accidentally drop live
                # lines that arrive during the auth round trip.
                authed_at: Optional[float] = None

                while not self._stop.is_set():
                    msg = await ws.receive()

                    if msg.type is aiohttp.WSMsgType.TEXT:
                        event, args = self._parse_frame(msg.data)
                        if event is None:
                            continue

                        if event == "auth success":
                            # Mark the grace window; subsequent
                            # replayed lines within _REPLAY_GRACE
                            # seconds will be dropped.
                            authed_at = time.monotonic()
                            log.info("Pterodactyl WebSocket authenticated.")
                            continue

                        if event == "console output":
                            if not args:
                                continue
                            if (
                                authed_at is not None
                                and time.monotonic() - authed_at < _REPLAY_GRACE
                            ):
                                # Drop the post-connect replay burst
                                # so we don't re-post yesterday's
                                # chat messages to Discord.
                                continue
                            yield args[0]
                            continue

                        if event == "token expiring":
                            # Refresh the JWT in place; the panel
                            # keeps the WebSocket open as long as
                            # we re-auth promptly.
                            try:
                                new_token, _ = (
                                    await self._client.fetch_websocket_credentials()
                                )
                                await self._send_auth(ws, new_token)
                            except PterodactylAuthError:
                                raise
                            except PterodactylError as e:
                                log.warning(
                                    "Failed to refresh Pterodactyl token: %s",
                                    type(e).__name__,
                                )
                                # Bail; the outer loop will reconnect.
                                return
                            continue

                        if event == "token expired":
                            # Panel will close the socket next.
                            # Return cleanly so the outer loop
                            # re-handshakes from scratch.
                            log.info("Pterodactyl token expired; reconnecting.")
                            return

                        if event == "jwt error":
                            # Fatal: auth was rejected. Almost
                            # always a bad API key. Don't spin in a
                            # tight reconnect loop for this.
                            raise PterodactylAuthError(
                                "Pterodactyl auth rejected"
                            )
                        # Unknown/uninteresting events (status,
                        # stats, install output, etc.) are ignored.

                    elif msg.type in (
                        aiohttp.WSMsgType.CLOSED,
                        aiohttp.WSMsgType.CLOSE,
                        aiohttp.WSMsgType.CLOSING,
                    ):
                        log.info("Pterodactyl WebSocket closed by server.")
                        return

                    elif msg.type is aiohttp.WSMsgType.ERROR:
                        raise PterodactylError("WebSocket error")

    @staticmethod
    async def _send_auth(ws: aiohttp.ClientWebSocketResponse, token: str) -> None:
        """Send the JSON auth frame the panel expects after connect.

        Extracted so the initial auth and the token-refresh path share
        exactly one implementation.
        """
        await ws.send_str(json.dumps({"event": "auth", "args": [token]}))

    @staticmethod
    def _parse_frame(raw: str) -> tuple[Optional[str], list[Any]]:
        """Decode a single text frame into ``(event, args)``.

        Returns ``(None, [])`` for malformed frames so the caller can
        skip them without raising — we'd rather drop one weird
        message than kill the whole session.
        """
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            return None, []
        event = payload.get("event")
        args = payload.get("args") or []
        if not isinstance(args, list):
            args = [args]
        return event, args

    async def _sleep_or_stop(self, seconds: float) -> None:
        """Sleep for ``seconds`` or wake early if :py:meth:`stop` was called."""
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass
