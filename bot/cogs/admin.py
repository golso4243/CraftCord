"""
Administrative slash commands for bot operators.

This cog exposes a small set of utility commands that are useful for keeping
the bot healthy in production:

* ``/ping``    — quick liveness / latency check (available to anyone in the
  configured guild).
* ``/botinfo`` — runtime information (Python version, discord.py version,
  uptime) for diagnostics.
* ``/sync``    — re-register the bot's slash commands with the *configured*
  guild only. Guild-scoped syncs propagate within seconds.

All responses are sent ephemerally so they don't clutter public channels.
"""
from __future__ import annotations

import logging
import platform
import time

import discord
from discord import app_commands
from discord.ext import commands

from bot.config import config
from bot.utils.permissions import require_admin

log = logging.getLogger(__name__)

_NO_MENTIONS = discord.AllowedMentions.none()


class AdminCog(commands.Cog):
    """Operational / diagnostic commands for the bot itself."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="ping", description="Check bot latency.")
    async def ping(self, interaction: discord.Interaction) -> None:
        """Report the WebSocket heartbeat latency to Discord's gateway.

        This measures the bot<->Discord gateway round trip, *not* the
        responsiveness of the Minecraft server — see ``/status`` for that.
        """
        await interaction.response.send_message(
            f"Pong! `{self.bot.latency * 1000:.0f} ms`",
            ephemeral=True,
            allowed_mentions=_NO_MENTIONS,
        )

    @app_commands.command(name="botinfo", description="Show bot info.")
    async def botinfo(self, interaction: discord.Interaction) -> None:
        """Return a small embed with runtime/version information.

        ``bot.start_time`` is set in ``bot.py`` during startup; the
        ``type: ignore`` comment silences mypy's strict attribute check
        because the attribute is attached dynamically.
        """
        uptime = int(time.time() - self.bot.start_time)  # type: ignore[attr-defined]
        embed = discord.Embed(
            title="CraftCord",
            description="Your server. Your Discord. Connected.",
            color=discord.Color.blurple(),
        )
        embed.add_field(name="Python", value=platform.python_version(), inline=True)
        embed.add_field(name="discord.py", value=discord.__version__, inline=True)
        embed.add_field(name="Uptime (s)", value=str(uptime), inline=True)
        embed.set_footer(text="Built by SwornHero")
        await interaction.response.send_message(
            embed=embed, ephemeral=True, allowed_mentions=_NO_MENTIONS
        )

    @app_commands.command(
        name="sync", description="Re-sync slash commands for the configured guild. (admin)"
    )
    @require_admin()
    async def sync(self, interaction: discord.Interaction) -> None:
        """Force a guild-scoped command tree sync for the configured guild.

        Syncing is relatively expensive and rate-limited by Discord, so it is
        gated behind the admin role. We defer the response because a sync can
        take longer than the 3 second initial-response window.

        Always targets :data:`config.guild_id`, never the invoking guild —
        the tree-wide guild check already rejects foreign guilds, and this
        keeps the sync destination explicit.
        """
        await interaction.response.defer(ephemeral=True, thinking=True)
        guild = discord.Object(id=config.guild_id)
        synced = await self.bot.tree.sync(guild=guild)
        await interaction.followup.send(
            f"Synced {len(synced)} commands to the configured guild.",
            ephemeral=True,
            allowed_mentions=_NO_MENTIONS,
        )


async def setup(bot: commands.Bot) -> None:
    """discord.py extension entry point — called by ``bot.load_extension``."""
    await bot.add_cog(AdminCog(bot))
