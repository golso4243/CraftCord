"""
Slash commands that proxy to the Minecraft server via RCON.

Commands are grouped by the minimum permission level required:

* Member    — ``/list``                          (read-only info)
* Moderator — ``/say``, ``/kick``, ``/ban``, ``/pardon``, ``/whitelist …``

Design notes:

* All handlers ``defer`` before doing any RCON I/O. RCON can be slow or time
  out, and Discord requires an initial response within 3 seconds.
* RCON output is cleaned of Minecraft color / formatting codes and truncated
  to fit inside a single Discord code block (see ``_truncate_for_discord``).
* There is no raw arbitrary-command endpoint. Only the named commands above
  are exposed.
"""
from __future__ import annotations

import logging

import discord
from discord import app_commands
from discord.ext import commands

from bot.services.rcon_service import RconError, RconService
from bot.utils.formatting import clean_mc_text, neutralize_code_fences
from bot.utils.minecraft import (
    INVALID_MINECRAFT_USERNAME_MSG,
    normalize_minecraft_username,
    sanitize_moderation_reason,
)
from bot.utils.permissions import require_member, require_mod

log = logging.getLogger(__name__)

_NO_MENTIONS = discord.AllowedMentions.none()
_RCON_USER_ERROR = "Could not reach the Minecraft server. Try again later."


def _truncate_for_discord(text: str, limit: int = 1900) -> str:
    """Normalize RCON output for safe inclusion in a Discord code block.

    Steps:

    1. Strip Minecraft formatting codes and control characters.
    2. Neutralize embedded triple-backtick sequences.
    3. Substitute a placeholder when output is empty (some commands legally
       return nothing).
    4. Truncate to ``limit`` characters to stay well below Discord's 2000
       character per-message cap (the surrounding code fence + a safety
       margin consume the rest).
    """
    text = neutralize_code_fences(clean_mc_text(text) or "(no output)")
    if len(text) <= limit:
        return text
    return text[:limit] + "\n… (truncated)"


class RconCog(commands.Cog):
    """Expose vetted RCON operations as Discord slash commands."""

    def __init__(self, bot: commands.Bot, rcon: RconService):
        self.bot = bot
        self.rcon = rcon

    # ── member-level ──────────────────────────────────────────────
    @app_commands.command(name="list", description="List online players.")
    @require_member()
    async def list_players(self, interaction: discord.Interaction) -> None:
        """Public successful reply; failures are ephemeral and sanitized."""
        await interaction.response.defer(thinking=True)
        try:
            out = await self.rcon.command("list", replay_if_uncertain=True)
        except RconError:
            await interaction.followup.send(
                _RCON_USER_ERROR, ephemeral=True, allowed_mentions=_NO_MENTIONS
            )
            return
        await interaction.followup.send(
            f"```\n{_truncate_for_discord(out)}\n```",
            allowed_mentions=_NO_MENTIONS,
        )

    # ── mod-level ─────────────────────────────────────────────────
    @app_commands.command(name="say", description="Broadcast a message in-game. (mod)")
    @app_commands.describe(message="Message to broadcast")
    @require_mod()
    async def say(self, interaction: discord.Interaction, message: str) -> None:
        """Broadcast ``message`` to all players via the vanilla ``say`` command.

        The naive approach of interpolating the message directly into an
        RCON string is unsafe because a double-quote can terminate the
        argument. We replace ``"`` with ``'`` as a minimal, conservative
        sanitizer — it preserves intent while preventing accidental command
        injection.
        """
        await interaction.response.defer(ephemeral=True, thinking=True)
        safe = message.replace('"', "'")
        try:
            await self.rcon.command(f'say {safe}')
        except RconError:
            await interaction.followup.send(
                _RCON_USER_ERROR, ephemeral=True, allowed_mentions=_NO_MENTIONS
            )
            return
        await interaction.followup.send(
            "Message sent.", ephemeral=True, allowed_mentions=_NO_MENTIONS
        )

    @app_commands.command(name="kick", description="Kick a player. (mod)")
    @app_commands.describe(player="Player name", reason="Reason (optional)")
    @require_mod()
    async def kick(
        self,
        interaction: discord.Interaction,
        player: str,
        reason: str = "Kicked by a moderator",
    ) -> None:
        """Kick ``player`` from the server with an optional ``reason``."""
        try:
            player = normalize_minecraft_username(player)
        except ValueError:
            await interaction.response.send_message(
                INVALID_MINECRAFT_USERNAME_MSG,
                ephemeral=True,
                allowed_mentions=_NO_MENTIONS,
            )
            return
        reason = sanitize_moderation_reason(reason)
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            out = await self.rcon.command(f"kick {player} {reason}")
        except RconError:
            await interaction.followup.send(
                _RCON_USER_ERROR, ephemeral=True, allowed_mentions=_NO_MENTIONS
            )
            return
        await interaction.followup.send(
            f"```\n{_truncate_for_discord(out)}\n```",
            ephemeral=True,
            allowed_mentions=_NO_MENTIONS,
        )

    @app_commands.command(name="ban", description="Ban a player. (mod)")
    @app_commands.describe(player="Player name", reason="Reason (optional)")
    @require_mod()
    async def ban(
        self,
        interaction: discord.Interaction,
        player: str,
        reason: str = "Banned by a moderator",
    ) -> None:
        """Ban ``player`` from the server with an optional ``reason``.

        This proxies to the vanilla ``ban`` command (name-based ban). For
        IP bans, use the in-game console.
        """
        try:
            player = normalize_minecraft_username(player)
        except ValueError:
            await interaction.response.send_message(
                INVALID_MINECRAFT_USERNAME_MSG,
                ephemeral=True,
                allowed_mentions=_NO_MENTIONS,
            )
            return
        reason = sanitize_moderation_reason(reason)
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            out = await self.rcon.command(f"ban {player} {reason}")
        except RconError:
            await interaction.followup.send(
                _RCON_USER_ERROR, ephemeral=True, allowed_mentions=_NO_MENTIONS
            )
            return
        await interaction.followup.send(
            f"```\n{_truncate_for_discord(out)}\n```",
            ephemeral=True,
            allowed_mentions=_NO_MENTIONS,
        )

    @app_commands.command(name="pardon", description="Unban a player. (mod)")
    @app_commands.describe(player="Player name")
    @require_mod()
    async def pardon(
        self,
        interaction: discord.Interaction,
        player: str,
    ) -> None:
        """Remove ``player`` from the ban list (vanilla ``pardon``)."""
        try:
            player = normalize_minecraft_username(player)
        except ValueError:
            await interaction.response.send_message(
                INVALID_MINECRAFT_USERNAME_MSG,
                ephemeral=True,
                allowed_mentions=_NO_MENTIONS,
            )
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            out = await self.rcon.command(f"pardon {player}")
        except RconError:
            await interaction.followup.send(
                _RCON_USER_ERROR, ephemeral=True, allowed_mentions=_NO_MENTIONS
            )
            return
        await interaction.followup.send(
            f"```\n{_truncate_for_discord(out)}\n```",
            ephemeral=True,
            allowed_mentions=_NO_MENTIONS,
        )

    # A command group renders in Discord as ``/whitelist add`` and
    # ``/whitelist remove``, which keeps related operations tidy.
    whitelist = app_commands.Group(
        name="whitelist", description="Manage the server whitelist."
    )

    @whitelist.command(
        name="add",
        description="Add a player to the whitelist on the configured Minecraft server.",
    )
    @require_mod()
    async def whitelist_add(
        self, interaction: discord.Interaction, player: str
    ) -> None:
        """Add ``player`` to the whitelist on the configured Minecraft server."""
        try:
            player = normalize_minecraft_username(player)
        except ValueError:
            await interaction.response.send_message(
                INVALID_MINECRAFT_USERNAME_MSG,
                ephemeral=True,
                allowed_mentions=_NO_MENTIONS,
            )
            return
        await interaction.response.defer(ephemeral=True, thinking=True)

        try:
            out = await self.rcon.command(f"whitelist add {player}")
        except RconError:
            await interaction.followup.send(
                _RCON_USER_ERROR, ephemeral=True, allowed_mentions=_NO_MENTIONS
            )
            return
        await interaction.followup.send(
            f"```\n{_truncate_for_discord(out)}\n```",
            ephemeral=True,
            allowed_mentions=_NO_MENTIONS,
        )

    @whitelist.command(
        name="remove",
        description="Remove a player from the whitelist on the configured Minecraft server.",
    )
    @require_mod()
    async def whitelist_remove(
        self, interaction: discord.Interaction, player: str
    ) -> None:
        """Remove ``player`` from the whitelist on the configured Minecraft server."""
        try:
            player = normalize_minecraft_username(player)
        except ValueError:
            await interaction.response.send_message(
                INVALID_MINECRAFT_USERNAME_MSG,
                ephemeral=True,
                allowed_mentions=_NO_MENTIONS,
            )
            return
        await interaction.response.defer(ephemeral=True, thinking=True)

        try:
            out = await self.rcon.command(f"whitelist remove {player}")
        except RconError:
            await interaction.followup.send(
                _RCON_USER_ERROR, ephemeral=True, allowed_mentions=_NO_MENTIONS
            )
            return
        await interaction.followup.send(
            f"```\n{_truncate_for_discord(out)}\n```",
            ephemeral=True,
            allowed_mentions=_NO_MENTIONS,
        )


async def setup(bot: commands.Bot) -> None:
    """discord.py extension entry point — called by ``bot.load_extension``.

    The shared ``RconService`` instance is attached to the bot in ``bot.py``
    so that every cog that needs RCON uses the same connection pool.
    """
    await bot.add_cog(RconCog(bot, bot.rcon))  # type: ignore[attr-defined]
