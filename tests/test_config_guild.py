"""Configuration validation: guild id, booleans, no value echo."""
from __future__ import annotations

import os

import pytest

from bot.config import load_config
from bot.errors import ConfigError


def _base_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CRAFTCORD_SKIP_DOTENV", "1")
    monkeypatch.setenv("DISCORD_TOKEN", "test-token")
    monkeypatch.setenv("RCON_PASSWORD", "test-password")
    monkeypatch.setenv("DISCORD_GUILD_ID", "111111111111111111")
    monkeypatch.setenv("MC_HOST", "127.0.0.1")
    monkeypatch.setenv("RCON_HOST", "127.0.0.1")


@pytest.mark.parametrize(
    "value",
    ["", " ", "not-a-number", "0", "-1", "-999"],
)
def test_invalid_guild_id_fails_without_echoing(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    _base_env(monkeypatch)
    if value.strip() == "":
        monkeypatch.delenv("DISCORD_GUILD_ID", raising=False)
        monkeypatch.setenv("DISCORD_GUILD_ID", value)
    else:
        monkeypatch.setenv("DISCORD_GUILD_ID", value)

    with pytest.raises(ConfigError) as exc_info:
        load_config()
    msg = str(exc_info.value)
    assert "DISCORD_GUILD_ID" in msg
    # Must never echo the raw rejected value (including secrets pasted here).
    if value.strip():
        assert value not in msg


def test_missing_guild_id_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    _base_env(monkeypatch)
    monkeypatch.delenv("DISCORD_GUILD_ID", raising=False)
    with pytest.raises(ConfigError) as exc_info:
        load_config()
    assert "DISCORD_GUILD_ID" in str(exc_info.value)


def test_valid_guild_id_loads(monkeypatch: pytest.MonkeyPatch) -> None:
    _base_env(monkeypatch)
    cfg = load_config()
    assert cfg.guild_id == 111111111111111111
    assert cfg.enable_console_mirror is False


@pytest.mark.parametrize("raw", ["true", "TRUE", "1", "yes", "on"])
def test_enable_console_mirror_true(
    monkeypatch: pytest.MonkeyPatch, raw: str
) -> None:
    _base_env(monkeypatch)
    monkeypatch.setenv("ENABLE_CONSOLE_MIRROR", raw)
    assert load_config().enable_console_mirror is True


@pytest.mark.parametrize("raw", ["false", "0", "no", "off", ""])
def test_enable_console_mirror_false(
    monkeypatch: pytest.MonkeyPatch, raw: str
) -> None:
    _base_env(monkeypatch)
    if raw == "":
        monkeypatch.delenv("ENABLE_CONSOLE_MIRROR", raising=False)
    else:
        monkeypatch.setenv("ENABLE_CONSOLE_MIRROR", raw)
    assert load_config().enable_console_mirror is False


def test_invalid_bool_does_not_echo(monkeypatch: pytest.MonkeyPatch) -> None:
    _base_env(monkeypatch)
    secretish = "Bearer-fake-token-xyz"
    monkeypatch.setenv("ENABLE_CONSOLE_MIRROR", secretish)
    with pytest.raises(ConfigError) as exc_info:
        load_config()
    msg = str(exc_info.value)
    assert "ENABLE_CONSOLE_MIRROR" in msg
    assert secretish not in msg


def test_invalid_integer_does_not_echo(monkeypatch: pytest.MonkeyPatch) -> None:
    _base_env(monkeypatch)
    planted = "super-secret-token-value"
    monkeypatch.setenv("ADMIN_ROLE_ID", planted)
    with pytest.raises(ConfigError) as exc_info:
        load_config()
    assert planted not in str(exc_info.value)
    assert "ADMIN_ROLE_ID" in str(exc_info.value)
