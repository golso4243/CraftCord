"""
External integration services used by the CraftCord bot.

The modules in this package encapsulate everything that talks to the
Minecraft server or its log files, giving the rest of the codebase
(primarily the cogs) a clean async interface to work against:

* :mod:`bot.services.log_service` — defines the :class:`LogSource`
  abstract base plus :class:`LocalLogTailer` for the ``tail -F``
  local-file implementation, and the :func:`build_log_source` factory
  used by the console cog.
* :mod:`bot.services.pterodactyl_service` — WebSocket + REST client
  for Pterodactyl-based panels (PebbleHost etc.). Provides the
  :class:`PterodactylLogSource` implementation selected when
  ``LOG_SOURCE=pterodactyl``.
* :mod:`bot.services.mcstatus_service` — pings the server via the
  public Server List Ping protocol for liveness and player info.
* :mod:`bot.services.rcon_service` — sends RCON commands over TCP,
  with locking, reconnect for later requests, and no silent replay of
  mutating commands after uncertain delivery.
* :mod:`bot.services.player_identity` — optional player-identity
  delivery for Minecraft chat and player events via one CraftCord-owned
  webhook per channel, with cached Minecraft head avatars.

Each service is constructed once during bot startup (see ``bot.py``) and
reused across cogs so that connections, threads and tailers aren't
duplicated.
"""
