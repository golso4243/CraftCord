"""
Discord -> Minecraft chat bridge.

This cog is the "write" half of the chat bridge; the "read" half lives
in :mod:`bot.cogs.console`, which parses chat lines out of the server
log and forwards them to the same channel.

Flow
----
1. A user sends a message in ``CHAT_CHANNEL_ID``.
2. We resolve mentions/emoji/attachments into a plain-text rendering
   that will make sense to players in-game.
3. We build a ``tellraw @a`` SNBT text-component payload and fire it
   over RCON.

Gating rules
------------
* The message must belong to the configured guild.
* Only messages in :data:`config.chat_channel_id` trigger the bridge.
* Messages from bots or webhooks are ignored.
* The author must pass :func:`bot.utils.permissions.has_member_access`
  (configured-guild owner, Discord Administrator, or a configured
  admin/mod/member role). Users who can type in the channel but lack a
  role are silently ignored.
* Empty messages (e.g. image-only posts) are substituted with an
  ``[attachment]`` placeholder so players can see something happened.
"""
from __future__ import annotations

import logging
import re
from typing import List

import discord
from discord.ext import commands

from bot.config import config
from bot.services.rcon_service import RconError, RconService
from bot.utils.permissions import has_member_access, is_configured_guild
from bot.utils.text_component import (
    DEFAULT_MESSAGE_CHAR_LIMIT,
    TellrawOutcome,
    build_tellraw_command,
    classify_tellraw_response,
)

log = logging.getLogger(__name__)

# Minecraft chat has a soft limit of ~256 chars per message. Anything
# longer gets rejected or chopped. Leave headroom for the "[Discord]
# <Author>: " prefix that tellraw prepends visually. The RCON byte-fit
# pass may shorten further.
_MAX_MC_LEN = DEFAULT_MESSAGE_CHAR_LIMIT

# Patterns used when rendering Discord markup in-game. Matching is
# deliberately lenient — we're formatting for visual clarity, not
# parsing Discord's full grammar.
_CUSTOM_EMOJI_RE = re.compile(r"<a?:([A-Za-z0-9_]+):\d+>")
_URL_RE = re.compile(r"https?://\S+")


class ChatCog(commands.Cog):
    """Forward messages from the chat channel into Minecraft via RCON."""

    def __init__(self, bot: commands.Bot, rcon: RconService):
        self.bot = bot
        self.rcon = rcon

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        """Relay a Discord message into the game as ``tellraw @a``.

        Heavy-use gate: the very first checks are cheap id comparisons
        so that servers without the bridge configured pay essentially
        nothing for this listener, and the bot doesn't spam RCON for
        every message in every channel.
        """
        if config.chat_channel_id is None:
            return
        if message.guild is None or not is_configured_guild(message.guild):
            return
        if message.channel.id != config.chat_channel_id:
            return
        # Ignore all bot messages and webhooks — otherwise our own
        # MC -> Discord posts (or third-party webhooks) would get
        # re-forwarded into the game.
        if message.author.bot or message.webhook_id is not None:
            return
        # DMs / webhook User objects lack roles; only guild Members do.
        if not hasattr(message.author, "roles"):
            log.debug(
                "Ignoring chat bridge from non-member author %s",
                message.author.id,
            )
            return
        if not has_member_access(message.author):  # type: ignore[arg-type]
            log.debug(
                "Ignoring chat bridge from unauthorized member %s",
                message.author.id,
            )
            return

        rendered = self._render_for_minecraft(message)
        if not rendered:
            return

        author = message.author.display_name
        try:
            command = build_tellraw_command(author, rendered)
        except ValueError:
            log.warning(
                "Discord chat tellraw exceeded RCON body limit "
                "(author_len=%d message_len=%d)",
                len(author),
                len(rendered),
            )
            await self._warn_delivery_failed(message)
            return

        try:
            response = await self.rcon.command(command)
        except RconError as e:
            # Transport / auth failure. Do not log command body or the
            # exception message (may contain host details).
            log.warning(
                "Failed to bridge Discord chat to MC: %s", type(e).__name__
            )
            await self._warn_delivery_failed(message)
            return

        outcome = classify_tellraw_response(response)
        if outcome in (
            TellrawOutcome.COMMAND_REJECTED,
            TellrawOutcome.NO_PLAYERS,
        ):
            # Rejected by Minecraft or selector miss — treat as delivery
            # failure. Never log the response body (may echo user text).
            log.warning(
                "Minecraft rejected Discord chat tellraw: outcome=%s",
                outcome.name,
            )
            await self._warn_delivery_failed(message)
            return
        if outcome is TellrawOutcome.UNKNOWN_OUTPUT:
            # Do not claim acceptance or rejection; do not surface the
            # body. Length-only diagnostic for operators.
            log.info(
                "Unexpected non-empty tellraw response (len=%d); "
                "not treating as rejection",
                len(response or ""),
            )
        # ACCEPTED_NO_OUTPUT: no error text observed. This is not proof
        # a player saw the intended rendering.

    @staticmethod
    async def _warn_delivery_failed(message: discord.Message) -> None:
        """Add the warning reaction without posting raw server details."""
        try:
            await message.add_reaction("\N{WARNING SIGN}")
        except discord.DiscordException:
            pass

    # ── rendering helpers ───────────────────────────────────────
    @staticmethod
    def _render_for_minecraft(message: discord.Message) -> str:
        """Flatten a Discord message into plain text safe for in-game chat.

        Transformations applied, in order:

        1. Replace user/role/channel mentions with human-readable
           forms (``@DisplayName``, ``#channel-name``, ``@role``) —
           raw mention ids like ``<@12345>`` look like noise in-game.
        2. Strip custom emoji wrapping, keeping just the ``:name:``
           so players still see which emoji was used.
        3. If the message has attachments, append ``[attachment]``
           markers so image-only posts aren't silently empty.
        4. Collapse newlines into spaces — Minecraft chat is
           single-line, and preserving newlines breaks the tellraw
           rendering mid-sentence.
        5. Truncate to fit the soft Minecraft chat char cap (the RCON
           byte-fit pass may shorten further).
        """
        text = message.clean_content  # resolves mentions for us

        text = _CUSTOM_EMOJI_RE.sub(r":\1:", text)

        attachments: List[str] = []
        if message.attachments:
            # We deliberately don't try to inline-link every
            # attachment; `clean_content` already omits CDN URLs, and
            # pasting them into chat tends to be unreadable. A simple
            # count tag conveys "there's an image here, check Discord".
            attachments.append(f"[{len(message.attachments)} attachment(s)]")

        if message.stickers:
            attachments.append(f"[sticker: {message.stickers[0].name}]")

        text = " ".join(part for part in [text, *attachments] if part)

        # Minecraft chat is single-line.
        text = text.replace("\r", "").replace("\n", " ").strip()
        text = re.sub(r"\s{2,}", " ", text)

        if len(text) > _MAX_MC_LEN:
            text = text[: _MAX_MC_LEN - 1] + "…"
        return text


async def setup(bot: commands.Bot) -> None:
    """discord.py extension entry point — called by ``bot.load_extension``.

    Reuses the bot-level :class:`RconService` instance so messages go
    out on the same pooled connection as the slash commands.
    """
    await bot.add_cog(ChatCog(bot, bot.rcon))  # type: ignore[attr-defined]
