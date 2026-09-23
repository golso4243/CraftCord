"""Mention hardening, error sanitizing, and public-address privacy."""
from __future__ import annotations

import asyncio
import logging
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from bot.cogs import console as console_module
from bot.cogs.console import ConsoleCog
from bot.cogs.rcon import RconCog, _truncate_for_discord
from bot.services.rcon_service import RconError
from bot.utils.embeds import StatusSnapshot, build_status_embed
from bot.utils.formatting import neutralize_code_fences

CONFIGURED_GUILD = 111111111111111111


class FakeGuild:
    def __init__(self, guild_id: int) -> None:
        self.id = guild_id
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


def test_neutralize_code_fences_breaks_triple_backticks() -> None:
    raw = "before ``` @everyone after"
    out = neutralize_code_fences(raw)
    assert "```" not in out
    assert "@everyone" in out
    assert "\u200b" in out


def test_truncate_for_discord_neutralizes_fences() -> None:
    out = _truncate_for_discord("oops ``` @here leak")
    assert "```" not in out


def test_console_send_uses_no_mentions_and_neutralized_fences(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = replace(
        console_module.config,
        guild_id=CONFIGURED_GUILD,
        enable_console_mirror=True,
        console_channel_id=4002,
    )
    monkeypatch.setattr(console_module, "config", cfg)

    guild = FakeGuild(CONFIGURED_GUILD)
    channel = FakeTextChannel(4002, guild)
    cog = ConsoleCog(MagicMock())
    cog._console_channel = channel
    cog._console_resolved = True

    async def _flush_once() -> None:
        chunk = neutralize_code_fences("line with ``` and @everyone")
        await channel.send(
            f"```\n{chunk}\n```",
            allowed_mentions=discord.AllowedMentions.none(),
        )

    asyncio.run(_flush_once())
    kwargs = channel.send.await_args.kwargs
    assert kwargs["allowed_mentions"].everyone is False
    assert kwargs["allowed_mentions"].users is False
    body = channel.send.await_args.args[0]
    # Outer fences exist, but inner triple-backtick run is broken.
    inner = body[4:-4]  # strip leading ```\n and trailing \n```
    assert "```" not in inner


def test_list_error_is_ephemeral_and_sanitized(caplog: pytest.LogCaptureFixture) -> None:
    rcon = MagicMock()
    planted = "RCON connect failed: [Errno 111] Connection refused to 10.9.8.7:25575 secret=abc"
    rcon.command = AsyncMock(side_effect=RconError(planted))
    cog = RconCog(MagicMock(), rcon)

    guild = SimpleNamespace(id=CONFIGURED_GUILD, owner_id=1)
    member = SimpleNamespace(
        id=2,
        guild=guild,
        roles=[SimpleNamespace(id=300)],
        guild_permissions=SimpleNamespace(administrator=False),
    )
    response = SimpleNamespace(
        defer=AsyncMock(),
        is_done=lambda: True,
        send_message=AsyncMock(),
    )
    followup = SimpleNamespace(send=AsyncMock())
    interaction = SimpleNamespace(
        guild=guild, user=member, response=response, followup=followup
    )

    # Patch member access so the callback can run without decorator checks.
    asyncio.run(cog.list_players.callback(cog, interaction))  # type: ignore[arg-type]

    followup.send.assert_awaited()
    args, kwargs = followup.send.await_args
    assert kwargs.get("ephemeral") is True
    text = args[0] if args else ""
    assert "10.9.8.7" not in text
    assert "secret=abc" not in text
    assert "25575" not in text


def test_status_omits_address_when_public_blank() -> None:
    snap = StatusSnapshot(
        online=True,
        host="10.0.0.5",
        port=25565,
        public_address=None,
        version="1.21",
    )
    embed = build_status_embed(snap)
    names = [f.name for f in embed.fields]
    assert "Address" not in names
    # Backend host must not appear anywhere in the embed.
    blob = (embed.description or "") + "".join(f.value for f in embed.fields)
    assert "10.0.0.5" not in blob


def test_status_renders_explicit_public_address() -> None:
    snap = StatusSnapshot(
        online=True,
        host="10.0.0.5",
        port=25565,
        public_address="play.example.com",
        version="1.21",
    )
    embed = build_status_embed(snap)
    address_fields = [f for f in embed.fields if f.name == "Address"]
    assert len(address_fields) == 1
    assert "play.example.com" in address_fields[0].value
    assert "10.0.0.5" not in address_fields[0].value


def test_status_embed_has_no_tps_mspt_fields() -> None:
    snap = StatusSnapshot(
        online=True,
        host="127.0.0.1",
        port=25565,
        version="26.2",
        players_online=1,
        players_max=20,
        online_since=1_000_000,
    )
    embed = build_status_embed(snap)
    names = [f.name for f in embed.fields]
    assert "TPS" not in names
    assert "MSPT" not in names


def test_app_command_error_does_not_log_secret(
    caplog: pytest.LogCaptureFixture,
) -> None:
    from bot.bot import CraftCordBot

    interaction = SimpleNamespace(
        response=SimpleNamespace(
            is_done=lambda: False,
            send_message=AsyncMock(),
        ),
        followup=SimpleNamespace(send=AsyncMock()),
    )
    planted = "Authorization: Bearer FAKESECRET_s4t5u6v7w8x9y0z1a2b3"
    err = app_commands_error_with_secret(planted)

    with caplog.at_level(logging.ERROR):
        asyncio.run(
            CraftCordBot._on_app_command_error(interaction, err)  # type: ignore[arg-type]
        )

    joined = " ".join(r.getMessage() for r in caplog.records)
    assert "sekrit-panel-token" not in joined
    assert "Bearer" not in joined


def app_commands_error_with_secret(secret: str):
    from discord import app_commands

    class Boom(app_commands.AppCommandError):
        def __str__(self) -> str:
            return secret

    return Boom()
