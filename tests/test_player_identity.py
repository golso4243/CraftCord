"""Player-identity delivery: modes, webhook reuse/ownership, fallback, avatars."""
from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import aiohttp
import discord
import pytest

from bot.cogs import chat as chat_module
from bot.cogs import console as console_module
from bot.cogs.chat import ChatCog
from bot.cogs.console import ConsoleCog
from bot.services import player_identity as pi
from bot.services.player_identity import (
    DeliveryResult,
    MinecraftHeadResolver,
    PlayerIdentityService,
)
from bot.utils import destinations as dest_module
from bot.utils import permissions as perms
from bot.utils.destinations import reset_destination_diagnostics
from bot.utils.mc_log_parser import (
    AdvancementEvent,
    ChatMessage,
    DeathEvent,
    JoinEvent,
    LeaveEvent,
)

CONFIGURED_GUILD = 111111111111111111
OTHER_GUILD = 222222222222222222
CHAT_CHANNEL = 5001
EVENTS_CHANNEL = 5002
BOT_ID = 9000
OTHER_USER_ID = 9001
FALLBACK_AVATAR = "https://cdn.example/craftcord.png"
STEVE_UUID = "11111111222233334444555555555555"


# ── fakes ───────────────────────────────────────────────────────────
class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class FakeHeads:
    def __init__(self, mapping: dict[str, str] | None = None, error: bool = False):
        self.mapping = mapping or {}
        self.error = error
        self.calls: list[str] = []

    async def head_url(self, player: str):
        self.calls.append(player)
        if self.error:
            raise RuntimeError("boom")
        uuid = self.mapping.get(player)
        return pi.HEAD_URL.format(uuid=uuid) if uuid else None

    async def close(self) -> None:
        pass


def _response(status: int) -> SimpleNamespace:
    return SimpleNamespace(status=status, reason="x")


def make_webhook(
    channel_id: int,
    *,
    owner_id: int = BOT_ID,
    name: str = pi.WEBHOOK_NAME,
    token: str | None = "tok",
    wtype=discord.WebhookType.incoming,
) -> SimpleNamespace:
    return SimpleNamespace(
        id=hash((channel_id, owner_id, name)),
        type=wtype,
        token=token,
        name=name,
        channel_id=channel_id,
        user=SimpleNamespace(id=owner_id),
        send=AsyncMock(return_value=SimpleNamespace(id=1)),
    )


class FakeChannel:
    def __init__(self, channel_id: int, guild_id: int = CONFIGURED_GUILD) -> None:
        self.id = channel_id
        self.guild = SimpleNamespace(id=guild_id)
        self.send = AsyncMock()
        self.existing: list = []
        self.created: list = []
        self.webhooks = AsyncMock(side_effect=self._list)
        self.create_webhook = AsyncMock(side_effect=self._create)

    async def _list(self):
        await asyncio.sleep(0)
        return list(self.existing)

    async def _create(self, *, name: str, reason: str | None = None):
        await asyncio.sleep(0)
        hook = make_webhook(self.id, name=name)
        self.created.append(hook)
        self.existing.append(hook)
        return hook


def make_bot() -> SimpleNamespace:
    user = MagicMock()
    user.id = BOT_ID
    user.display_avatar.replace.return_value.url = FALLBACK_AVATAR
    return SimpleNamespace(user=user)


def make_service(heads=None, clock=None) -> PlayerIdentityService:
    return PlayerIdentityService(
        make_bot(),  # type: ignore[arg-type]
        heads=heads or FakeHeads({"Steve": STEVE_UUID}),  # type: ignore[arg-type]
        clock=clock or Clock(),
    )


@pytest.fixture
def cfg(monkeypatch: pytest.MonkeyPatch):
    reset_destination_diagnostics()
    base = replace(
        console_module.config,
        guild_id=CONFIGURED_GUILD,
        chat_channel_id=CHAT_CHANNEL,
        events_channel_id=EVENTS_CHANNEL,
        enable_console_mirror=False,
        chat_identity_mode="bot",
        events_identity_mode="bot",
        member_role_id=300,
    )

    def apply(**overrides):
        new = replace(base, **overrides)
        for mod in (console_module, chat_module, dest_module, perms):
            monkeypatch.setattr(mod, "config", new)
        return new

    apply()
    return apply


def make_cog(service=None, chat_ch=None, events_ch=None) -> ConsoleCog:
    bot = MagicMock()
    bot.player_identity = service
    cog = ConsoleCog(bot)
    if chat_ch is not None:
        cog._chat_channel, cog._chat_resolved = chat_ch, True
    if events_ch is not None:
        cog._events_channel, cog._events_resolved = events_ch, True
    return cog


def run(coro):
    return asyncio.run(coro)


# ── default / bot mode ──────────────────────────────────────────────
def test_default_modes_are_bot(cfg) -> None:
    from bot.config import load_config

    loaded = load_config()
    assert loaded.chat_identity_mode == "bot"
    assert loaded.events_identity_mode == "bot"


def test_bot_mode_never_touches_webhooks(cfg) -> None:
    service = make_service()
    chat_ch, events_ch = FakeChannel(CHAT_CHANNEL), FakeChannel(EVENTS_CHANNEL)
    cog = make_cog(service, chat_ch, events_ch)
    run(cog._send_chat(ChatMessage(player="Steve", message="hi")))
    run(cog._send_event(JoinEvent(player="Steve")))

    chat_ch.send.assert_awaited_once()
    assert chat_ch.send.await_args.args[0] == "**[Minecraft]** **Steve**: hi"
    events_ch.send.assert_awaited_once()
    for ch in (chat_ch, events_ch):
        ch.webhooks.assert_not_awaited()
        ch.create_webhook.assert_not_awaited()
    assert service.heads.calls == []  # type: ignore[attr-defined]


# ── independent modes ───────────────────────────────────────────────
def test_chat_player_events_bot(cfg) -> None:
    cfg(chat_identity_mode="player")
    service = make_service()
    chat_ch, events_ch = FakeChannel(CHAT_CHANNEL), FakeChannel(EVENTS_CHANNEL)
    cog = make_cog(service, chat_ch, events_ch)
    run(cog._send_chat(ChatMessage(player="Steve", message="hi")))
    run(cog._send_event(JoinEvent(player="Steve")))

    chat_ch.send.assert_not_awaited()
    assert len(chat_ch.created) == 1
    events_ch.send.assert_awaited_once()
    events_ch.webhooks.assert_not_awaited()


def test_events_player_chat_bot(cfg) -> None:
    cfg(events_identity_mode="player")
    service = make_service()
    chat_ch, events_ch = FakeChannel(CHAT_CHANNEL), FakeChannel(EVENTS_CHANNEL)
    cog = make_cog(service, chat_ch, events_ch)
    run(cog._send_chat(ChatMessage(player="Steve", message="hi")))
    run(cog._send_event(JoinEvent(player="Steve")))

    chat_ch.send.assert_awaited_once()
    chat_ch.webhooks.assert_not_awaited()
    events_ch.send.assert_not_awaited()
    assert len(events_ch.created) == 1


# ── player chat / events content ────────────────────────────────────
def test_player_chat_sender_avatar_and_body(cfg) -> None:
    cfg(chat_identity_mode="player")
    chat_ch = FakeChannel(CHAT_CHANNEL)
    cog = make_cog(make_service(), chat_ch)
    run(cog._send_chat(ChatMessage(player="Steve", message="hello @everyone")))

    hook = chat_ch.created[0]
    kwargs = hook.send.await_args.kwargs
    assert kwargs["username"] == "Steve \u2022 Minecraft"
    assert kwargs["avatar_url"] == pi.HEAD_URL.format(uuid=STEVE_UUID)
    assert kwargs["content"] == "hello @everyone"
    assert "[Minecraft]" not in kwargs["content"]
    assert kwargs["wait"] is True
    am = kwargs["allowed_mentions"]
    assert am.everyone is False and am.users is False and am.roles is False
    chat_ch.send.assert_not_awaited()


def test_player_chat_body_respects_discord_limit(cfg) -> None:
    cfg(chat_identity_mode="player")
    chat_ch = FakeChannel(CHAT_CHANNEL)
    cog = make_cog(make_service(), chat_ch)
    run(cog._send_chat(ChatMessage(player="Steve", message="x" * 2500)))
    assert len(chat_ch.created[0].send.await_args.kwargs["content"]) == 2000


@pytest.mark.parametrize(
    "event,player,needle",
    [
        (JoinEvent(player="Alex"), "Alex", "joined the game"),
        (LeaveEvent(player="Alex"), "Alex", "left the game"),
        (DeathEvent(player="Alex", message="Alex was slain by Zombie"), "Alex", "slain"),
        (
            AdvancementEvent(player="Alex", kind="challenge", name="Hero"),
            "Alex",
            "completed the challenge",
        ),
    ],
)
def test_player_events_use_affected_player_and_keep_embed(
    cfg, event, player, needle
) -> None:
    cfg(events_identity_mode="player")
    events_ch = FakeChannel(EVENTS_CHANNEL)
    cog = make_cog(make_service(FakeHeads({"Alex": STEVE_UUID})), None, events_ch)
    run(cog._send_event(event))

    kwargs = events_ch.created[0].send.await_args.kwargs
    assert kwargs["username"] == f"{player} \u2022 Minecraft"
    assert "content" not in kwargs
    embed = kwargs["embed"]
    expected = ConsoleCog._build_event_embed(event)
    assert embed.description == expected.description
    assert embed.color == expected.color
    assert needle in embed.description
    assert kwargs["allowed_mentions"].everyone is False
    events_ch.send.assert_not_awaited()


def test_two_players_get_distinct_identities(cfg) -> None:
    cfg(chat_identity_mode="player")
    heads = FakeHeads({"Steve": STEVE_UUID})
    chat_ch = FakeChannel(CHAT_CHANNEL)
    cog = make_cog(make_service(heads), chat_ch)
    run(cog._send_chat(ChatMessage(player="Steve", message="a")))
    run(cog._send_chat(ChatMessage(player="Offline_Guy", message="b")))

    calls = chat_ch.created[0].send.await_args_list
    assert calls[0].kwargs["username"].startswith("Steve ")
    assert calls[1].kwargs["username"].startswith("Offline_Guy ")
    # Unknown profile gets the explicit fallback, not Steve's head.
    assert calls[1].kwargs["avatar_url"] == FALLBACK_AVATAR


# ── bot-authored paths stay bot-authored ────────────────────────────
def test_unclassified_console_output_never_uses_webhooks(cfg) -> None:
    cfg(
        chat_identity_mode="player",
        events_identity_mode="player",
        enable_console_mirror=True,
        console_channel_id=5003,
    )
    service = make_service()
    service.send = AsyncMock()  # type: ignore[method-assign]
    cog = make_cog(service)
    run(cog._dispatch("[12:00:00] [Server thread/INFO]: Steve is a great player"))
    run(cog.note_broadcast("Server restarting"))
    service.send.assert_not_awaited()
    assert len(cog._console_buffer) == 2


def test_commands_and_status_do_not_reference_player_identity() -> None:
    cogs = Path(__file__).resolve().parents[1] / "bot" / "cogs"
    for name in ("rcon.py", "status.py", "admin.py", "chat.py"):
        text = (cogs / name).read_text(encoding="utf-8")
        assert "player_identity" not in text
        assert "webhook.send" not in text


# ── webhook reuse, concurrency, ownership ───────────────────────────
def test_webhook_reused_and_shared_between_chat_and_events(cfg) -> None:
    cfg(
        chat_identity_mode="player",
        events_identity_mode="player",
        events_channel_id=CHAT_CHANNEL,
    )
    shared = FakeChannel(CHAT_CHANNEL)
    cog = make_cog(make_service(), shared, shared)
    run(cog._send_chat(ChatMessage(player="Steve", message="a")))
    run(cog._send_event(JoinEvent(player="Steve")))
    run(cog._send_chat(ChatMessage(player="Steve", message="b")))

    assert len(shared.created) == 1
    shared.webhooks.assert_awaited_once()
    assert shared.created[0].send.await_count == 3


def test_separate_channels_get_separate_webhooks() -> None:
    service = make_service()
    a, b = FakeChannel(1), FakeChannel(2)
    run(service.send(a, "Steve", route="A", content="x"))
    run(service.send(b, "Steve", route="B", content="x"))
    assert len(a.created) == 1 and len(b.created) == 1


def test_concurrent_first_sends_create_one_webhook() -> None:
    service = make_service()
    ch = FakeChannel(1)

    async def go():
        return await asyncio.gather(
            *(service.send(ch, "Steve", route="R", content=str(i)) for i in range(5))
        )

    results = run(go())
    assert results == [DeliveryResult.CONFIRMED] * 5
    assert ch.create_webhook.await_count == 1
    assert ch.webhooks.await_count == 1


def test_existing_owned_webhook_adopted() -> None:
    service = make_service()
    ch = FakeChannel(1)
    owned = make_webhook(1)
    ch.existing = [owned]
    assert run(service.send(ch, "Steve", route="R", content="x")) is DeliveryResult.CONFIRMED
    ch.create_webhook.assert_not_awaited()
    owned.send.assert_awaited_once()


@pytest.mark.parametrize(
    "kwargs",
    [
        {"owner_id": OTHER_USER_ID},
        {"name": "Something Else"},
        {"token": None},
        {"wtype": discord.WebhookType.channel_follower},
    ],
)
def test_unrelated_webhooks_not_adopted(kwargs) -> None:
    service = make_service()
    ch = FakeChannel(1)
    foreign = make_webhook(1, **kwargs)
    ch.existing = [foreign]
    run(service.send(ch, "Steve", route="R", content="x"))
    foreign.send.assert_not_awaited()
    ch.create_webhook.assert_awaited_once()


# ── fallback and recovery ───────────────────────────────────────────
def test_missing_manage_webhooks_falls_back_and_backs_off(cfg) -> None:
    cfg(chat_identity_mode="player")
    clock = Clock()
    chat_ch = FakeChannel(CHAT_CHANNEL)
    chat_ch.webhooks = AsyncMock(
        side_effect=discord.Forbidden(_response(403), "Missing Permissions")
    )
    cog = make_cog(make_service(clock=clock), chat_ch)
    run(cog._send_chat(ChatMessage(player="Steve", message="hi @here")))
    run(cog._send_chat(ChatMessage(player="Steve", message="again")))

    assert chat_ch.send.await_count == 2
    first = chat_ch.send.await_args_list[0]
    assert first.args[0] == "**[Minecraft]** **Steve**: hi @here"
    assert first.kwargs["allowed_mentions"].everyone is False
    # Setup failure is cached; no webhook listing on every line.
    assert chat_ch.webhooks.await_count == 1

    clock.now += 61
    run(cog._send_chat(ChatMessage(player="Steve", message="later")))
    assert chat_ch.webhooks.await_count == 2


@pytest.mark.parametrize("status", [400, 403])
def test_confirmed_webhook_rejection_falls_back_event_embed(cfg, status) -> None:
    cfg(events_identity_mode="player")
    events_ch = FakeChannel(EVENTS_CHANNEL)
    hook = make_webhook(EVENTS_CHANNEL)
    exc_type = discord.Forbidden if status == 403 else discord.HTTPException
    hook.send = AsyncMock(side_effect=exc_type(_response(status), "no"))
    events_ch.existing = [hook]
    cog = make_cog(make_service(), None, events_ch)
    event = DeathEvent(player="Steve", message="Steve drowned")
    run(cog._send_event(event))

    events_ch.send.assert_awaited_once()
    kwargs = events_ch.send.await_args.kwargs
    assert kwargs["embed"].description == ConsoleCog._build_event_embed(event).description
    assert kwargs["allowed_mentions"].everyone is False


@pytest.mark.parametrize(
    "exc",
    [
        asyncio.TimeoutError(),
        aiohttp.ServerDisconnectedError(),
        discord.DiscordServerError(_response(503), "down"),
    ],
)
def test_ambiguous_failure_is_not_resent(cfg, exc) -> None:
    cfg(chat_identity_mode="player")
    chat_ch = FakeChannel(CHAT_CHANNEL)
    hook = make_webhook(CHAT_CHANNEL)
    hook.send = AsyncMock(side_effect=exc)
    chat_ch.existing = [hook]
    cog = make_cog(make_service(), chat_ch)
    run(cog._send_chat(ChatMessage(player="Steve", message="hi")))

    hook.send.assert_awaited_once()
    chat_ch.send.assert_not_awaited()


def test_deleted_webhook_recovery_is_bounded(cfg) -> None:
    cfg(chat_identity_mode="player")
    clock = Clock()
    chat_ch = FakeChannel(CHAT_CHANNEL)
    service = make_service(clock=clock)
    cog = make_cog(service, chat_ch)

    run(cog._send_chat(ChatMessage(player="Steve", message="1")))
    first = chat_ch.created[0]

    # Someone deletes the webhook: the next send recreates exactly once.
    first.send = AsyncMock(side_effect=discord.NotFound(_response(404), "Unknown Webhook"))
    chat_ch.existing = []
    run(cog._send_chat(ChatMessage(player="Steve", message="2")))
    assert chat_ch.create_webhook.await_count == 2
    second = chat_ch.created[1]
    assert second.send.await_args.kwargs["content"] == "2"
    chat_ch.send.assert_not_awaited()

    # Deleted again within the cooldown: fall back, no further creation.
    second.send = AsyncMock(side_effect=discord.NotFound(_response(404), "Unknown Webhook"))
    chat_ch.existing = []
    for i in range(3):
        run(cog._send_chat(ChatMessage(player="Steve", message=f"x{i}")))
    assert chat_ch.create_webhook.await_count == 2
    assert chat_ch.send.await_count == 3
    assert chat_ch.send.await_args_list[0].args[0] == "**[Minecraft]** **Steve**: x0"

    # After the cooldown, recovery is allowed again.
    clock.now += 61
    run(cog._send_chat(ChatMessage(player="Steve", message="later")))
    assert chat_ch.create_webhook.await_count == 3


def test_webhook_forbidden_name_falls_back_without_setup(cfg) -> None:
    cfg(chat_identity_mode="player")
    chat_ch = FakeChannel(CHAT_CHANNEL)
    cog = make_cog(make_service(), chat_ch)
    run(cog._send_chat(ChatMessage(player="DiscordFan", message="hi")))
    chat_ch.webhooks.assert_not_awaited()
    chat_ch.send.assert_awaited_once()


# ── destinations ────────────────────────────────────────────────────
def test_wrong_guild_destination_never_touches_webhooks(cfg) -> None:
    cfg(chat_identity_mode="player", events_identity_mode="player")
    foreign_chat = MagicMock(spec=discord.TextChannel)
    foreign_chat.id = CHAT_CHANNEL
    foreign_chat.guild = SimpleNamespace(id=OTHER_GUILD)
    foreign_chat.send = AsyncMock()
    foreign_chat.webhooks = AsyncMock()
    bot = MagicMock()
    bot.get_channel = MagicMock(return_value=foreign_chat)
    service = make_service()
    service.send = AsyncMock()  # type: ignore[method-assign]
    bot.player_identity = service
    cog = ConsoleCog(bot)
    run(cog._send_chat(ChatMessage(player="Steve", message="hi")))
    run(cog._send_event(JoinEvent(player="Steve")))
    service.send.assert_not_awaited()
    foreign_chat.webhooks.assert_not_awaited()
    foreign_chat.send.assert_not_awaited()


def test_unset_events_channel_drops_player_event(cfg) -> None:
    cfg(events_identity_mode="player", events_channel_id=None)
    service = make_service()
    service.send = AsyncMock()  # type: ignore[method-assign]
    cog = make_cog(service)
    run(cog._dispatch("[12:00:00] [Server thread/INFO]: Steve joined the game"))
    service.send.assert_not_awaited()


# ── relay-loop safety ───────────────────────────────────────────────
def test_player_webhook_messages_do_not_loop_into_minecraft(cfg) -> None:
    cfg(chat_identity_mode="player")
    guild = SimpleNamespace(id=CONFIGURED_GUILD, owner_id=1, get_role=lambda _i: None)
    channel = SimpleNamespace(id=CHAT_CHANNEL, guild=guild)
    author = SimpleNamespace(
        id=BOT_ID,
        bot=False,
        display_name="Steve \u2022 Minecraft",
        guild_permissions=SimpleNamespace(administrator=True),
        roles=[SimpleNamespace(id=300)],
    )
    message = SimpleNamespace(
        guild=guild,
        channel=channel,
        author=author,
        webhook_id=12345,
        clean_content="hi",
        attachments=[],
        stickers=[],
        add_reaction=AsyncMock(),
    )
    rcon = MagicMock()
    rcon.command = AsyncMock(return_value="")
    run(ChatCog(MagicMock(), rcon).on_message(message))  # type: ignore[arg-type]
    rcon.command.assert_not_awaited()


# ── avatars ─────────────────────────────────────────────────────────
class FakeResp:
    def __init__(self, status: int, payload=None) -> None:
        self.status = status
        self._payload = payload

    async def json(self, content_type=None):
        return self._payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class FakeSession:
    def __init__(self, responder) -> None:
        self.responder = responder
        self.urls: list[str] = []
        self.closed = False

    def get(self, url, timeout=None):
        self.urls.append(url)
        assert timeout is not None and timeout.total == 5.0
        return self.responder(url)

    async def close(self) -> None:
        self.closed = True


def make_resolver(responder, clock=None):
    session = FakeSession(responder)
    resolver = MinecraftHeadResolver(
        session_factory=lambda: session,  # type: ignore[return-value]
        clock=clock or Clock(),
    )
    return resolver, session


def test_successful_lookup_is_cached() -> None:
    clock = Clock()
    resolver, session = make_resolver(
        lambda _u: FakeResp(200, {"id": STEVE_UUID.upper(), "name": "Steve"}), clock
    )
    url1 = run(resolver.head_url("Steve"))
    url2 = run(resolver.head_url("steve"))
    assert url1 == url2 == f"https://minotar.net/helm/{STEVE_UUID}/128.png"
    assert session.urls == [
        "https://api.minecraftservices.com/minecraft/profile/lookup/name/Steve"
    ]
    clock.now += 3601
    run(resolver.head_url("Steve"))
    assert len(session.urls) == 2


def test_failed_lookup_is_briefly_cached() -> None:
    clock = Clock()
    resolver, session = make_resolver(lambda _u: FakeResp(404), clock)
    assert run(resolver.head_url("Nobody")) is None
    assert run(resolver.head_url("Nobody")) is None
    assert len(session.urls) == 1
    clock.now += 601
    run(resolver.head_url("Nobody"))
    assert len(session.urls) == 2


def test_network_error_and_bad_payload_return_none() -> None:
    def boom(_u):
        raise aiohttp.ClientConnectionError()

    resolver, _ = make_resolver(boom)
    assert run(resolver.head_url("Steve")) is None
    resolver, _ = make_resolver(lambda _u: FakeResp(200, {"id": "not-a-uuid"}))
    assert run(resolver.head_url("Steve")) is None
    resolver, _ = make_resolver(lambda _u: FakeResp(503))
    assert run(resolver.head_url("Steve")) is None


def test_invalid_username_makes_no_request() -> None:
    resolver, session = make_resolver(lambda _u: FakeResp(200, {"id": STEVE_UUID}))
    for bad in ("", "has space", "a" * 17, "../x", "name?q=1"):
        assert run(resolver.head_url(bad)) is None
    assert session.urls == []


def test_cache_is_bounded() -> None:
    session = FakeSession(lambda _u: FakeResp(404))
    resolver = MinecraftHeadResolver(
        session_factory=lambda: session,  # type: ignore[return-value]
        max_entries=3,
    )
    for name in ("a", "b", "c", "d"):
        run(resolver.head_url(name))
    assert len(resolver._cache) == 3
    assert "a" not in resolver._cache


def test_concurrent_lookups_share_one_request() -> None:
    async def go():
        resolver, session = make_resolver(
            lambda _u: FakeResp(200, {"id": STEVE_UUID})
        )
        urls = await asyncio.gather(*(resolver.head_url("Steve") for _ in range(4)))
        return urls, session

    urls, session = run(go())
    assert len(set(urls)) == 1 and len(session.urls) == 1


def test_avatar_failure_does_not_drop_message() -> None:
    service = make_service(FakeHeads(error=True))
    ch = FakeChannel(1)
    assert run(service.send(ch, "Steve", route="R", content="x")) is DeliveryResult.CONFIRMED
    assert ch.created[0].send.await_args.kwargs["avatar_url"] == FALLBACK_AVATAR


def test_close_closes_lookup_session() -> None:
    resolver, session = make_resolver(lambda _u: FakeResp(404))
    service = PlayerIdentityService(make_bot(), heads=resolver)  # type: ignore[arg-type]
    run(resolver.head_url("Steve"))
    run(service.close())
    assert session.closed is True
