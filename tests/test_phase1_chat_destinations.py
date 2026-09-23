"""Chat bridge gating and outbound destination guild checks."""
from __future__ import annotations

import asyncio
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from bot.cogs import chat as chat_module
from bot.cogs import console as console_module
from bot.cogs.chat import ChatCog
from bot.cogs.console import ConsoleCog
from bot.utils import destinations as dest_module
from bot.utils import permissions as perms
from bot.utils.destinations import reset_destination_diagnostics, resolve_guild_text_channel
from bot.utils.mc_log_parser import ChatMessage, JoinEvent

CONFIGURED_GUILD = 111111111111111111
OTHER_GUILD = 222222222222222222
CHAT_CHANNEL = 4001
CONSOLE_CHANNEL = 4002
EVENTS_CHANNEL = 4003


class FakeGuild:
    def __init__(self, guild_id: int, owner_id: int = 1) -> None:
        self.id = guild_id
        self.owner_id = owner_id
        self._roles: dict[int, SimpleNamespace] = {}

    def get_role(self, role_id: int):
        return self._roles.get(role_id)

    def add_role(self, role_id: int) -> SimpleNamespace:
        role = SimpleNamespace(id=role_id)
        self._roles[role_id] = role
        return role


class FakeTextChannel:
    def __init__(self, channel_id: int, guild: FakeGuild) -> None:
        self.id = channel_id
        self.guild = guild
        self.send = AsyncMock()


class FakeMember:
    def __init__(
        self,
        guild: FakeGuild,
        *,
        bot: bool = False,
        roles: list | None = None,
        administrator: bool = False,
        member_id: int = 7,
    ) -> None:
        self.id = member_id
        self.guild = guild
        self.bot = bot
        self.roles = roles or []
        self.display_name = "Tester"
        self.guild_permissions = SimpleNamespace(administrator=administrator)


class FakeMessage:
    def __init__(
        self,
        *,
        guild: FakeGuild | None,
        channel: FakeTextChannel,
        author,
        content: str = "hello",
        webhook_id=None,
    ) -> None:
        self.guild = guild
        self.channel = channel
        self.author = author
        self.content = content
        self.clean_content = content
        self.webhook_id = webhook_id
        self.attachments = []
        self.stickers = []
        self.add_reaction = AsyncMock()


@pytest.fixture
def configured(monkeypatch: pytest.MonkeyPatch):
    reset_destination_diagnostics()
    cfg = replace(
        chat_module.config,
        guild_id=CONFIGURED_GUILD,
        chat_channel_id=CHAT_CHANNEL,
        member_role_id=300,
        enable_console_mirror=False,
        console_channel_id=CONSOLE_CHANNEL,
        events_channel_id=EVENTS_CHANNEL,
    )
    monkeypatch.setattr(chat_module, "config", cfg)
    monkeypatch.setattr(console_module, "config", cfg)
    monkeypatch.setattr(dest_module, "config", cfg)
    monkeypatch.setattr(perms, "config", cfg)
    return cfg


def _authorized_message(configured) -> tuple[ChatCog, MagicMock, FakeMessage]:
    guild = FakeGuild(CONFIGURED_GUILD)
    channel = FakeTextChannel(CHAT_CHANNEL, guild)
    author = FakeMember(guild, roles=[SimpleNamespace(id=300)])
    message = FakeMessage(guild=guild, channel=channel, author=author, content="hi")
    rcon = MagicMock()
    rcon.command = AsyncMock(return_value="")
    cog = ChatCog(MagicMock(), rcon)
    return cog, rcon, message


def test_authorized_chat_reaches_rcon(configured) -> None:
    cog, rcon, message = _authorized_message(configured)
    asyncio.run(cog.on_message(message))  # type: ignore[arg-type]
    rcon.command.assert_awaited()
    cmd = rcon.command.await_args.args[0]
    assert cmd.startswith("tellraw @a ")
    assert "hoverEvent" not in cmd
    assert "hover_event" not in cmd
    assert '{text:"[Discord] "' in cmd or '{text:"[Discord] ",color:"blue"}' in cmd
    message.add_reaction.assert_not_awaited()


def test_tellraw_transport_failure_warns_without_retry(configured) -> None:
    from bot.services.rcon_service import RconError

    cog, rcon, message = _authorized_message(configured)
    rcon.command = AsyncMock(side_effect=RconError("simulated"))
    asyncio.run(cog.on_message(message))  # type: ignore[arg-type]
    rcon.command.assert_awaited_once()
    message.add_reaction.assert_awaited_once()


def test_tellraw_command_rejection_warns_without_retry(configured) -> None:
    cog, rcon, message = _authorized_message(configured)
    rcon.command = AsyncMock(return_value="Incorrect argument for command")
    asyncio.run(cog.on_message(message))  # type: ignore[arg-type]
    rcon.command.assert_awaited_once()
    message.add_reaction.assert_awaited_once()


def test_tellraw_no_players_warns_without_retry(configured) -> None:
    cog, rcon, message = _authorized_message(configured)
    rcon.command = AsyncMock(return_value="No player was found")
    asyncio.run(cog.on_message(message))  # type: ignore[arg-type]
    rcon.command.assert_awaited_once()
    message.add_reaction.assert_awaited_once()


def test_tellraw_empty_response_no_warning(configured) -> None:
    cog, rcon, message = _authorized_message(configured)
    rcon.command = AsyncMock(return_value="")
    asyncio.run(cog.on_message(message))  # type: ignore[arg-type]
    rcon.command.assert_awaited_once()
    message.add_reaction.assert_not_awaited()


def test_tellraw_unknown_output_no_warning(configured) -> None:
    cog, rcon, message = _authorized_message(configured)
    rcon.command = AsyncMock(return_value="unexpected chatter")
    asyncio.run(cog.on_message(message))  # type: ignore[arg-type]
    rcon.command.assert_awaited_once()
    message.add_reaction.assert_not_awaited()


def test_wrong_guild_chat_never_reaches_rcon(configured) -> None:
    cog, rcon, message = _authorized_message(configured)
    message.guild = FakeGuild(OTHER_GUILD)
    message.channel = FakeTextChannel(CHAT_CHANNEL, message.guild)
    message.author = FakeMember(
        message.guild, roles=[SimpleNamespace(id=300)], administrator=True
    )
    asyncio.run(cog.on_message(message))  # type: ignore[arg-type]
    rcon.command.assert_not_awaited()


def test_bot_message_ignored(configured) -> None:
    cog, rcon, message = _authorized_message(configured)
    message.author = FakeMember(
        message.guild, bot=True, roles=[SimpleNamespace(id=300)]
    )
    asyncio.run(cog.on_message(message))  # type: ignore[arg-type]
    rcon.command.assert_not_awaited()


def test_webhook_message_ignored(configured) -> None:
    cog, rcon, message = _authorized_message(configured)
    message.webhook_id = 999
    asyncio.run(cog.on_message(message))  # type: ignore[arg-type]
    rcon.command.assert_not_awaited()


def test_wrong_channel_ignored(configured) -> None:
    cog, rcon, message = _authorized_message(configured)
    message.channel = FakeTextChannel(9999, message.guild)
    asyncio.run(cog.on_message(message))  # type: ignore[arg-type]
    rcon.command.assert_not_awaited()


def test_unauthorized_member_ignored(configured) -> None:
    cog, rcon, message = _authorized_message(configured)
    message.author = FakeMember(message.guild, roles=[])
    asyncio.run(cog.on_message(message))  # type: ignore[arg-type]
    rcon.command.assert_not_awaited()


def test_cross_guild_outbound_channel_rejected(configured) -> None:
    other = FakeGuild(OTHER_GUILD)
    channel = FakeTextChannel(CHAT_CHANNEL, other)
    bot = MagicMock()
    bot.get_channel = MagicMock(return_value=channel)
    bot.fetch_channel = AsyncMock()

    result = asyncio.run(
        resolve_guild_text_channel(
            bot, CHAT_CHANNEL, setting_name="CHAT_CHANNEL_ID"
        )
    )
    assert result is None
    bot.fetch_channel.assert_not_awaited()


def test_console_silent_by_default(configured, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        console_module,
        "config",
        replace(configured, enable_console_mirror=False, console_channel_id=CONSOLE_CHANNEL),
    )
    cog = ConsoleCog(MagicMock())
    guild = FakeGuild(CONFIGURED_GUILD)
    console_ch = FakeTextChannel(CONSOLE_CHANNEL, guild)
    cog._console_channel = console_ch
    cog._console_resolved = True

    asyncio.run(cog._dispatch("Some vanilla console line with IP 10.0.0.1"))
    assert cog._console_buffer == []
    console_ch.send.assert_not_awaited()


def test_console_mirror_enabled_buffers(
    configured, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        console_module,
        "config",
        replace(configured, enable_console_mirror=True, console_channel_id=CONSOLE_CHANNEL),
    )
    cog = ConsoleCog(MagicMock())
    asyncio.run(cog._dispatch("Server thread/INFO: Starting minecraft server"))
    assert len(cog._console_buffer) == 1


def test_chat_still_works_with_console_disabled(
    configured, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        console_module,
        "config",
        replace(
            configured,
            enable_console_mirror=False,
            chat_channel_id=CHAT_CHANNEL,
        ),
    )
    guild = FakeGuild(CONFIGURED_GUILD)
    chat_ch = FakeTextChannel(CHAT_CHANNEL, guild)
    cog = ConsoleCog(MagicMock())
    cog._chat_channel = chat_ch
    cog._chat_resolved = True

    asyncio.run(
        cog._send_chat(ChatMessage(player="Steve", message="hello @everyone"))
    )
    chat_ch.send.assert_awaited_once()
    kwargs = chat_ch.send.await_args.kwargs
    assert kwargs["allowed_mentions"].everyone is False


def test_events_work_with_console_disabled(configured) -> None:
    guild = FakeGuild(CONFIGURED_GUILD)
    events_ch = FakeTextChannel(EVENTS_CHANNEL, guild)
    cog = ConsoleCog(MagicMock())
    cog._events_channel = events_ch
    cog._events_resolved = True
    asyncio.run(
        cog._send_event(JoinEvent(player="Steve"))
    )
    events_ch.send.assert_awaited_once()


def test_no_fallback_to_console_when_events_unset(
    configured, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        console_module,
        "config",
        replace(
            configured,
            events_channel_id=None,
            enable_console_mirror=True,
            console_channel_id=CONSOLE_CHANNEL,
        ),
    )
    cog = ConsoleCog(MagicMock())
    # A join event with no events channel should be dropped, not buffered.
    # parse_line path: use _dispatch with vanilla join text.
    asyncio.run(
        cog._dispatch(
            "[12:00:00] [Server thread/INFO]: Steve joined the game"
        )
    )
    assert cog._console_buffer == []
