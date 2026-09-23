"""
Environment-driven configuration for the CraftCord bot.

All runtime tunables (tokens, role ids, hostnames, paths) come from
environment variables — typically via a ``.env`` file in the project
root, loaded automatically on import. Every other module depends on the
singleton :data:`config` instance rather than reading ``os.environ``
directly, which keeps the "what does this bot need?" surface area
documented in one place (see :class:`Config`).

Design choices:

* **Frozen dataclass.** :class:`Config` is immutable, so we catch
  accidental mutation at runtime and can safely share the object across
  threads / tasks.
* **Fail fast.** ``_req`` raises :class:`~bot.errors.ConfigError` as
  soon as a required variable is missing, so startup aborts with a
  clear message instead of crashing later from an unexpected
  ``NoneType`` dereference.
* **Import-time load.** :func:`load_config` runs exactly once on first
  import. Anywhere you ``from bot.config import config``, you get the
  same validated instance.
* **No value echo.** Invalid configuration errors name the setting but
  never include the raw environment value, so a mistyped token pasted
  into the wrong variable cannot leak into stderr.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

from bot.errors import ConfigError

# Populates os.environ from a local .env file if one exists. This is a
# no-op in production environments where variables are injected by the
# platform (Docker, systemd, etc.). Tests set CRAFTCORD_SKIP_DOTENV so
# a developer machine's real .env cannot override synthetic fixtures.
if not os.environ.get("CRAFTCORD_SKIP_DOTENV"):
    load_dotenv()


__all__ = ["Config", "ConfigError", "config", "load_config"]

# Truthy / falsy tokens accepted by ``_bool``. Anything else fails
# validation without echoing the raw string.
_BOOL_TRUE = frozenset({"1", "true", "yes", "on"})
_BOOL_FALSE = frozenset({"0", "false", "no", "off"})


def _req(name: str) -> str:
    """Read a required environment variable.

    Raises :class:`ConfigError` if the variable is unset or whitespace-
    only, so the bot refuses to start in a half-configured state.
    """
    value = os.getenv(name, "").strip()
    if not value:
        raise ConfigError(f"Missing required environment variable: {name}")
    return value


def _opt(name: str, default: str = "") -> str:
    """Read an optional string variable, returning ``default`` if unset."""
    return os.getenv(name, default).strip()


def _int(
    name: str,
    default: Optional[int] = None,
    *,
    required: bool = False,
    positive: bool = False,
) -> Optional[int]:
    """Read an integer variable, with optional default and required flag.

    Malformed values raise :class:`ConfigError` naming the variable
    without echoing the raw string. When ``positive`` is true, zero and
    negative values are also rejected (used for Discord snowflake IDs).
    """
    raw = os.getenv(name, "").strip()
    if not raw:
        if required:
            raise ConfigError(f"Missing required environment variable: {name}")
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ConfigError(f"{name} must be an integer") from None
    if positive and value <= 0:
        raise ConfigError(f"{name} must be a positive integer") from None
    return value


def _bool(name: str, default: bool = False) -> bool:
    """Read a boolean variable with a fixed accept-list of tokens.

    Blank / unset returns ``default``. Any other non-blank value that
    is not a recognised true/false token raises :class:`ConfigError`
    without echoing the raw value.
    """
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    lowered = raw.lower()
    if lowered in _BOOL_TRUE:
        return True
    if lowered in _BOOL_FALSE:
        return False
    raise ConfigError(f"{name} must be a boolean (true/false)") from None


@dataclass(frozen=True)
class Config:
    """Immutable snapshot of all configuration values.

    Fields are grouped by subsystem (Discord, Roles, Channels, Minecraft,
    RCON, Log file, Tuning) to mirror how they're documented in
    ``.env.example``.
    """

    # ── Discord ──────────────────────────────────────────────────
    discord_token: str
    # Required guild this installation is bound to. Commands and
    # outbound destinations are refused outside this guild.
    guild_id: int

    # ── Roles ────────────────────────────────────────────────────
    # Consumed by bot.utils.permissions; all optional so servers can
    # start up with only some tiers defined.
    admin_role_id: Optional[int]
    mod_role_id: Optional[int]
    member_role_id: Optional[int]

    # ── Channels ─────────────────────────────────────────────────
    status_channel_id: Optional[int]
    # Destination for generic console output when console mirroring is
    # explicitly enabled. See enable_console_mirror.
    console_channel_id: Optional[int]
    # Bidirectional chat bridge. When set, player chat is forwarded
    # from MC -> here, and messages typed in this channel are injected
    # back into MC via RCON tellraw. See bot.cogs.chat.
    chat_channel_id: Optional[int]
    # Pretty embeds for join/leave/death/advancement events. When
    # unset, those events are dropped (they do not fall through to
    # the console channel).
    events_channel_id: Optional[int]

    # ── Minecraft (SLP / public ping) ─────────────────────────────
    mc_host: str
    mc_port: int
    # Optional pretty address shown in the status embed (e.g.
    # "play.example.com"). Purely cosmetic — the bot still uses
    # ``mc_host``/``mc_port`` for the actual SLP ping. When unset the
    # Address field is omitted from the embed entirely.
    public_address: Optional[str]

    # ── RCON (single configured Minecraft server) ─────────────────
    rcon_host: str
    rcon_port: int
    rcon_password: str

    # ── Log source ───────────────────────────────────────────────
    # Which backend the console cog uses to ingest log lines. See
    # bot.services.log_service.build_log_source for the allowed
    # values ("local", "pterodactyl").
    log_source: str

    # LOG_SOURCE=local: path to latest.log on the local filesystem.
    # Unset disables the log-mirror feature when in local mode.
    mc_log_path: Optional[Path]

    # LOG_SOURCE=pterodactyl: credentials for the panel (PebbleHost,
    # BisectHosting, Apex, Shockbyte, self-hosted Pterodactyl, …).
    # All three must be set together; any missing value disables the
    # log-mirror feature when in pterodactyl mode.
    pterodactyl_panel_url: str
    pterodactyl_server_id: str
    pterodactyl_api_key: str

    # ── Tuning ───────────────────────────────────────────────────
    # How often the background status loop refreshes the pinned embed.
    status_update_interval: int
    # Where the SQLite database file lives. Parent directory is created
    # on demand by Database.connect().
    database_path: Path
    # When true *and* CONSOLE_CHANNEL_ID resolves in the configured
    # guild, generic console lines are mirrored to Discord. Defaults
    # to false because raw logs can contain player IPs, UUIDs, chat,
    # and operational detail.
    enable_console_mirror: bool


def load_config() -> Config:
    """Read the current environment and return a validated :class:`Config`.

    Called exactly once at import time to populate the module-level
    :data:`config` singleton. Exposed publicly so tests can build a
    fresh Config after monkeypatching ``os.environ``.
    """
    log_path_raw = _opt("MC_LOG_PATH")
    db_path_raw = _opt("DATABASE_PATH", "data/bot.db")

    guild_id = _int("DISCORD_GUILD_ID", required=True, positive=True)
    if guild_id is None:  # pragma: no cover - required path always returns int
        raise ConfigError("Missing required environment variable: DISCORD_GUILD_ID")

    cfg = Config(
        discord_token=_req("DISCORD_TOKEN"),
        guild_id=guild_id,
        admin_role_id=_int("ADMIN_ROLE_ID", positive=True),
        mod_role_id=_int("MOD_ROLE_ID", positive=True),
        member_role_id=_int("MEMBER_ROLE_ID", positive=True),
        status_channel_id=_int("STATUS_CHANNEL_ID", positive=True),
        console_channel_id=_int("CONSOLE_CHANNEL_ID", positive=True),
        chat_channel_id=_int("CHAT_CHANNEL_ID", positive=True),
        events_channel_id=_int("EVENTS_CHANNEL_ID", positive=True),
        mc_host=_opt("MC_HOST", "127.0.0.1"),
        # `_int(..., default) or default` collapses a None return (which
        # can't happen here given the default, but keeps the type
        # checker happy) into the concrete int the field expects.
        mc_port=_int("MC_PORT", 25565) or 25565,
        public_address=(_opt("PUBLIC_ADDRESS") or None),
        rcon_host=_opt("RCON_HOST", "127.0.0.1"),
        rcon_port=_int("RCON_PORT", 25575) or 25575,
        rcon_password=_req("RCON_PASSWORD"),
        log_source=_opt("LOG_SOURCE", "local").lower(),
        mc_log_path=Path(log_path_raw) if log_path_raw else None,
        pterodactyl_panel_url=_opt("PTERODACTYL_PANEL_URL"),
        pterodactyl_server_id=_opt("PTERODACTYL_SERVER_ID"),
        pterodactyl_api_key=_opt("PTERODACTYL_API_KEY"),
        # Floor the refresh interval at 10s to avoid hammering Discord
        # with edit requests and tripping rate limits.
        status_update_interval=max(10, _int("STATUS_UPDATE_INTERVAL", 30) or 30),
        database_path=Path(db_path_raw),
        enable_console_mirror=_bool("ENABLE_CONSOLE_MIRROR", False),
    )
    return cfg


# Module-level singleton. Importing this module runs ``load_dotenv()``
# (unless CRAFTCORD_SKIP_DOTENV is set) and ``load_config()`` exactly
# once; every subsequent ``from bot.config import config`` returns the
# same validated instance.
config: Config = load_config()
