"""Shared pytest configuration and fixtures.

Must run before any ``bot.*`` import so that:

1. A developer's real ``.env`` is never loaded (``CRAFTCORD_SKIP_DOTENV``).
2. Required secrets are synthetic placeholders, never production values.
3. Optional connection settings that could point at a live host are cleared.
"""
from __future__ import annotations

import os

# Signal bot.config to skip load_dotenv() before that module is imported.
os.environ["CRAFTCORD_SKIP_DOTENV"] = "1"

# Overwrite (not setdefault) so inherited shell env cannot supply real
# tokens or point tests at a live Minecraft / panel host.
os.environ["DISCORD_TOKEN"] = "test-token"
os.environ["RCON_PASSWORD"] = "test-password"
os.environ["DISCORD_GUILD_ID"] = "111111111111111111"

# Clear optional connection / destination settings that might be present
# in the process environment from a previous interactive session.
_CLEAR_OPTIONAL = (
    "ADMIN_ROLE_ID",
    "MOD_ROLE_ID",
    "MEMBER_ROLE_ID",
    "STATUS_CHANNEL_ID",
    "CONSOLE_CHANNEL_ID",
    "CHAT_CHANNEL_ID",
    "EVENTS_CHANNEL_ID",
    "PUBLIC_ADDRESS",
    "MC_LOG_PATH",
    "PTERODACTYL_PANEL_URL",
    "PTERODACTYL_SERVER_ID",
    "PTERODACTYL_API_KEY",
    "ENABLE_CONSOLE_MIRROR",
)
for _name in _CLEAR_OPTIONAL:
    os.environ.pop(_name, None)

# Force localhost defaults so a shell export of MC_HOST / RCON_HOST
# cannot redirect unit tests at a real server.
os.environ["MC_HOST"] = "127.0.0.1"
os.environ["RCON_HOST"] = "127.0.0.1"
os.environ["MC_PORT"] = "25565"
os.environ["RCON_PORT"] = "25575"
os.environ["LOG_SOURCE"] = "local"
os.environ["DATABASE_PATH"] = "data/test-bot.db"
