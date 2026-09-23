"""
Persistence layer for the CraftCord Discord bot.

The bot uses a small SQLite database to retain state across restarts —
primarily the id of the pinned status message and the Minecraft server's
reachability-based "online since" timestamp. All access goes through
the :class:`Database` class defined in :mod:`bot.database.db`.

Typical usage::

    from bot.database.db import Database

    db = Database(Path("data/bot.sqlite"))
    await db.connect()
    try:
        ...
    finally:
        await db.close()
"""
