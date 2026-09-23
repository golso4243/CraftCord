"""
Helpers for resolving Discord destinations that belong to the configured guild.

Every outbound route (status, chat, events, console) must verify that a
channel id resolves to a text channel *inside*
:data:`bot.config.config.guild_id`. A channel from another guild that
happens to be fetchable must never receive Minecraft output.
"""
from __future__ import annotations

import logging
from typing import Optional

import discord
from discord.ext import commands

from bot.config import config
from bot.utils.permissions import is_configured_guild

log = logging.getLogger(__name__)

# Per-setting "already warned" flags so a missing / wrong-guild channel
# produces one diagnostic instead of spamming on every refresh.
_disabled_routes: set[str] = set()


def reset_destination_diagnostics() -> None:
    """Clear the one-shot diagnostic set (used by tests)."""
    _disabled_routes.clear()


async def resolve_guild_text_channel(
    bot: commands.Bot,
    channel_id: Optional[int],
    *,
    setting_name: str,
) -> Optional[discord.TextChannel]:
    """Look up ``channel_id`` and accept it only inside the configured guild.

    Returns ``None`` when the id is unset, the channel cannot be loaded,
    it is not a text channel, or it belongs to another guild. On the
    first failure for a given ``setting_name``, logs a sanitized
    diagnostic that names the setting and never the channel id or host.
    """
    if channel_id is None:
        return None

    channel = bot.get_channel(channel_id)
    if channel is None:
        try:
            channel = await bot.fetch_channel(channel_id)
        except discord.DiscordException:
            _disable_route(
                setting_name,
                f"{setting_name} destination could not be loaded; route disabled.",
            )
            return None

    if not isinstance(channel, discord.TextChannel):
        _disable_route(
            setting_name,
            f"{setting_name} is not a text channel; route disabled.",
        )
        return None

    if not is_configured_guild(channel.guild):
        _disable_route(
            setting_name,
            f"{setting_name} is outside the configured guild; route disabled.",
        )
        return None

    return channel


def _disable_route(setting_name: str, message: str) -> None:
    """Log ``message`` once per ``setting_name`` for the process lifetime."""
    if setting_name in _disabled_routes:
        return
    _disabled_routes.add(setting_name)
    log.warning("%s", message)
