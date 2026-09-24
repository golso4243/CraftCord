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
from bot.utils.text_component import (
    TellrawOutcome,
    build_broadcast_tellraw_command,
    classify_tellraw_response,
    fit_broadcast_message,
)

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
        """Broadcast ``message`` to all players as ``[Broadcast] message``.

        Vanilla ``say`` is not used. Minecraft labels an RCON ``say`` as
        ``[Rcon]`` in chat and in the server log. ``tellraw`` carries the
        same bracket style as Discord chat (``[Discord]``), with the
        message stored as literal SNBT text so quotes cannot change the
        command.
        """
        await interaction.response.defer(ephemeral=True, thinking=True)
        text = " ".join(message.replace("\r", " ").replace("\n", " ").split())
        if not text:
            await interaction.followup.send(
                "Enter a message to broadcast.",
                ephemeral=True,
                allowed_mentions=_NO_MENTIONS,
            )
            return
        try:
            fitted = fit_broadcast_message(text)
            command = build_broadcast_tellraw_command(fitted)
        except ValueError:
            await interaction.followup.send(
                "That broadcast is too long.",
                ephemeral=True,
                allowed_mentions=_NO_MENTIONS,
            )
            return
        try:
            response = await self.rcon.command(command)
        except RconError:
            await interaction.followup.send(
                _RCON_USER_ERROR, ephemeral=True, allowed_mentions=_NO_MENTIONS
            )
            return

        outcome = classify_tellraw_response(response)
        if outcome is TellrawOutcome.COMMAND_REJECTED:
            log.warning(
                "Minecraft rejected broadcast tellraw: outcome=%s", outcome.name
            )
            await interaction.followup.send(
                "Could not broadcast that message.",
                ephemeral=True,
                allowed_mentions=_NO_MENTIONS,
            )
            return
        if outcome is TellrawOutcome.NO_PLAYERS:
            log.info("Broadcast not delivered: no players online")
            await interaction.followup.send(
                "No players are online to receive that broadcast.",
                ephemeral=True,
                allowed_mentions=_NO_MENTIONS,
            )
            return
        if outcome is TellrawOutcome.UNKNOWN_OUTPUT:
            log.info(
                "Unexpected non-empty broadcast tellraw response (len=%d); "
                "not treating as rejection",
                len(response or ""),
            )

        # Players see ``[Broadcast]`` via tellraw, which vanilla does not
        # write to the server log. Record the text here so the process
        # log still shows what was commanded.
        log.info("Broadcast: %s", fitted)
        console = self.bot.get_cog("ConsoleCog")
        if console is not None:
            await console.note_broadcast(fitted)
        await interaction.followup.send(
            "Broadcast sent.", ephemeral=True, allowed_mentions=_NO_MENTIONS
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
