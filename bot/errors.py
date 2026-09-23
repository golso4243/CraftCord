"""
Custom exception types used throughout the bot.

Keeping project-specific exceptions in a single, dependency-free module
lets every other module import them without risking circular imports
(notably :mod:`bot.config`, which raises :class:`ConfigError` during the
very first import of the package).

Service-level exceptions that are tightly coupled to a single subsystem
live next to that subsystem instead — for example,
:class:`bot.services.rcon_service.RconError` — so they can evolve with
their owning module.
"""
from __future__ import annotations


class ConfigError(RuntimeError):
    """Raised when a required environment variable is missing or invalid.

    Subclasses :class:`RuntimeError` rather than :class:`ValueError` so
    callers can distinguish "the environment is wrong" from "this
    function received a bad argument", and so a bare
    ``except RuntimeError`` at the top of ``main`` catches configuration
    problems cleanly without swallowing unrelated ``ValueError``\\s.
    """
