"""
Async wrapper around the :mod:`mcstatus` library.

We talk to the Minecraft server via the public Server List Ping (SLP)
protocol to fetch liveness, version, MOTD and player counts. This is the
same query the vanilla client uses to render the "server entry" on the
multiplayer menu, so it works against any compliant Java server without
needing RCON or special plugins.

The upstream ``mcstatus`` API exposes an async variant we forward to here.
Errors are captured and translated into a single offline :class:`PingResult`
so callers can safely ``await ping()`` from a loop without worrying about
exception handling — network blips simply show up as "offline" until the
next tick.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import List, Optional

from mcstatus import JavaServer

log = logging.getLogger(__name__)


@dataclass
class PingResult:
    """Flattened, UI-friendly view of a server status response.

    Every field except ``online`` is optional because an offline server
    (``online=False``) has no status payload. Keeping the shape flat makes
    the data easy to drop straight into an embed without further
    transformation.
    """

    online: bool
    version: Optional[str] = None
    motd: Optional[str] = None
    players_online: int = 0
    players_max: int = 0
    player_names: Optional[List[str]] = None
    latency_ms: Optional[float] = None


class McStatusService:
    """Ping a single Java Edition Minecraft server by host/port."""

    def __init__(self, host: str, port: int):
        # mcstatus accepts the combined "host:port" form, which is also the
        # format it prints in log messages — handy for diagnostics.
        self._address = f"{host}:{port}"

    async def ping(self) -> PingResult:
        """Return a :class:`PingResult`, never raising on network errors.

        Any failure (DNS issue, connection refused, timeout, malformed
        response, …) is collapsed into ``PingResult(online=False)``. This
        keeps the call site simple: the status cog can ping on a fixed
        schedule without wrapping every iteration in ``try/except``.
        """
        try:
            server = await JavaServer.async_lookup(self._address)
            status = await server.async_status()
        except Exception as e:
            # Debug level because transient unreachability is expected
            # during restarts; we don't want to spam warnings in that case.
            log.debug("mcstatus ping failed: %s", e)
            return PingResult(online=False)

        # MOTD normalisation: modern mcstatus returns a rich object with a
        # ``to_plain()`` helper, but older versions expose raw JSON via
        # ``description``. We accept either so the service keeps working
        # across dependency upgrades.
        motd: Optional[str] = None
        try:
            motd = status.motd.to_plain()
        except Exception:
            motd = getattr(status, "description", None)
            if isinstance(motd, dict):
                motd = motd.get("text")

        # The player sample is optional and truncated by the server — most
        # vanilla servers only include up to 12 names. Treat it as a
        # best-effort preview, not an exhaustive list.
        names: Optional[List[str]] = None
        sample = getattr(status.players, "sample", None)
        if sample:
            names = [p.name for p in sample if getattr(p, "name", None)]

        return PingResult(
            online=True,
            version=getattr(status.version, "name", None),
            motd=motd,
            players_online=status.players.online,
            players_max=status.players.max,
            player_names=names,
            latency_ms=float(status.latency) if status.latency is not None else None,
        )
