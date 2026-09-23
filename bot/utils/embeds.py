"""
Builders for the server-status embed.

The embed is the user-facing face of the bot — it's what members see in
the status channel and in ``/status`` replies — so it's worth keeping the
construction logic isolated here. That way the data-gathering code (in
:mod:`bot.cogs.status`) doesn't get mixed up with presentation details.

Data flows through this module as a :class:`StatusSnapshot` dataclass:
services produce one, :func:`build_status_embed` renders it. Keeping the
render pure (no I/O, no side effects) means it's trivially unit-testable
and safe to call from any context.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import List, Optional

import discord

from bot.utils.formatting import humanize_uptime


@dataclass
class StatusSnapshot:
    """Immutable view of the server state at a single point in time.

    Produced by :class:`bot.cogs.status.StatusCog` from multiple sources
    (SLP ping, RCON, database) and then handed to
    :func:`build_status_embed` for rendering. Every field except
    ``online``, ``host`` and ``port`` is optional because an offline
    server yields no payload.
    """

    online: bool
    host: str
    port: int
    # Optional override for the "Address" field. When set (from
    # ``PUBLIC_ADDRESS`` in config) it's rendered verbatim. When
    # unset the Address field is omitted — never fall back to the
    # backend ``host:port`` pair.
    public_address: Optional[str] = None
    version: Optional[str] = None
    motd: Optional[str] = None
    players_online: int = 0
    players_max: int = 0
    player_names: Optional[List[str]] = None
    latency_ms: Optional[float] = None
    # Unix epoch seconds; None when the server is offline. See
    # Database.mark_online/mark_offline for how this is maintained.
    # This is RCON-reachability uptime, not a measured Minecraft
    # process uptime clock.
    online_since: Optional[int] = None


def build_status_embed(s: StatusSnapshot) -> discord.Embed:
    """Render a :class:`StatusSnapshot` into a Discord embed.

    Pure function: given the same snapshot, always produces the same
    embed (modulo the ``timestamp`` footer which reflects render time).
    All layout/formatting decisions live in one place so tweaking the
    look of the status message doesn't require touching the cogs.
    """
    # Online vs offline pick different titles, descriptions and colors up
    # front; all subsequent fields are either shared or online-only.
    if s.online:
        embed = discord.Embed(
            title="🟢 Server Online",
            description=s.motd or "Minecraft server is up and running.",
            color=discord.Color.green(),
        )
    else:
        embed = discord.Embed(
            title="🔴 Server Offline",
            description="Cannot reach the Minecraft server right now.",
            color=discord.Color.red(),
        )

    # Prefer an explicitly configured public address. Never fall back
    # to MC_HOST / RCON_HOST — those may be private panel backends.
    if s.public_address:
        embed.add_field(name="Address", value=f"`{s.public_address}`", inline=True)
    if s.version:
        embed.add_field(name="Version", value=s.version, inline=True)
    if s.latency_ms is not None:
        embed.add_field(name="Ping", value=f"{s.latency_ms:.0f} ms", inline=True)

    if s.online:
        embed.add_field(
            name="Players",
            value=f"{s.players_online} / {s.players_max}",
            inline=True,
        )

        # Uptime is computed at render time from the stored "online since"
        # epoch. Falls back to a dash if we don't have the timestamp yet
        # (e.g. very first tick after the bot starts).
        uptime = (
            humanize_uptime(int(time.time()) - s.online_since)
            if s.online_since
            else "—"
        )
        embed.add_field(name="Uptime", value=uptime, inline=True)

        if s.player_names:
            # Sort for stability — without this the field would reshuffle
            # on every refresh as the SLP sample changes order.
            names = ", ".join(sorted(s.player_names))
            # Discord embed fields cap at 1024 characters; we clip a bit
            # early to leave room for the ellipsis marker.
            if len(names) > 1000:
                names = names[:1000] + "…"
            embed.add_field(name="Who's online", value=names, inline=False)

    # Discord renders ``footer + timestamp`` as "Last updated · 12:34" in
    # the client, giving users a clear "freshness" indicator.
    embed.set_footer(text="Last updated")
    embed.timestamp = discord.utils.utcnow()
    return embed
