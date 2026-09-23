"""
Pluggable "give me a stream of Minecraft log lines" abstraction.

The :class:`LogSource` abstract base defines the interface every log
producer exposes to the rest of the bot:

* :py:meth:`LogSource.tail` — an async iterator that yields one
  complete log line per element, forever, until :py:meth:`stop` is
  called.
* :py:meth:`LogSource.stop` — cooperative shutdown signal.

Concrete implementations currently live here (:class:`LocalLogTailer`,
tails a file on disk) and in :mod:`bot.services.pterodactyl_service`
(:class:`~bot.services.pterodactyl_service.PterodactylLogSource`,
connects to a Pterodactyl/PebbleHost panel over WebSocket). The console
cog consumes *either* through the same interface, so swapping between
them is a pure configuration change.

Select between implementations with :func:`build_log_source` — it reads
:data:`bot.config.config` and returns the right instance (or ``None`` if
nothing is configured, which disables the log-mirror feature entirely).
"""
from __future__ import annotations

import asyncio
import logging
import os
from abc import ABC, abstractmethod
from pathlib import Path
from typing import AsyncIterator, Optional

log = logging.getLogger(__name__)


class LogSource(ABC):
    """A source of Minecraft server log lines.

    Subclasses must yield *cleaned-enough* text: the console cog applies
    :func:`bot.utils.formatting.clean_mc_text` on top to strip colour and
    ANSI codes, so implementations don't have to worry about that.
    """

    @abstractmethod
    def stop(self) -> None:
        """Request shutdown. Safe to call more than once."""

    @abstractmethod
    def tail(self) -> AsyncIterator[str]:
        """Yield newline-terminated log lines until :py:meth:`stop` is called."""


class LocalLogTailer(LogSource):
    """Tail a ``latest.log`` file on the local filesystem, ``tail -F`` style.

    Use this when the bot runs on the same host as the Minecraft server
    (e.g. a home Paper server, a dedicated VPS, or development on
    localhost). For managed hosting like PebbleHost the bot usually
    doesn't share a filesystem with the server — prefer
    :class:`~bot.services.pterodactyl_service.PterodactylLogSource`
    there.

    Handles the awkward edge cases of real-world log files:

    * **File not existing yet.** On a fresh install the log may not
      exist until the MC server first starts.
    * **Log rotation / truncation.** Paper rotates ``latest.log`` to a
      timestamped archive; we detect the inode change and reopen. If
      the file is truncated in-place we rewind to the beginning.
    * **Partial lines at EOF.** Servers flush mid-line; we buffer
      bytes and only yield once a newline lands.

    Blocking I/O (``open``, ``read``, ``stat``, ``close``) is dispatched
    to the default thread pool via :func:`asyncio.to_thread` so the
    event loop stays responsive even on slow filesystems.
    """

    def __init__(self, path: Path, poll_interval: float = 1.0):
        self._path = path
        # How long to wait between poll iterations when there's nothing
        # new to read. Small enough to feel live, large enough to be
        # gentle on the filesystem.
        self._poll = poll_interval
        self._stop = asyncio.Event()

    def stop(self) -> None:
        """Signal the tailer to exit after the current iteration."""
        self._stop.set()

    async def tail(self) -> AsyncIterator[str]:
        """Yield log lines until :py:meth:`stop` is called.

        This is an infinite async generator; callers typically iterate
        it with ``async for`` inside a dedicated task.
        """
        buffer = ""
        fh = None
        inode: Optional[int] = None
        try:
            while not self._stop.is_set():
                # (Re)open on first iteration or after a rotation/missing file.
                if fh is None:
                    fh, inode = await asyncio.to_thread(self._open_at_end, self._path)
                    if fh is None:
                        # File isn't there yet — back off and retry.
                        await self._sleep_or_stop(self._poll)
                        continue

                # Detect rotation or in-place truncation *before* reading,
                # so we don't accidentally read the tail of a freshly
                # rotated-out file.
                try:
                    stat = await asyncio.to_thread(os.stat, self._path)
                    if inode is not None and stat.st_ino != inode:
                        # The path now points to a different file
                        # (logrotate style). Drop our handle and
                        # reopen on the next pass.
                        await asyncio.to_thread(fh.close)
                        fh = None
                        continue
                    if stat.st_size < await asyncio.to_thread(fh.tell):
                        # File was truncated in place (size shrunk
                        # below our read cursor). Rewind to the
                        # beginning rather than sitting past EOF.
                        await asyncio.to_thread(fh.seek, 0)
                except FileNotFoundError:
                    # Path vanished between iterations (e.g.
                    # mid-rotation). Reset and wait for it to return.
                    await asyncio.to_thread(fh.close)
                    fh = None
                    buffer = ""
                    await self._sleep_or_stop(self._poll)
                    continue

                chunk = await asyncio.to_thread(fh.read)
                if not chunk:
                    # Nothing new — sleep until the next poll or stop signal.
                    await self._sleep_or_stop(self._poll)
                    continue

                # Accumulate and split only on complete lines so we
                # never yield a partial line while the server flushes.
                buffer += chunk
                while "\n" in buffer:
                    line, buffer = buffer.split("\n", 1)
                    if line:
                        yield line
        finally:
            # Best-effort close on shutdown. Swallow errors because
            # the generator may be unwinding from cancellation and
            # there's nowhere useful to propagate them.
            if fh is not None:
                try:
                    await asyncio.to_thread(fh.close)
                except Exception:
                    pass

    async def _sleep_or_stop(self, seconds: float) -> None:
        """Sleep for ``seconds`` or wake early if :py:meth:`stop` was called.

        Using ``wait_for`` on the stop event (rather than a plain
        ``asyncio.sleep``) lets shutdown be snappy — we don't have to
        wait out a full poll interval after ``stop()`` is invoked.
        """
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass

    @staticmethod
    def _open_at_end(path: Path):
        """Open ``path`` for reading, seek to EOF, and return ``(handle, inode)``.

        We seek to the end on initial open so we only emit *new* log
        lines — reading the full backlog on every startup would spam
        the channel. Returns ``(None, None)`` if the file can't be
        opened yet.
        """
        try:
            # errors="replace" guards against the occasional malformed
            # byte that MC plugins sometimes emit; we'd rather see a
            # replacement character than crash the tailer.
            fh = open(path, "r", encoding="utf-8", errors="replace")
        except FileNotFoundError:
            return None, None
        except OSError as e:
            log.warning("Could not open log file %s: %s", path, e)
            return None, None
        fh.seek(0, os.SEEK_END)
        try:
            # Cache the inode so we can detect a rotation later via os.stat.
            inode = os.fstat(fh.fileno()).st_ino
        except OSError:
            # Some filesystems (e.g. certain Windows setups) don't
            # expose a stable inode; we degrade gracefully by
            # disabling rotation detection in that case.
            inode = None
        return fh, inode


# Backwards-compatibility alias. The old class was called LogTailer;
# existing imports (if any) continue to work while new code should use
# ``LocalLogTailer`` or ``build_log_source()``.
LogTailer = LocalLogTailer


def build_log_source() -> Optional[LogSource]:
    """Construct the configured :class:`LogSource`, or return ``None``.

    Reads :data:`bot.config.config` to decide between:

    * ``LOG_SOURCE=local`` (default) — :class:`LocalLogTailer`,
      requires ``MC_LOG_PATH`` to be set.
    * ``LOG_SOURCE=pterodactyl`` —
      :class:`~bot.services.pterodactyl_service.PterodactylLogSource`,
      requires ``PTERODACTYL_PANEL_URL``, ``PTERODACTYL_SERVER_ID``
      and ``PTERODACTYL_API_KEY``.

    Returns ``None`` (with a friendly log message) if the selected
    source is missing its required configuration. That allows the
    console cog to silently disable log mirroring without aborting
    the whole bot.
    """
    # Imported here (not at module top) to keep the import graph
    # acyclic: config imports bot.errors, and this module is imported
    # by the console cog which in turn is imported by the bot. The
    # lazy import avoids making log_service transitively depend on
    # aiohttp just for the Pterodactyl path.
    from bot.config import config

    source = (config.log_source or "local").lower()

    if source == "local":
        if config.mc_log_path is None:
            log.info("LOG_SOURCE=local but MC_LOG_PATH is not set — log mirroring disabled.")
            return None
        return LocalLogTailer(config.mc_log_path)

    if source == "pterodactyl":
        if (
            not config.pterodactyl_panel_url
            or not config.pterodactyl_server_id
            or not config.pterodactyl_api_key
        ):
            log.info(
                "LOG_SOURCE=pterodactyl but panel URL / server ID / API key "
                "are not all set — log mirroring disabled."
            )
            return None
        # Local import: pterodactyl_service pulls in aiohttp, which
        # we only want to import if the operator actually opted in.
        from bot.services.pterodactyl_service import PterodactylLogSource

        return PterodactylLogSource(
            panel_url=config.pterodactyl_panel_url,
            server_id=config.pterodactyl_server_id,
            api_key=config.pterodactyl_api_key,
        )

    log.warning("Unknown LOG_SOURCE=%r — log mirroring disabled.", source)
    return None
