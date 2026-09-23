"""Phase 2 product identity and vanilla-baseline regressions."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from bot.bot import CraftCordBot
from bot.cogs.admin import AdminCog
from bot.cogs import console as console_module
from bot.cogs.console import ConsoleCog
from bot.cogs.rcon import RconCog
from bot.config import load_config
from bot.utils.mc_log_parser import parse_line


CONFIGURED_GUILD = 111111111111111111
_PREFIX = "[12:00:00] [Server thread/INFO]: "


class FakeGuild:
    def __init__(self, guild_id: int, owner_id: int = 1) -> None:
        self.id = guild_id
        self.owner_id = owner_id


class FakeMember:
    def __init__(self, guild: FakeGuild, *, administrator: bool = True) -> None:
        self.id = 7
        self.guild = guild
        self.roles = []
        self.guild_permissions = SimpleNamespace(administrator=administrator)


class FakeInteraction:
    def __init__(self, guild: FakeGuild, user: FakeMember) -> None:
        self.guild = guild
        self.user = user
        self.response = SimpleNamespace(
            defer=AsyncMock(),
            send_message=AsyncMock(),
            is_done=lambda: False,
        )
        self.followup = SimpleNamespace(send=AsyncMock())


def test_craftcord_bot_initializes_with_single_rcon() -> None:
    bot = CraftCordBot()
    assert hasattr(bot, "rcon")
    assert not hasattr(bot, "rcon_creative")
    assert not hasattr(bot, "rcon_targets")


def test_deleted_integration_settings_are_not_required(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Ensure removed settings are absent; load_config must still succeed.
    for name in (
        "STAFF_ALERT_CHANNEL_ID",
        "STAFF_ALERT_ROLE_ID",
        "CREATIVE_RCON_HOST",
        "CREATIVE_RCON_PORT",
        "CREATIVE_RCON_PASSWORD",
        "TPS_COMMAND",
    ):
        monkeypatch.delenv(name, raising=False)
    cfg = load_config()
    assert not hasattr(cfg, "staff_alert_channel_id")
    assert not hasattr(cfg, "creative_rcon_host")
    assert not hasattr(cfg, "tps_command")
    assert cfg.guild_id == CONFIGURED_GUILD


def test_whitelist_add_invokes_primary_rcon_once() -> None:
    rcon = MagicMock()
    rcon.command = AsyncMock(return_value="Added Steve to the whitelist")
    cog = RconCog(MagicMock(), rcon)

    guild = FakeGuild(CONFIGURED_GUILD)
    member = FakeMember(guild)
    interaction = FakeInteraction(guild, member)

    asyncio.run(cog.whitelist_add.callback(cog, interaction, "Steve"))  # type: ignore[arg-type]

    rcon.command.assert_awaited_once_with("whitelist add Steve")
    interaction.followup.send.assert_awaited_once()


def test_whitelist_remove_invokes_primary_rcon_once() -> None:
    rcon = MagicMock()
    rcon.command = AsyncMock(return_value="Removed Steve from the whitelist")
    cog = RconCog(MagicMock(), rcon)

    guild = FakeGuild(CONFIGURED_GUILD)
    member = FakeMember(guild)
    interaction = FakeInteraction(guild, member)

    asyncio.run(cog.whitelist_remove.callback(cog, interaction, "Steve"))  # type: ignore[arg-type]

    rcon.command.assert_awaited_once_with("whitelist remove Steve")
    interaction.followup.send.assert_awaited_once()


def test_staff_alert_text_is_generic_console_when_mirror_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from dataclasses import replace

    cfg = replace(
        console_module.config,
        enable_console_mirror=True,
        console_channel_id=4002,
    )
    monkeypatch.setattr(console_module, "config", cfg)
    cog = ConsoleCog(MagicMock())
    line = _PREFIX + '[STAFF_ALERT] sender="Steve" message="help needed"'
    assert parse_line(line) is None
    asyncio.run(cog._dispatch(line))
    assert cog._console_buffer == [line]


def test_staff_alert_text_dropped_when_mirror_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from dataclasses import replace

    cfg = replace(
        console_module.config,
        enable_console_mirror=False,
        console_channel_id=4002,
    )
    monkeypatch.setattr(console_module, "config", cfg)
    cog = ConsoleCog(MagicMock())
    line = _PREFIX + '[STAFF_ALERT] sender="Steve" message="help needed"'
    asyncio.run(cog._dispatch(line))
    assert cog._console_buffer == []


def test_botinfo_identifies_craftcord() -> None:
    bot = MagicMock()
    bot.start_time = 0
    cog = AdminCog(bot)
    guild = FakeGuild(CONFIGURED_GUILD)
    member = FakeMember(guild)
    interaction = FakeInteraction(guild, member)

    asyncio.run(cog.botinfo.callback(cog, interaction))  # type: ignore[arg-type]

    embed = interaction.response.send_message.await_args.kwargs["embed"]
    assert embed.title == "CraftCord"
    assert embed.description == "Your server. Your Discord. Connected."
    assert embed.footer.text == "Built by SwornHero"
