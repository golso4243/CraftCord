"""
Deliver Minecraft chat / player events under the player's own identity.

When ``CHAT_IDENTITY_MODE`` or ``EVENTS_IDENTITY_MODE`` is ``player``,
the console cog hands already-validated destination channels to
:class:`PlayerIdentityService`, which posts through one CraftCord-owned
webhook per channel and overrides the sender name / avatar per message
(``{player} • Minecraft`` plus the player's head). Bot mode never calls
this module, so installs without Manage Webhooks are unaffected.

Delivery outcomes
-----------------
:class:`DeliveryResult` tells the caller what to do next:

* ``CONFIRMED`` — Discord returned the created message (``wait=True``).
* ``FAILED`` — definitely not posted (setup refused, 4xx, webhook gone
  after the bounded recovery). The caller may post the bot-format
  message in the same channel.
* ``UNCONFIRMED`` — timeout, connection drop mid-request, or 5xx after
  discord.py's own retries. The message may already be visible, so the
  caller must **not** post a second copy. A rare drop is preferred to a
  duplicate.

Avatars
-------
:class:`MinecraftHeadResolver` maps a validated username to a UUID via
the Minecraft Services profile lookup and builds a Minotar head URL.
Only the username goes to Minecraft Services and only the UUID appears
in the Minotar URL (Discord, not CraftCord, fetches the image). Offline
-mode or server-side skins cannot be matched to official profiles; any
lookup failure yields an explicit fallback avatar so a previous
player's head never carries over.
"""
from __future__ import annotations

import asyncio
import enum
import logging
import re
import time
from collections import OrderedDict
from typing import Callable, Dict, Optional, Tuple

import aiohttp
import discord

log = logging.getLogger(__name__)

__all__ = [
    "DeliveryResult",
    "MinecraftHeadResolver",
    "PlayerIdentityService",
    "WEBHOOK_NAME",
    "webhook_username",
]

# Stored name of the webhook CraftCord creates. Adoption also requires
# the webhook to be created by this bot user, so the name alone is
# never enough to take over someone else's webhook.
WEBHOOK_NAME = "CraftCord"
_SENDER_SUFFIX = " \u2022 Minecraft"
# Discord's documented webhook username limit.
_WEBHOOK_USERNAME_MAX = 80
# Discord rejects webhook usernames containing these substrings.
_FORBIDDEN_USERNAME_PARTS = ("discord", "clyde")

_MC_NAME_RE = re.compile(r"^[A-Za-z0-9_]{1,16}$")
_UUID_RE = re.compile(r"^[0-9a-f]{32}$")

PROFILE_LOOKUP_URL = (
    "https://api.minecraftservices.com/minecraft/profile/lookup/name/{name}"
)
# Minotar "Avatar With Helm" (face plus hat layer). Crafatar is not
# usable: it blocks Discord's image proxy (crafatar/crafatar#322), so
# Discord shows its default avatar instead of the head.
HEAD_URL = "https://minotar.net/helm/{uuid}/128.png"
# MHF_Steve's profile UUID; only used when the bot user's own avatar is
# unavailable.
_LAST_RESORT_AVATAR = HEAD_URL.format(uuid="c06f89064c8a49119c29ea1dbd1aab82")

# Minimum spacing between setup attempts / webhook recreation for a
# channel, and between repeated warnings of the same kind.
_COOLDOWN_SECONDS = 60.0
_WARN_INTERVAL = 60.0

_NO_MENTIONS = discord.AllowedMentions.none()


class DeliveryResult(enum.Enum):
    CONFIRMED = "confirmed"
    FAILED = "failed"
    UNCONFIRMED = "unconfirmed"


def webhook_username(player: str) -> str:
    """Return the per-message sender name shown for ``player``."""
    return (player + _SENDER_SUFFIX)[:_WEBHOOK_USERNAME_MAX]


class _RateLimitedWarner:
    """Emit each warning key at most once per interval."""

    def __init__(self, clock: Callable[[], float], interval: float) -> None:
        self._clock = clock
        self._interval = interval
        self._last: Dict[str, float] = {}

    def warn(self, key: str, msg: str, *args: object) -> None:
        now = self._clock()
        last = self._last.get(key)
        if last is not None and now - last < self._interval:
            return
        self._last[key] = now
        log.warning(msg, *args)


class MinecraftHeadResolver:
    """Resolve Minecraft usernames to Minotar head URLs with caching."""

    def __init__(
        self,
        *,
        session_factory: Optional[Callable[[], aiohttp.ClientSession]] = None,
        clock: Callable[[], float] = time.monotonic,
        max_entries: int = 256,
        success_ttl: float = 3600.0,
        failure_ttl: float = 600.0,
        timeout: float = 5.0,
    ) -> None:
        self._session_factory = session_factory
        self._session: Optional[aiohttp.ClientSession] = None
        self._clock = clock
        self._max_entries = max_entries
        self._success_ttl = success_ttl
        self._failure_ttl = failure_ttl
        self._timeout = timeout
        # lowercase name -> (expires_at, uuid or None for a cached miss)
        self._cache: "OrderedDict[str, Tuple[float, Optional[str]]]" = OrderedDict()
        self._pending: Dict[str, "asyncio.Future[Optional[str]]"] = {}
        self._warner = _RateLimitedWarner(clock, _WARN_INTERVAL)

    async def head_url(self, player: str) -> Optional[str]:
        """Return a head URL for ``player``, or ``None`` if unavailable."""
        if not _MC_NAME_RE.match(player):
            return None
        uuid = await self._uuid_for(player)
        return HEAD_URL.format(uuid=uuid) if uuid else None

    async def _uuid_for(self, player: str) -> Optional[str]:
        key = player.lower()
        now = self._clock()
        entry = self._cache.get(key)
        if entry is not None:
            if entry[0] > now:
                self._cache.move_to_end(key)
                return entry[1]
            del self._cache[key]

        pending = self._pending.get(key)
        if pending is not None:
            return await asyncio.shield(pending)

        future: "asyncio.Future[Optional[str]]" = (
            asyncio.get_running_loop().create_future()
        )
        self._pending[key] = future
        try:
            try:
                uuid = await self._lookup(player)
            except Exception as e:
                self._warner.warn(
                    f"error:{type(e).__name__}",
                    "Minecraft profile lookup failed (%s); using fallback avatar",
                    type(e).__name__,
                )
                uuid = None
            ttl = self._success_ttl if uuid else self._failure_ttl
            self._store(key, uuid, self._clock() + ttl)
            future.set_result(uuid)
            return uuid
        except BaseException:
            if not future.done():
                future.cancel()
            raise
        finally:
            self._pending.pop(key, None)

    def _store(self, key: str, uuid: Optional[str], expires: float) -> None:
        self._cache[key] = (expires, uuid)
        self._cache.move_to_end(key)
        while len(self._cache) > self._max_entries:
            self._cache.popitem(last=False)

    async def _lookup(self, player: str) -> Optional[str]:
        session = self._get_session()
        url = PROFILE_LOOKUP_URL.format(name=player)
        try:
            async with session.get(
                url, timeout=aiohttp.ClientTimeout(total=self._timeout)
            ) as resp:
                if resp.status == 404:
                    return None
                if resp.status != 200:
                    self._warner.warn(
                        f"status:{resp.status}",
                        "Minecraft profile lookup returned HTTP %d; "
                        "using fallback avatar",
                        resp.status,
                    )
                    return None
                data = await resp.json(content_type=None)
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as e:
            self._warner.warn(
                f"error:{type(e).__name__}",
                "Minecraft profile lookup failed (%s); using fallback avatar",
                type(e).__name__,
            )
            return None
        raw = data.get("id") if isinstance(data, dict) else None
        if not isinstance(raw, str):
            return None
        uuid = raw.replace("-", "").lower()
        return uuid if _UUID_RE.match(uuid) else None

    def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            if self._session_factory is not None:
                self._session = self._session_factory()
            else:
                self._session = aiohttp.ClientSession(
                    headers={"User-Agent": "CraftCord (+https://craftcordbot.com)"}
                )
        return self._session

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()
        self._session = None


class PlayerIdentityService:
    """Send player-identity messages through one owned webhook per channel."""

    def __init__(
        self,
        bot: discord.Client,
        *,
        heads: Optional[MinecraftHeadResolver] = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._bot = bot
        self._clock = clock
        self.heads = heads or MinecraftHeadResolver(clock=clock)
        self._webhooks: Dict[int, discord.Webhook] = {}
        self._locks: Dict[int, asyncio.Lock] = {}
        # channel id -> monotonic time before which setup is not retried
        self._setup_blocked_until: Dict[int, float] = {}
        self._last_recreate: Dict[int, float] = {}
        self._warner = _RateLimitedWarner(clock, _WARN_INTERVAL)

    async def send(
        self,
        channel: discord.TextChannel,
        player: str,
        *,
        route: str,
        content: Optional[str] = None,
        embed: Optional[discord.Embed] = None,
    ) -> DeliveryResult:
        """Post ``content`` / ``embed`` in ``channel`` as ``player``.

        ``channel`` must already be validated for the configured guild.
        ``route`` names the setting (e.g. ``CHAT_CHANNEL_ID``) for logs.
        """
        if not _MC_NAME_RE.match(player) or any(
            part in player.lower() for part in _FORBIDDEN_USERNAME_PARTS
        ):
            self._warner.warn(
                f"{route}:name",
                "%s: player name cannot be used as a webhook sender; "
                "using CraftCord format",
                route,
            )
            return DeliveryResult.FAILED

        try:
            avatar = await self.heads.head_url(player)
        except Exception as e:  # avatar problems must never drop delivery
            self._warner.warn(
                f"avatar:{type(e).__name__}",
                "Avatar lookup error (%s); using fallback avatar",
                type(e).__name__,
            )
            avatar = None
        avatar_url = avatar or self._fallback_avatar()
        kwargs: Dict[str, object] = {
            "username": webhook_username(player),
            "avatar_url": avatar_url,
            "allowed_mentions": _NO_MENTIONS,
            "wait": True,
        }
        if content is not None:
            kwargs["content"] = content
        if embed is not None:
            kwargs["embed"] = embed

        webhook = await self._get_webhook(channel, route)
        if webhook is None:
            return DeliveryResult.FAILED

        result = await self._execute(webhook, kwargs, route)
        if result is not None:
            return result

        # Webhook was deleted. Another task may already have replaced it.
        if self._webhooks.get(channel.id) is webhook:
            del self._webhooks[channel.id]
        replacement = self._webhooks.get(channel.id)
        if replacement is None:
            now = self._clock()
            last = self._last_recreate.get(channel.id)
            if last is not None and now - last < _COOLDOWN_SECONDS:
                self._setup_blocked_until[channel.id] = last + _COOLDOWN_SECONDS
                self._warner.warn(
                    f"{route}:deleted",
                    "%s: CraftCord webhook was deleted again; using CraftCord "
                    "format for up to %d seconds before recreating",
                    route,
                    int(_COOLDOWN_SECONDS),
                )
                return DeliveryResult.FAILED
            self._last_recreate[channel.id] = now
            self._warner.warn(
                f"{route}:recreate",
                "%s: CraftCord webhook was deleted; recreating it once",
                route,
            )
            replacement = await self._get_webhook(channel, route)
            if replacement is None:
                return DeliveryResult.FAILED

        result = await self._execute(replacement, kwargs, route)
        if result is None:
            if self._webhooks.get(channel.id) is replacement:
                del self._webhooks[channel.id]
            self._setup_blocked_until[channel.id] = self._clock() + _COOLDOWN_SECONDS
            return DeliveryResult.FAILED
        return result

    async def _execute(
        self, webhook: discord.Webhook, kwargs: Dict[str, object], route: str
    ) -> Optional[DeliveryResult]:
        """Send once. ``None`` means the webhook no longer exists."""
        try:
            message = await webhook.send(**kwargs)  # type: ignore[arg-type]
        except discord.NotFound:
            return None
        except discord.DiscordServerError as e:
            self._warn_unconfirmed(route, type(e).__name__)
            return DeliveryResult.UNCONFIRMED
        except discord.HTTPException as e:
            self._warner.warn(
                f"{route}:send:{e.status}",
                "%s: webhook delivery rejected (HTTP %s); using CraftCord format",
                route,
                e.status,
            )
            return DeliveryResult.FAILED
        except aiohttp.ClientConnectorError as e:
            # Connection never established, so nothing was posted.
            self._warner.warn(
                f"{route}:connect",
                "%s: webhook connection failed (%s); using CraftCord format",
                route,
                type(e).__name__,
            )
            return DeliveryResult.FAILED
        except (asyncio.TimeoutError, aiohttp.ClientError) as e:
            self._warn_unconfirmed(route, type(e).__name__)
            return DeliveryResult.UNCONFIRMED
        if message is None:
            self._warn_unconfirmed(route, "NoMessage")
            return DeliveryResult.UNCONFIRMED
        return DeliveryResult.CONFIRMED

    def _warn_unconfirmed(self, route: str, reason: str) -> None:
        self._warner.warn(
            f"{route}:unconfirmed:{reason}",
            "%s: webhook delivery unconfirmed (%s); not resending to avoid "
            "a duplicate message",
            route,
            reason,
        )

    async def _get_webhook(
        self, channel: discord.TextChannel, route: str
    ) -> Optional[discord.Webhook]:
        cached = self._webhooks.get(channel.id)
        if cached is not None:
            return cached
        if self._setup_blocked(channel.id):
            return None
        lock = self._locks.setdefault(channel.id, asyncio.Lock())
        async with lock:
            cached = self._webhooks.get(channel.id)
            if cached is not None:
                return cached
            if self._setup_blocked(channel.id):
                return None
            try:
                hooks = await channel.webhooks()
                webhook = next(
                    (h for h in hooks if self._is_owned(h, channel.id)), None
                )
                if webhook is None:
                    webhook = await channel.create_webhook(
                        name=WEBHOOK_NAME,
                        reason="CraftCord player identities",
                    )
            except discord.Forbidden:
                self._block_setup(channel.id)
                self._warner.warn(
                    f"{route}:forbidden",
                    "%s: missing Manage Webhooks permission; using CraftCord "
                    "format. Grant Manage Webhooks in that channel or set the "
                    "identity mode to bot.",
                    route,
                )
                return None
            except (discord.HTTPException, asyncio.TimeoutError, aiohttp.ClientError) as e:
                self._block_setup(channel.id)
                self._warner.warn(
                    f"{route}:setup:{type(e).__name__}",
                    "%s: webhook setup failed (%s); using CraftCord format",
                    route,
                    type(e).__name__,
                )
                return None
            self._webhooks[channel.id] = webhook
            self._setup_blocked_until.pop(channel.id, None)
            return webhook

    def _setup_blocked(self, channel_id: int) -> bool:
        until = self._setup_blocked_until.get(channel_id)
        return until is not None and self._clock() < until

    def _block_setup(self, channel_id: int) -> None:
        self._setup_blocked_until[channel_id] = self._clock() + _COOLDOWN_SECONDS

    def _is_owned(self, webhook: discord.Webhook, channel_id: int) -> bool:
        me = self._bot.user
        return (
            me is not None
            and webhook.type is discord.WebhookType.incoming
            and bool(webhook.token)
            and webhook.name == WEBHOOK_NAME
            and webhook.channel_id == channel_id
            and webhook.user is not None
            and webhook.user.id == me.id
        )

    def _fallback_avatar(self) -> str:
        me = self._bot.user
        if me is not None:
            try:
                return str(me.display_avatar.replace(size=128, static_format="png").url)
            except Exception:
                pass
        return _LAST_RESORT_AVATAR

    async def close(self) -> None:
        await self.heads.close()
