"""
Mirror Minecraft server output into Discord, with per-category routing.

A background task tails ``latest.log``; a second task drains the output
and dispatches each line to the right destination:

* **Chat** (``<Player> message``)       -> :data:`config.chat_channel_id`
  rendered as ``**Player**: message`` so it looks like a native Discord
  message.
* **Events** (join / leave / death /
  advancement)                           -> :data:`config.events_channel_id`
  rendered as small coloured embeds.
* **Anything else** (generic console
  output)                               -> :data:`config.console_channel_id`
  only when :data:`config.enable_console_mirror` is true, batched into
  code blocks.

Every specialised channel is optional. When a specialised channel is
missing, that category is **dropped** — it does not fall through to the
console channel. Generic console mirroring is opt-in because raw logs
can contain player IPs, UUIDs, chat, and operational detail. Lines that
contain ``[STAFF_ALERT]`` are not special; they follow the same generic
console path when mirroring is enabled.
"""
from __future__ import annotations

import asyncio
import logging
from typing import List, Optional

import discord
from discord.ext import commands

from bot.config import config
from bot.services.log_service import LogSource, build_log_source
from bot.utils.destinations import resolve_guild_text_channel
from bot.utils.formatting import (
    clean_mc_text,
    escape_minecraft_username,
    neutralize_code_fences,
)
from bot.utils.mc_log_parser import (
    AdvancementEvent,
    ChatMessage,
    DeathEvent,
    JoinEvent,
    LeaveEvent,
    parse_line,
)

log = logging.getLogger(__name__)

# Discord's hard per-message cap is 2000 characters. We reserve ~100
# chars of headroom for the surrounding code fence and a safety margin.
_MAX_CHUNK = 1900

# How often the flush task drains the generic-console buffer. Small
# enough that messages feel "live", large enough to batch bursty output.
_FLUSH_INTERVAL = 1.5

# Discord's per-field/per-description character limit is 2048/4096;
# we clip event messages well below that to keep embeds compact.
_EVENT_TEXT_LIMIT = 500

_NO_MENTIONS = discord.AllowedMentions.none()


class ConsoleCog(commands.Cog):
    """Tail ``latest.log`` and dispatch lines to the right Discord channel."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # Concrete LogSource implementation chosen at cog_load time by
        # build_log_source() — either a LocalLogTailer or a
        # PterodactylLogSource, depending on LOG_SOURCE.
        self._tailer: Optional[LogSource] = None
        self._tail_task: Optional[asyncio.Task] = None
        self._flush_task: Optional[asyncio.Task] = None
        # Only holds lines bound for the generic console channel; chat
        # and events are sent immediately without batching.
        self._console_buffer: List[str] = []
        self._lock = asyncio.Lock()
        # Resolved once on first flush iteration. ``False`` means "tried
        # and failed"; ``None`` means "not yet attempted". Cached as
        # TextChannel | None; a sentinel object marks permanent disable.
        self._console_channel: Optional[discord.TextChannel] = None
        self._console_resolved = False
        self._chat_channel: Optional[discord.TextChannel] = None
        self._chat_resolved = False
        self._events_channel: Optional[discord.TextChannel] = None
        self._events_resolved = False

    # ── lifecycle ───────────────────────────────────────────────
    async def cog_load(self) -> None:
        """Start the tail+flush tasks, or stay dormant if unconfigured.

        Log ingestion continues whenever any chat/events destination
        is set, even if console mirroring is disabled.
        """
        console_wanted = (
            config.enable_console_mirror and config.console_channel_id is not None
        )
        if not any(
            (
                console_wanted,
                config.chat_channel_id,
                config.events_channel_id,
            )
        ):
            log.info(
                "No CHAT/EVENTS channel configured and console "
                "mirroring is disabled — log ingestion inactive."
            )
            return

        self._tailer = build_log_source()
        if self._tailer is None:
            # build_log_source logs its own reason — nothing to add.
            return

        self._tail_task = asyncio.create_task(self._run_tail(), name="log-tail")
        if console_wanted:
            self._flush_task = asyncio.create_task(
                self._run_flush(), name="log-flush"
            )
        else:
            log.info(
                "Console mirroring disabled (ENABLE_CONSOLE_MIRROR is off "
                "or CONSOLE_CHANNEL_ID is unset); chat/event routing may "
                "still run."
            )

    async def cog_unload(self) -> None:
        """Cleanly stop the tailer and cancel both background tasks."""
        if self._tailer is not None:
            self._tailer.stop()
        for task in (self._tail_task, self._flush_task):
            if task is not None:
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass

    # ── tail loop ───────────────────────────────────────────────
    async def _run_tail(self) -> None:
        """Read new log lines forever and route each one by category."""
        assert self._tailer is not None
        try:
            async for raw in self._tailer.tail():
                cleaned = clean_mc_text(raw)
                # Skip blank lines and lines that were fully stripped
                # by the cleaner (e.g. nothing but colour codes).
                if not cleaned:
                    continue
                await self._dispatch(cleaned)
        except asyncio.CancelledError:
            raise
        except Exception:
            # Log-and-swallow: crashing the task would silently stop
            # log mirroring until the next bot restart.
            log.exception("Log tailer crashed")

    async def _dispatch(self, line: str) -> None:
        """Classify ``line`` and hand it to the appropriate destination.

        Missing specialised destinations drop the line. Generic console
        buffering only runs when console mirroring is explicitly enabled.
        """
        parsed = parse_line(line)

        if isinstance(parsed, ChatMessage):
            if config.chat_channel_id is not None:
                await self._send_chat(parsed)
            return
        if parsed is not None:
            if config.events_channel_id is not None:
                await self._send_event(parsed)
            return

        # Unclassified / generic console line.
        if config.enable_console_mirror and config.console_channel_id is not None:
            async with self._lock:
                self._console_buffer.append(line)

    async def note_broadcast(self, message: str) -> None:
        """Queue a broadcast line for the Discord console mirror.

        In-game broadcasts are delivered with ``tellraw``, so the
        Minecraft server log does not get a ``[Rcon]`` chat line.
        When console mirroring is on, this records ``[Broadcast]`` plus
        the text that was sent, as generic console output.
        """
        if not (
            config.enable_console_mirror and config.console_channel_id is not None
        ):
            return
        text = message.strip()
        if not text:
            return
        async with self._lock:
            self._console_buffer.append(f"[Broadcast] {text}")

    # ── chat & event senders ────────────────────────────────────
    # Minecraft-originated content is untrusted: players can type
    # @everyone, @here, <@userId>, or <@&roleId> in chat or death
    # messages. AllowedMentions.none() keeps the visible text while
    # suppressing Discord notifications.
    async def _send_chat(self, chat: ChatMessage) -> None:
        """Render a chat message as a native-looking Discord message."""
        channel = await self._resolve_chat_channel()
        if channel is None:
            return
        safe_name = escape_minecraft_username(chat.player)
        try:
            await channel.send(
                f"**[Minecraft]** **{safe_name}**: {chat.message}",
                allowed_mentions=_NO_MENTIONS,
            )
        except discord.DiscordException as e:
            log.warning("Failed to forward chat line: %s", type(e).__name__)

    async def _send_event(self, event: object) -> None:
        """Render a join/leave/death/advancement event as an embed."""
        channel = await self._resolve_events_channel()
        if channel is None:
            return

        embed = self._build_event_embed(event)
        if embed is None:
            return
        try:
            await channel.send(embed=embed, allowed_mentions=_NO_MENTIONS)
        except discord.DiscordException as e:
            log.warning("Failed to forward event: %s", type(e).__name__)

    @staticmethod
    def _build_event_embed(event: object) -> Optional[discord.Embed]:
        """Pick a colour + label for each event type.

        Returns ``None`` for unrecognised variants so new parser
        outputs can be added without breaking the console cog — they
        will simply be dropped when no events channel is configured.
        """
        if isinstance(event, JoinEvent):
            return discord.Embed(
                description=f":green_circle: **{event.player}** joined the game",
                color=discord.Color.green(),
            )
        if isinstance(event, LeaveEvent):
            return discord.Embed(
                description=f":red_circle: **{event.player}** left the game",
                color=discord.Color.light_grey(),
            )
        if isinstance(event, DeathEvent):
            msg = event.message
            if len(msg) > _EVENT_TEXT_LIMIT:
                msg = msg[:_EVENT_TEXT_LIMIT] + "…"
            return discord.Embed(
                description=f":skull: {msg}",
                color=discord.Color.dark_red(),
            )
        if isinstance(event, AdvancementEvent):
            label = {
                "advancement": "earned the advancement",
                "challenge": "completed the challenge",
                "goal": "reached the goal",
            }[event.kind]
            return discord.Embed(
                description=(
                    f":trophy: **{event.player}** {label} "
                    f"**[{event.name}]**"
                ),
                color=discord.Color.gold(),
            )
        return None

    # ── console flush loop ──────────────────────────────────────
    async def _run_flush(self) -> None:
        """Periodically drain the generic-console buffer to Discord."""
        await self.bot.wait_until_ready()

        try:
            while True:
                await asyncio.sleep(_FLUSH_INTERVAL)
                async with self._lock:
                    lines, self._console_buffer = self._console_buffer, []
                if not lines:
                    continue
                if not (
                    config.enable_console_mirror
                    and config.console_channel_id is not None
                ):
                    continue
                channel = await self._resolve_console_channel()
                if channel is None:
                    continue
                for chunk in self._chunk_lines(lines):
                    safe = neutralize_code_fences(chunk)
                    try:
                        await channel.send(
                            f"```\n{safe}\n```",
                            allowed_mentions=_NO_MENTIONS,
                        )
                    except discord.DiscordException as e:
                        log.warning(
                            "Failed to forward console chunk: %s",
                            type(e).__name__,
                        )
                        break
        except asyncio.CancelledError:
            raise

    # ── channel resolution helpers ──────────────────────────────
    async def _resolve_console_channel(self) -> Optional[discord.TextChannel]:
        if self._console_resolved:
            return self._console_channel
        self._console_channel = await resolve_guild_text_channel(
            self.bot, config.console_channel_id, setting_name="CONSOLE_CHANNEL_ID"
        )
        self._console_resolved = True
        return self._console_channel

    async def _resolve_chat_channel(self) -> Optional[discord.TextChannel]:
        if self._chat_resolved:
            return self._chat_channel
        self._chat_channel = await resolve_guild_text_channel(
            self.bot, config.chat_channel_id, setting_name="CHAT_CHANNEL_ID"
        )
        self._chat_resolved = True
        return self._chat_channel

    async def _resolve_events_channel(self) -> Optional[discord.TextChannel]:
        if self._events_resolved:
            return self._events_channel
        self._events_channel = await resolve_guild_text_channel(
            self.bot, config.events_channel_id, setting_name="EVENTS_CHANNEL_ID"
        )
        self._events_resolved = True
        return self._events_channel

    # ── chunking helper ─────────────────────────────────────────
    @staticmethod
    def _chunk_lines(lines: List[str]) -> List[str]:
        """Pack ``lines`` into ``<=_MAX_CHUNK`` strings without splitting a line."""
        chunks: List[str] = []
        current = ""
        for line in lines:
            if len(line) > _MAX_CHUNK:
                line = line[: _MAX_CHUNK - 1] + "…"
            if len(current) + len(line) + 1 > _MAX_CHUNK:
                if current:
                    chunks.append(current)
                current = line
            else:
                current = f"{current}\n{line}" if current else line
        if current:
            chunks.append(current)
        return chunks


async def setup(bot: commands.Bot) -> None:
    """discord.py extension entry point — called by ``bot.load_extension``."""
    await bot.add_cog(ConsoleCog(bot))
