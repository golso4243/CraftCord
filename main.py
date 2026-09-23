"""
Process entry point for the CraftCord Discord bot.

This module does three small but important things:

1. Configures root logging before anything else runs, so that errors
   during subsequent imports (most notably configuration validation
   in :mod:`bot.config`) are visible.
2. Imports the bot and config *inside* ``main()`` so that a
   :class:`~bot.errors.ConfigError` raised at import time can be
   caught and turned into a clean CLI error message + non-zero exit
   code, instead of a Python traceback.
3. Hands control over to :meth:`discord.Client.run`, which owns the
   asyncio event loop for the lifetime of the process.

Run with ``python main.py`` from the project root.
"""
from __future__ import annotations

import logging
import sys

from bot.errors import ConfigError


def _setup_logging() -> None:
    """Configure root logging for the whole process.

    We deliberately write to stdout (not stderr) so that systemd /
    Docker pick the output up as normal application logs rather than
    flagging every line as an error. The format is intentionally plain
    text so log aggregators don't need a custom parser.

    ``discord.py`` itself is quite chatty at INFO level (gateway
    heartbeats, session resumes, etc.) so we clamp its logger to
    WARNING to keep the bot's own logs readable. Raise it back to INFO
    temporarily if you need to debug a connection issue.
    """
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        logging.Formatter(
            "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    # Clear any handlers that were installed by earlier imports (e.g.
    # dependencies calling ``logging.basicConfig``) so we don't end up
    # with duplicate lines.
    root.handlers.clear()
    root.addHandler(handler)
    logging.getLogger("discord").setLevel(logging.WARNING)


def main() -> None:
    """Configure logging, validate config, and run the bot until shutdown."""
    _setup_logging()

    # Imports are deferred until after logging is set up so that the
    # ConfigError path below can render a friendly message. Importing
    # ``bot.config`` runs ``load_config()`` as a side effect, which is
    # where missing/invalid env vars are discovered.
    try:
        from bot.bot import CraftCordBot
        from bot.config import config
    except ConfigError as e:
        # Print to stderr (not the logger) because the operator is
        # likely running this interactively and expects misconfiguration
        # to show up in the terminal, not buried in a log file.
        print(f"Configuration error: {e}", file=sys.stderr)
        sys.exit(1)

    bot = CraftCordBot()
    # ``log_handler=None`` tells discord.py not to install its own
    # logging handler — we've already configured root logging above
    # and don't want duplicate formatting.
    bot.run(config.discord_token, log_handler=None)


if __name__ == "__main__":
    main()
