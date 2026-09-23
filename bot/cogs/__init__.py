"""
Cog package for the CraftCord Discord bot.

Each module in this package is a discord.py extension that can be loaded
individually via ``bot.load_extension("bot.cogs.<name>")``. Keeping the
extensions self-contained lets us reload or disable a feature without
restarting the whole bot.

Current cogs:

* ``admin``   — operational / diagnostic commands (``/ping``, ``/botinfo``,
  ``/sync``).
* ``rcon``    — RCON-backed slash commands (``/list``, ``/say``, ``/kick``,
  ``/ban``, ``/pardon``, ``/whitelist``).
* ``status``  — maintains a pinned live-status embed and the ``/status``
  command.
* ``console`` — tails ``latest.log`` and routes each line to the right
  channel: chat -> chat channel, events -> events channel, everything
  else -> generic console channel when mirroring is enabled.
* ``chat``    — Discord -> Minecraft direction of the chat bridge;
  relays messages in the chat channel into the game via
  RCON ``/tellraw``.
"""
