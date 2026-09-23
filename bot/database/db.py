"""
Tiny async SQLite wrapper.

We intentionally stay well below "ORM" levels of abstraction — the bot's
persistence needs are modest enough that raw parameterized SQL is the
simplest thing that works:

* ``settings``     — generic key/value store. Currently holds the pinned
  status-message id, but it's a good landing spot for any other small
  piece of state we need to survive restarts.
* ``server_state`` — a single-row table (enforced via the ``CHECK (id = 1)``
  constraint) tracking when the Minecraft server was first observed online.
  We persist this so that uptime in the status embed is continuous across
  bot restarts instead of resetting every time the process cycles.

All methods require :py:meth:`Database.connect` to have been called first;
the underlying connection is reused for the lifetime of the bot.
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Optional

import aiosqlite


class Database:
    """Thin async wrapper around a single SQLite file.

    The class owns exactly one :class:`aiosqlite.Connection`. Concurrency is
    left to aiosqlite/SQLite — for this bot's workload (a handful of
    writes per minute at most) that's more than enough, and avoiding a
    connection pool keeps the code and transactional semantics simple.
    """

    def __init__(self, path: Path):
        self.path = path
        # Connection is created lazily in `connect()` so constructing a
        # Database is cheap and side-effect free (handy for tests).
        self._conn: Optional[aiosqlite.Connection] = None

    async def connect(self) -> None:
        """Open the database file, create the schema, and seed fixed rows.

        Safe to call on a fresh install *and* on every subsequent startup:
        the ``IF NOT EXISTS`` / ``INSERT OR IGNORE`` guards make the
        statements idempotent, so we can treat this as our one-stop
        migration step until the schema grows enough to need real migrations.
        """
        # Ensure the parent directory exists — by default we're writing to
        # something like ``./data/bot.sqlite`` which may not exist yet on a
        # first run.
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(self.path)
        # Row factory gives us mapping-style access (row["col"]) which is
        # much more readable than positional tuple indexing.
        self._conn.row_factory = aiosqlite.Row
        await self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS settings (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS server_state (
                -- Single-row table: the CHECK constraint makes it impossible
                -- to accidentally insert a second row of state.
                id           INTEGER PRIMARY KEY CHECK (id = 1),
                online_since INTEGER
            );
            INSERT OR IGNORE INTO server_state (id, online_since) VALUES (1, NULL);
            """
        )
        await self._conn.commit()

    async def close(self) -> None:
        """Close the connection if it's open.

        Idempotent — calling ``close()`` on an already-closed database is a
        no-op, which lets callers use it unconditionally in shutdown paths.
        """
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    @property
    def conn(self) -> aiosqlite.Connection:
        """Return the live connection, raising if ``connect()`` wasn't called.

        Using a property with an explicit error gives callers a much clearer
        message than the ``AttributeError`` they'd otherwise hit when
        dereferencing ``None``.
        """
        if self._conn is None:
            raise RuntimeError("Database is not connected. Call connect() first.")
        return self._conn

    # ── settings key/value ────────────────────────────────────────
    async def get_setting(self, key: str) -> Optional[str]:
        """Fetch a value from the ``settings`` table, or ``None`` if absent."""
        async with self.conn.execute(
            "SELECT value FROM settings WHERE key = ?", (key,)
        ) as cur:
            row = await cur.fetchone()
        return row["value"] if row else None

    async def set_setting(self, key: str, value: str) -> None:
        """Upsert ``(key, value)`` into the ``settings`` table.

        Uses SQLite's ``ON CONFLICT … DO UPDATE`` (available since 3.24) so
        this is a single round-trip rather than a separate select + insert
        or update.
        """
        await self.conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        await self.conn.commit()

    # ── server uptime tracking ────────────────────────────────────
    async def mark_online(self) -> int:
        """Record that the MC server is online and return ``online_since``.

        The operation is idempotent: if we already have an ``online_since``
        stamped, we return it unchanged. This is exactly what the status
        embed wants — a continuous "online since" that only resets when we
        explicitly call :py:meth:`mark_offline`.

        Returns:
            Unix epoch seconds of the earliest observed online time.
        """
        async with self.conn.execute(
            "SELECT online_since FROM server_state WHERE id = 1"
        ) as cur:
            row = await cur.fetchone()
        if row and row["online_since"]:
            return int(row["online_since"])
        now = int(time.time())
        await self.conn.execute(
            "UPDATE server_state SET online_since = ? WHERE id = 1", (now,)
        )
        await self.conn.commit()
        return now

    async def mark_offline(self) -> None:
        """Clear ``online_since`` so the next ``mark_online()`` starts fresh.

        We null the field (rather than deleting the row) to preserve the
        single-row invariant enforced by the ``CHECK (id = 1)`` constraint.
        """
        await self.conn.execute(
            "UPDATE server_state SET online_since = NULL WHERE id = 1"
        )
        await self.conn.commit()

    async def get_online_since(self) -> Optional[int]:
        """Return the current ``online_since`` epoch, or ``None`` if offline."""
        async with self.conn.execute(
            "SELECT online_since FROM server_state WHERE id = 1"
        ) as cur:
            row = await cur.fetchone()
        return int(row["online_since"]) if row and row["online_since"] else None
