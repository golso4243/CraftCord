"""
CraftCord Discord bot package.

This is the top-level namespace for the bot. The interesting entry points
are:

* :class:`bot.bot.CraftCordBot` — the ``commands.Bot`` subclass that
  wires up shared services (database, RCON, mcstatus) and loads every
  cog under :mod:`bot.cogs`.
* :data:`bot.config.config`      — a frozen :class:`bot.config.Config`
  dataclass populated from environment variables at import time.
* :mod:`bot.errors`              — the project's custom exception types.

Subpackages:

* :mod:`bot.cogs`     — discord.py extensions implementing user-facing
  commands and background tasks.
* :mod:`bot.services` — async clients for external systems (Minecraft
  SLP, RCON, log tailing).
* :mod:`bot.utils`    — pure helpers (embeds, formatting, permission
  checks).
* :mod:`bot.database` — SQLite persistence layer.

The package itself exposes no public API — import the specific module
you need.
"""
