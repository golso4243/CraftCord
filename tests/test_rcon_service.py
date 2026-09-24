"""Tests for :mod:`bot.services.rcon_service` connection / auth handling.

Each test stands up a small in-process TCP server on ``127.0.0.1`` and
points an :class:`RconService` at it. This is heavier than pure unit
tests but it's the only way to exercise the wire-level error paths
(EOF during auth, ``id=-1`` rejection, mid-flight disconnect after a
command is received, …) without mocking out the service's own
internals — which is exactly the surface we want to keep honest after
the user-reported "0 bytes read on a total of 4 expected bytes"
incident.
"""
from __future__ import annotations

import asyncio
import struct
from typing import Awaitable, Callable, List, Optional

import pytest

from bot.services.rcon_service import RconError, RconService, _TAIL_ID

# Longer than Windows' ~200 ms Nagle / delayed-ACK hold, so a tail that
# was written in the same burst as the command shows up in the read
# buffer before we decide the client waited. Short enough that it still
# fits inside the service's 1 s per-read timeout.
_PIPELINE_SETTLE_S = 0.35

# Server-to-client response packets use type 0 (RESPONSE) per the Valve
# RCON spec. Mirrored here so the test file stands alone.
_TYPE_RESPONSE = 0
_TYPE_COMMAND = 2


def _make_packet(request_id: int, type_: int, body: bytes = b"") -> bytes:
    """Encode an RCON packet exactly the way Minecraft's server does."""
    length = 4 + 4 + len(body) + 2
    return struct.pack("<iii", length, request_id, type_) + body + b"\x00\x00"


async def _read_packet(
    reader: asyncio.StreamReader,
) -> tuple[int, int, bytes]:
    """Consume one full RCON packet; return ``(id, type, body)``."""
    length_bytes = await reader.readexactly(4)
    (length,) = struct.unpack("<i", length_bytes)
    payload = await reader.readexactly(length)
    request_id, type_ = struct.unpack("<ii", payload[:8])
    body = payload[8:-2]
    return request_id, type_, body


async def _read_login_packet(reader: asyncio.StreamReader) -> int:
    """Consume one full RCON packet from the client and return its id."""
    request_id, _type, _body = await _read_packet(reader)
    return request_id


async def _auth_ok(
    reader: asyncio.StreamReader, writer: asyncio.StreamWriter
) -> None:
    """Accept a login packet and echo its id (successful auth)."""
    request_id = await _read_login_packet(reader)
    writer.write(_make_packet(request_id, _TYPE_RESPONSE))
    await writer.drain()


def _unread(reader: asyncio.StreamReader) -> bytes:
    """Bytes already pulled off the socket but not consumed by the test.

    A tail written in the same burst as the command lands here once the
    command packet has been framed out.
    """
    return bytes(getattr(reader, "_buffer", b""))


async def _pending_client_bytes(reader: asyncio.StreamReader) -> bytes:
    """Return client bytes queued behind the packet we just consumed."""
    await asyncio.sleep(_PIPELINE_SETTLE_S)
    return _unread(reader)


async def _respond_then_take_tail(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    *,
    response: bytes,
    received: List[bytes],
) -> None:
    """Answer one command, then read the tail that follows that answer.

    The tail is a later write. Reading it out of the same burst as the
    command deadlocks the client and reproduces the vanilla session
    drop this suite guards against (MC-87863).
    """
    rid, type_, body = await _read_packet(reader)
    assert type_ == _TYPE_COMMAND
    assert body
    received.append(body)
    writer.write(_make_packet(rid, _TYPE_RESPONSE, response))
    await writer.drain()

    tail_id, tail_type, _tail_body = await _read_packet(reader)
    assert tail_type == _TYPE_COMMAND
    writer.write(_make_packet(tail_id, _TYPE_RESPONSE))
    await writer.drain()
    await asyncio.sleep(0.05)


ClientHandler = Callable[
    [asyncio.StreamReader, asyncio.StreamWriter], Awaitable[None]
]


async def _run_with_fake_server(
    on_client: ClientHandler,
    body: Callable[[RconService], Awaitable[None]],
) -> None:
    """Start a one-shot fake RCON server on an ephemeral port and run
    ``body(service)`` against it.

    The short ``timeout`` keeps test runs snappy when the service path
    we're exercising would otherwise wait the full default 5 s.
    """
    server = await asyncio.start_server(on_client, host="127.0.0.1", port=0)
    host, port = server.sockets[0].getsockname()[:2]
    svc = RconService(host, port, "password", timeout=1.0)
    try:
        async with server:
            await body(svc)
    finally:
        await svc.close()


def test_ping_succeeds_when_server_echoes_auth() -> None:
    """Happy path: the server echoes the client's auth id and ping returns."""

    async def on_client(
        reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        request_id = await _read_login_packet(reader)
        writer.write(_make_packet(request_id, _TYPE_RESPONSE))
        await writer.drain()
        # Hold the socket open long enough for the client to read the
        # response before we tear it down on test exit.
        await asyncio.sleep(0.1)
        writer.close()

    async def body(svc: RconService) -> None:
        await svc.ping()

    asyncio.run(_run_with_fake_server(on_client, body))


def test_ping_reports_wrong_password_on_id_minus_one() -> None:
    """Spec-compliant auth failure: server returns ``id=-1``."""

    async def on_client(
        reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        await _read_login_packet(reader)
        writer.write(_make_packet(-1, _TYPE_RESPONSE))
        await writer.drain()
        writer.close()

    async def body(svc: RconService) -> None:
        with pytest.raises(RconError) as exc_info:
            await svc.ping()
        assert "wrong password" in str(exc_info.value).lower()

    asyncio.run(_run_with_fake_server(on_client, body))


def test_ping_reports_actionable_message_when_server_closes_during_auth() -> None:
    """Bug-reproduction case: server accepts the login packet then drops
    the connection without responding (the pattern the user saw).

    The error message must steer operators at the three knobs that
    actually fix this scenario — password, ``enable-rcon=true``, and the
    configured port — instead of the raw "0 bytes read on a total of 4
    expected bytes" framing detail it used to leak.
    """

    async def on_client(
        reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        await _read_login_packet(reader)
        writer.close()

    async def body(svc: RconService) -> None:
        with pytest.raises(RconError) as exc_info:
            await svc.ping()
        msg = str(exc_info.value)
        assert "authentication failed" in msg.lower()
        assert "RCON_PASSWORD" in msg
        assert "enable-rcon" in msg

    asyncio.run(_run_with_fake_server(on_client, body))


def test_ping_reports_unexpected_response_id() -> None:
    """Defensive case: server echoes a positive id that isn't ours."""

    async def on_client(
        reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        await _read_login_packet(reader)
        # 0x12345678 is arbitrary but deliberately not the client's
        # auth id (which the service allocates starting at 1).
        writer.write(_make_packet(0x12345678, _TYPE_RESPONSE))
        await writer.drain()
        await asyncio.sleep(0.1)
        writer.close()

    async def body(svc: RconService) -> None:
        with pytest.raises(RconError) as exc_info:
            await svc.ping()
        assert "unexpected response id" in str(exc_info.value).lower()

    asyncio.run(_run_with_fake_server(on_client, body))


async def _run_multi_connection_server(
    handle_connection: Callable[
        [int, asyncio.StreamReader, asyncio.StreamWriter], Awaitable[None]
    ],
    body: Callable[[RconService], Awaitable[None]],
    *,
    expected_connections: int,
) -> None:
    """Fake RCON server that accepts multiple sequential connections.

    ``handle_connection(index, reader, writer)`` is called for each
    accepted client. ``expected_connections`` bounds how many we wait
    for so the test does not hang if the client under-connects.
    """
    connection_index = 0
    lock = asyncio.Lock()
    seen = asyncio.Event()
    seen_count = 0

    async def on_client(
        reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        nonlocal connection_index, seen_count
        async with lock:
            idx = connection_index
            connection_index += 1
            seen_count += 1
            if seen_count >= expected_connections:
                seen.set()
        try:
            await handle_connection(idx, reader, writer)
        finally:
            try:
                writer.close()
            except Exception:
                pass

    server = await asyncio.start_server(on_client, host="127.0.0.1", port=0)
    host, port = server.sockets[0].getsockname()[:2]
    svc = RconService(host, port, "password", timeout=1.0)
    try:
        async with server:
            await body(svc)
            # Give a reconnect attempt a moment to open if one is pending.
            try:
                await asyncio.wait_for(seen.wait(), timeout=2.0)
            except asyncio.TimeoutError:
                pass
    finally:
        await svc.close()


def test_mutating_command_not_replayed_after_disconnect_before_reply() -> None:
    """Server receives ``say`` then drops before any response.

    Delivery is uncertain; the client must not send ``say`` a second time.
    """
    received: List[bytes] = []

    async def handle(
        idx: int, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        await _auth_ok(reader, writer)
        while True:
            try:
                _rid, type_, body = await _read_packet(reader)
            except (asyncio.IncompleteReadError, ConnectionError, OSError):
                break
            if type_ == _TYPE_COMMAND and body:
                received.append(body)
                # Drop immediately after seeing the real command body —
                # before any response packets.
                writer.close()
                return

    async def body(svc: RconService) -> None:
        with pytest.raises(RconError) as exc_info:
            await svc.command("say hello")
        assert "RCON command failed" in str(exc_info.value)
        # Host/port must stay out of the user-facing surface.
        assert "127.0.0.1" not in str(exc_info.value)

    asyncio.run(
        _run_multi_connection_server(handle, body, expected_connections=1)
    )
    assert received == [b"say hello"]


def test_tellraw_not_replayed_after_disconnect_before_reply() -> None:
    """``tellraw`` is mutating chat delivery — never auto-replayed."""
    received: List[bytes] = []

    async def handle(
        idx: int, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        await _auth_ok(reader, writer)
        while True:
            try:
                _rid, type_, body = await _read_packet(reader)
            except (asyncio.IncompleteReadError, ConnectionError, OSError):
                break
            if type_ == _TYPE_COMMAND and body:
                received.append(body)
                writer.close()
                return

    async def body(svc: RconService) -> None:
        with pytest.raises(RconError):
            await svc.command('tellraw @a [{text:"hi"}]')

    asyncio.run(
        _run_multi_connection_server(handle, body, expected_connections=1)
    )
    assert len(received) == 1
    assert received[0].startswith(b"tellraw ")


def test_list_replayed_once_when_replay_if_uncertain() -> None:
    """Read-only ``list`` may be resent once after an uncertain loss."""
    received: List[bytes] = []

    async def handle(
        idx: int, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        await _auth_ok(reader, writer)
        if idx == 0:
            # First connection: accept command, then drop with no reply.
            while True:
                try:
                    _rid, type_, body = await _read_packet(reader)
                except (asyncio.IncompleteReadError, ConnectionError, OSError):
                    break
                if type_ == _TYPE_COMMAND and body:
                    received.append(body)
                    writer.close()
                    return
            return

        # Second connection: answer list, then the tail that follows.
        await _respond_then_take_tail(
            reader, writer, response=b"There are 0", received=received
        )

    async def body(svc: RconService) -> None:
        out = await svc.command("list", replay_if_uncertain=True)
        assert "There are 0" in out

    asyncio.run(
        _run_multi_connection_server(handle, body, expected_connections=2)
    )
    assert received == [b"list", b"list"]


def test_pre_send_failure_retries_once_then_sends() -> None:
    """Connect/auth failure before any command write still allows one retry.

    First connection rejects auth; second authenticates and answers.
    The command body must appear exactly once on the wire.
    """
    received: List[bytes] = []

    async def handle(
        idx: int, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        if idx == 0:
            await _read_login_packet(reader)
            writer.close()
            return

        await _auth_ok(reader, writer)
        await _respond_then_take_tail(
            reader, writer, response=b"ok", received=received
        )

    async def body(svc: RconService) -> None:
        # Mutating body, but failure was *before* send — one retry is ok.
        out = await svc.command("say once")
        assert out == "ok"

    asyncio.run(
        _run_multi_connection_server(handle, body, expected_connections=2)
    )
    assert received == [b"say once"]


def test_tail_not_written_before_first_command_response() -> None:
    """MC-87863: the tail must follow the first command-id response.

    Vanilla does one socket read and drops the session unless that read
    is exactly one RCON packet. A tail already buffered when the command
    has been consumed — before any response is sent — fails this test.
    Fragment bodies that share the command id are still concatenated,
    and the tail is written once.
    """
    problems: List[str] = []
    tails: List[tuple[int, int]] = []

    async def handle(
        idx: int, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        await _auth_ok(reader, writer)
        rid, type_, body = await _read_packet(reader)
        assert type_ == _TYPE_COMMAND
        assert body == b"list"
        if await _pending_client_bytes(reader):
            problems.append(
                "tail packet was written before the first command response"
            )

        writer.write(_make_packet(rid, _TYPE_RESPONSE, b"There are "))
        writer.write(
            _make_packet(rid, _TYPE_RESPONSE, b"0 of a max of 20")
        )
        await writer.drain()

        tail_id, tail_type, _tail_body = await _read_packet(reader)
        tails.append((tail_id, tail_type))
        if await _pending_client_bytes(reader):
            problems.append("tail packet was written more than once")

        writer.write(_make_packet(tail_id, _TYPE_RESPONSE))
        await writer.drain()
        await asyncio.sleep(0.05)

    async def body(svc: RconService) -> None:
        out = await svc.command("list")
        assert out == "There are 0 of a max of 20"

    asyncio.run(
        _run_multi_connection_server(handle, body, expected_connections=1)
    )
    assert problems == []
    assert tails == [(_TAIL_ID, _TYPE_COMMAND)]
