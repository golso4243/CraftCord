"""
Native asyncio implementation of the Source/Minecraft RCON protocol.

An earlier version of this module wrapped the blocking ``mcrcon`` package
via :func:`asyncio.to_thread`. That library installs a ``SIGINT`` handler
inside its constructor, and Python only allows ``signal.signal`` to be
called from the main thread — so any command that happened to land on a
worker thread would fail with
``ValueError: signal only works in main thread of the main interpreter``.

The RCON wire protocol is small enough (packet = 3× little-endian int32
header + null-terminated body + trailing null) that rolling our own is
cheaper than papering over a library bug, and an async-native client lets
us:

* Serialise requests with a plain ``asyncio.Lock`` (no thread pool).
* Cancel cleanly via ``asyncio.wait_for`` timeouts instead of signals.
* Reuse a single long-lived TCP connection without any thread hops.

Public surface (:class:`RconError`, :class:`RconService`,
``RconService.command``, ``RconService.close``) is unchanged so the rest
of the bot doesn't need to know about the swap.
"""
from __future__ import annotations

import asyncio
import logging
import struct
from typing import Optional, Tuple

log = logging.getLogger(__name__)


class RconError(RuntimeError):
    """Raised when an RCON command cannot be executed.

    Wraps every failure mode (connect, auth, command, read) in a single
    exception type so cogs can ``except RconError`` without caring which
    step actually blew up.
    """


class _DeliveryUncertain(Exception):
    """Internal: the command packet may already have reached Minecraft.

    Raised after the command body has been handed to the socket writer.
    Callers must not automatically replay mutating commands or tellraw
    merely because the response was lost — that would risk double
    execution. This type is never exposed to cogs.
    """


# Source RCON packet types. Minecraft follows the Valve specification.
_TYPE_LOGIN = 3
_TYPE_COMMAND = 2
_TYPE_RESPONSE = 0  # used by the server for both auth ACK and command output

# Request id assigned to the "trailing empty packet" we use as an
# end-of-response marker. Any positive signed 32-bit int that we don't
# otherwise use will do; the value itself is only a cookie.
_TAIL_ID = 0x7F7F7F7F

# Maximum RCON request body this client will send. Longer bodies risk
# silent truncation on the wire, which would corrupt structured
# ``tellraw`` SNBT — so we refuse them early. Kept in sync with
# :data:`bot.utils.text_component.RCON_MAX_BODY_BYTES`.
_MAX_BODY_BYTES = 1446


class RconService:
    """Async-native, serialised RCON client with careful reconnect.

    A single long-lived TCP connection is kept open. Commands are issued
    one at a time (guarded by ``self._lock``) because Minecraft's RCON
    server multiplexes request ids onto a single stream of response
    packets — concurrent callers on the same socket would interleave
    each other's replies.

    Retry policy (not exactly-once delivery):

    * Failure **before** the command packet is written (connect / auth):
      drop the socket and try the full send path once more. The Minecraft
      command body never left this process.
    * Failure **at or after** the command write (drain / read / lost
      response): delivery is uncertain. The socket is closed so a later
      request can reconnect. The same body is **not** resent unless the
      caller passed ``replay_if_uncertain=True`` (reserved for read-only
      probes such as ``list``). Mutating commands and ``tellraw`` must
      not opt in.
    """

    def __init__(self, host: str, port: int, password: str, timeout: float = 5.0):
        self._host = host
        self._port = port
        self._password = password
        self._timeout = timeout
        self._reader: Optional[asyncio.StreamReader] = None
        self._writer: Optional[asyncio.StreamWriter] = None
        # Monotonically increasing id for real commands. We avoid 0 /
        # negative ids (reserved by the protocol) and ``_TAIL_ID``.
        self._next_id = 1
        self._lock = asyncio.Lock()

    # ── public API ───────────────────────────────────────────────
    async def command(
        self, command: str, *, replay_if_uncertain: bool = False
    ) -> str:
        """Run an RCON command and return its textual response.

        Args:
            command: The RCON command to execute, without a leading slash.
            replay_if_uncertain: When True, allow one extra send after a
                lossy mid-flight failure. Only safe for idempotent
                read-only probes such as ``list``. Defaults to False so
                mutating commands and ``tellraw`` are never silently
                replayed when Minecraft may already have executed them.

        Raises:
            RconError: If the command is empty, too long for the
                protocol, or the underlying socket fails after any
                permitted retry.
        """
        command = command.strip()
        if not command:
            raise RconError("Empty command")
        if len(command.encode("utf-8")) > _MAX_BODY_BYTES:
            # Catching this here (instead of letting the server
            # truncate) keeps tellraw payloads coherent — a chopped
            # SNBT string would break the whole command.
            raise RconError("RCON command body exceeds protocol limit")

        # Log the first token only so chat/tellraw bodies never hit stdout.
        kind = command.split(None, 1)[0]
        async with self._lock:
            try:
                return await self._send_command(command)
            except _DeliveryUncertain as first:
                # Command bytes may already be on the wire. Reconnect for
                # future requests; only replay when the caller opted in.
                cause_name = type(first.__cause__ or first).__name__
                log.info(
                    "RCON delivery uncertain, reconnecting: kind=%s (%s)",
                    kind,
                    cause_name,
                )
                await self._close()
                if not replay_if_uncertain:
                    raise RconError("RCON command failed") from first
                try:
                    return await self._send_command(command)
                except Exception as second:
                    log.warning(
                        "RCON command failed: kind=%s (%s)",
                        kind,
                        type(second).__name__,
                    )
                    await self._close()
                    raise RconError("RCON command failed") from second
            except Exception as first:
                # Connect/auth (or other pre-write) failure: safe to
                # retry once because the command body was never sent.
                log.info(
                    "RCON command failed before send, reconnecting: "
                    "kind=%s (%s)",
                    kind,
                    type(first).__name__,
                )
                await self._close()
                try:
                    return await self._send_command(command)
                except Exception as second:
                    log.warning(
                        "RCON command failed: kind=%s (%s)",
                        kind,
                        type(second).__name__,
                    )
                    await self._close()
                    raise RconError("RCON command failed") from second

    async def close(self) -> None:
        """Close the underlying connection, if one is open.

        Safe to call multiple times and safe to call before any command
        has ever been run — used by shutdown hooks in :mod:`bot.bot`.
        """
        async with self._lock:
            await self._close()

    async def ping(self) -> None:
        """Validate that we can connect and authenticate, then disconnect.

        Public diagnostic wrapper around :py:meth:`_ensure_connected`
        used by the bot's startup hook. We *close* the socket after a
        successful auth instead of holding it warm because:

        * The startup ping and the first real command can be separated
          by several seconds (cog setup, command sync, gateway login).
          Some MC hosts / proxies tear down idle RCON connections in
          that window, and a half-closed socket isn't detectable with
          the writer's ``is_closing()`` flag — the first command then
          writes successfully, hits an EOF on read, and surfaces a
          spurious "reconnecting" log line at startup. Closing here
          guarantees the first command opens its own fresh connection.
        * Conceptually a ping is a connectivity *test*, not a
          connection-pool warmup. Keeping the two paths independent
          makes the runtime behaviour easier to reason about.

        Raises :class:`RconError` on any connect or auth failure, with
        the actionable message produced by :py:meth:`_ensure_connected`.
        """
        async with self._lock:
            try:
                await self._ensure_connected()
            finally:
                # ``_ensure_connected`` already closes on failure; the
                # explicit close here is the success-case counterpart.
                # Idempotent, so a double-close on the failure path is
                # harmless.
                await self._close()

    # ── connection lifecycle ────────────────────────────────────
    async def _ensure_connected(self) -> None:
        """Open and authenticate a connection if we don't have one."""
        if self._writer is not None and not self._writer.is_closing():
            return

        try:
            self._reader, self._writer = await asyncio.wait_for(
                asyncio.open_connection(self._host, self._port),
                timeout=self._timeout,
            )
        except (OSError, asyncio.TimeoutError):
            raise RconError("RCON connect failed") from None

        auth_id = self._allocate_id()
        await self._write_packet(auth_id, _TYPE_LOGIN, self._password)
        # Any read failure during the auth round-trip almost always
        # means the server actively closed the socket after seeing our
        # login packet. Common causes are a wrong password (some MC
        # hosts / proxies drop the connection instead of returning the
        # spec-mandated id=-1 response), enable-rcon=false on the
        # server while a panel port-forwarder still answers TCP, or a
        # configured port that maps to a non-RCON service. Rewrap with
        # an actionable hint so the surfaced error points at the right
        # checks instead of leaking framing details or host:port.
        try:
            resp_id, _resp_type, _body = await self._read_packet()
        except RconError:
            await self._close()
            raise RconError(
                "RCON authentication failed — check RCON_PASSWORD, "
                "enable-rcon=true on the server, and that the configured "
                "port is the RCON port"
            ) from None
        # Protocol: id = -1 on auth failure, echoed id on success.
        if resp_id == -1:
            await self._close()
            raise RconError("RCON authentication failed (wrong password?)")
        # An echoed id that doesn't match our auth packet is a protocol
        # violation; refusing to proceed here keeps the unexpected
        # packet from confusing the next command's response reader.
        if resp_id != auth_id:
            await self._close()
            raise RconError(
                "RCON authentication returned unexpected response id"
            )

    async def _close(self) -> None:
        """Tear down the socket. Swallows errors — close is best-effort."""
        writer = self._writer
        self._reader = None
        self._writer = None
        if writer is None:
            return
        try:
            writer.close()
            # ``wait_closed`` can block indefinitely if the remote side
            # is unresponsive; bound it so shutdown never hangs.
            await asyncio.wait_for(writer.wait_closed(), timeout=self._timeout)
        except Exception:
            # We're dropping the reference regardless — there's nothing
            # useful a caller could do with a close error.
            pass

    # ── wire format ─────────────────────────────────────────────
    async def _send_command(self, command: str) -> str:
        """Send one command and collect its (possibly fragmented) response.

        Vanilla Minecraft caps a single response packet at ~4 kB. Longer
        responses (e.g. ``/list`` on a 1000-player server, or verbose
        plugin output) are split across multiple packets, *all* tagged
        with the original request id. There is no "last fragment" flag,
        so we use the well-known trick of sending a second, bogus
        command directly after the real one: Minecraft processes
        commands serially, so we know every packet with the real id
        has been delivered by the time we see a packet tagged with the
        bogus id.

        Connect/auth failures raise :class:`RconError` (safe to retry).
        Any failure after the command packet is written raises
        :class:`_DeliveryUncertain` so :meth:`command` can refuse
        automatic replay of mutating bodies.
        """
        await self._ensure_connected()

        cmd_id = self._allocate_id()
        # From the first write of the real command onward, Minecraft may
        # have received the body even if we never see a response.
        try:
            await self._write_packet(cmd_id, _TYPE_COMMAND, command)
            # Sentinel packet — body is irrelevant, only its id matters.
            # We deliberately don't use ``_TYPE_RESPONSE`` (2 is COMMAND
            # / 0 is RESPONSE) because some servers drop unexpected
            # type bytes.
            await self._write_packet(_TAIL_ID, _TYPE_COMMAND, "")

            fragments: list[bytes] = []
            while True:
                resp_id, _resp_type, body = await self._read_packet()
                if resp_id == _TAIL_ID:
                    break
                if resp_id == cmd_id:
                    fragments.append(body)
                # Any other id is unexpected (shouldn't happen on a
                # well-behaved server); we ignore it rather than raise so
                # one stray packet can't kill the connection.
        except RconError as exc:
            raise _DeliveryUncertain from exc

        # Minecraft uses UTF-8 for chat / tellraw output; ``replace``
        # keeps us from exploding on the odd invalid byte some plugins
        # emit when dumping binary data into the command stream.
        return b"".join(fragments).decode("utf-8", errors="replace")

    async def _write_packet(self, request_id: int, type_: int, body: str) -> None:
        """Encode and send a single RCON packet."""
        assert self._writer is not None
        body_bytes = body.encode("utf-8")
        # Length field covers everything *after* itself:
        # id(4) + type(4) + body(N) + terminator(2).
        length = 4 + 4 + len(body_bytes) + 2
        header = struct.pack("<iii", length, request_id, type_)
        self._writer.write(header + body_bytes + b"\x00\x00")
        try:
            await asyncio.wait_for(self._writer.drain(), timeout=self._timeout)
        except (OSError, asyncio.TimeoutError):
            raise RconError("RCON write failed") from None

    async def _read_packet(self) -> Tuple[int, int, bytes]:
        """Read one packet off the wire and return ``(id, type, body)``."""
        assert self._reader is not None
        try:
            length_bytes = await asyncio.wait_for(
                self._reader.readexactly(4), timeout=self._timeout
            )
            (length,) = struct.unpack("<i", length_bytes)
            # Sanity-check the declared length: vanilla MC caps response
            # payloads around 4 kB, so anything bigger is either a
            # desync or a malicious / bogus peer.
            if length < 10 or length > 4 * 1024 * 1024:
                raise RconError("RCON packet with invalid length")
            payload = await asyncio.wait_for(
                self._reader.readexactly(length), timeout=self._timeout
            )
        except (OSError, asyncio.IncompleteReadError, asyncio.TimeoutError):
            raise RconError("RCON read failed") from None

        request_id, type_ = struct.unpack("<ii", payload[:8])
        # Strip the two-byte null terminator (``\x00`` after the body,
        # then a second ``\x00`` that is the zero-length "packet
        # terminator" reserved by the protocol for future use).
        body = payload[8:-2]
        return request_id, type_, body

    # ── helpers ─────────────────────────────────────────────────
    def _allocate_id(self) -> int:
        """Return the next usable non-reserved request id."""
        value = self._next_id
        # Keep ids positive and avoid colliding with our tail sentinel.
        # At 2^31 we wrap back to 1 — the protocol doesn't care about
        # continuity, only that a command and its responses share an id.
        self._next_id = value + 1
        if self._next_id >= _TAIL_ID:
            self._next_id = 1
        return value
