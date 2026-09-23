"""Authorization, sync destination, and raw /rcon absence."""
from __future__ import annotations

import asyncio
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest
from discord import app_commands

from bot.bot import GuildBoundCommandTree, CraftCordBot
from bot.cogs.admin import AdminCog
from bot.cogs.rcon import RconCog
from bot.utils import permissions as perms
from bot.utils.permissions import (
    GUILD_DENIED_MSG,
    require_configured_guild,
)

CONFIGURED_GUILD = 111111111111111111
OTHER_GUILD = 222222222222222222


class FakeGuild:
    def __init__(self, guild_id: int, owner_id: int = 1) -> None:
        self.id = guild_id
        self.owner_id = owner_id


class FakeMember:
    def __init__(
        self,
        *,
        guild: FakeGuild,
        member_id: int = 10,
        administrator: bool = False,
        roles: list | None = None,
    ) -> None:
        self.id = member_id
        self.guild = guild
        self.roles = roles or []
        self.guild_permissions = SimpleNamespace(administrator=administrator)


class FakeResponse:
    def __init__(self) -> None:
        self._done = False
        self.send_message = AsyncMock()

    def is_done(self) -> bool:
        return self._done

    async def defer(self, **kwargs) -> None:
        self._done = True


class FakeInteraction:
    def __init__(self, *, guild: FakeGuild | None, user) -> None:
        self.guild = guild
        self.user = user
        self.response = FakeResponse()
        self.followup = SimpleNamespace(send=AsyncMock())


@pytest.fixture
def guild_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        perms,
        "config",
        replace(perms.config, guild_id=CONFIGURED_GUILD, member_role_id=300),
    )
    import bot.cogs.admin as admin_module

    monkeypatch.setattr(
        admin_module,
        "config",
        replace(admin_module.config, guild_id=CONFIGURED_GUILD),
    )


def test_require_configured_guild_denies_dm(guild_config: None) -> None:
    interaction = FakeInteraction(guild=None, user=SimpleNamespace(id=1))
    with pytest.raises(app_commands.CheckFailure) as exc_info:
        require_configured_guild(interaction)  # type: ignore[arg-type]
    assert str(exc_info.value) == GUILD_DENIED_MSG
    assert str(CONFIGURED_GUILD) not in str(exc_info.value)


def test_require_configured_guild_denies_other_guild(guild_config: None) -> None:
    guild = FakeGuild(OTHER_GUILD)
    interaction = FakeInteraction(
        guild=guild, user=FakeMember(guild=guild, administrator=True)
    )
    with pytest.raises(app_commands.CheckFailure):
        require_configured_guild(interaction)  # type: ignore[arg-type]


def test_require_configured_guild_allows_configured(guild_config: None) -> None:
    guild = FakeGuild(CONFIGURED_GUILD)
    interaction = FakeInteraction(
        guild=guild, user=FakeMember(guild=guild)
    )
    assert require_configured_guild(interaction) is True  # type: ignore[arg-type]


def test_list_wrong_guild_never_calls_rcon(guild_config: None) -> None:
    rcon = MagicMock()
    rcon.command = AsyncMock(return_value="There are 0 players")
    cog = RconCog(MagicMock(), rcon)

    guild = FakeGuild(OTHER_GUILD)
    member = FakeMember(guild=guild, administrator=True)
    interaction = FakeInteraction(guild=guild, user=member)

    checks = list(cog.list_players.checks)
    assert checks, "expected require_member check on /list"

    async def _run() -> None:
        for check in checks:
            await check(interaction)  # type: ignore[arg-type]

    with pytest.raises(app_commands.CheckFailure):
        asyncio.run(_run())
    rcon.command.assert_not_awaited()


def test_list_authorized_member_calls_rcon(guild_config: None) -> None:
    rcon = MagicMock()
    rcon.command = AsyncMock(return_value="There are 0 of a max of 20 players online:")
    cog = RconCog(MagicMock(), rcon)

    guild = FakeGuild(CONFIGURED_GUILD)
    role = SimpleNamespace(id=300)
    member = FakeMember(guild=guild, roles=[role])
    interaction = FakeInteraction(guild=guild, user=member)

    async def _run() -> None:
        for check in cog.list_players.checks:
            assert await check(interaction) is True  # type: ignore[arg-type]
        await cog.list_players.callback(cog, interaction)  # type: ignore[arg-type]

    asyncio.run(_run())
    rcon.command.assert_awaited_once_with("list", replay_if_uncertain=True)


def test_sync_targets_configured_guild_not_interaction_guild(
    guild_config: None,
) -> None:
    bot = MagicMock()
    bot.tree.sync = AsyncMock(return_value=[1, 2, 3])
    cog = AdminCog(bot)

    guild = FakeGuild(CONFIGURED_GUILD)
    member = FakeMember(guild=guild, administrator=True)
    interaction = FakeInteraction(guild=guild, user=member)

    async def _run() -> None:
        for check in cog.sync.checks:
            assert await check(interaction) is True  # type: ignore[arg-type]
        await cog.sync.callback(cog, interaction)  # type: ignore[arg-type]

    asyncio.run(_run())
    bot.tree.sync.assert_awaited_once()
    kwargs = bot.tree.sync.await_args.kwargs
    synced_guild = kwargs.get("guild") or bot.tree.sync.await_args.args[0]
    assert isinstance(synced_guild, discord.Object)
    assert synced_guild.id == CONFIGURED_GUILD


def test_raw_rcon_absent_from_command_tree() -> None:
    """Load RconCog onto a minimal bot and confirm /rcon is gone."""
    bot = MagicMock()
    bot.rcon = MagicMock()
    # Instantiate cog the same way setup() would.
    cog = RconCog(bot, bot.rcon)
    names = {cmd.name for cmd in cog.get_app_commands()}
    assert "rcon" not in names
    assert "list" in names
    assert "whitelist" in names
    assert "say" in names
    assert "kick" in names
    assert "ban" in names
    assert "pardon" in names


def test_guild_bound_tree_class_is_wired() -> None:
    # CraftCordBot.__init__ needs a valid config singleton (conftest).
    # Construction opens no network sockets.
    bot = CraftCordBot()
    assert isinstance(bot.tree, GuildBoundCommandTree)
    assert bot.allowed_mentions.everyone is False
    assert bot.allowed_mentions.users is False
    assert bot.allowed_mentions.roles is False
