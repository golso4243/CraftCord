"""
Minecraft-specific validation and sanitization helpers.

Pure, side-effect-free functions used before sending user input to RCON
or other Minecraft-facing APIs. Player-name rules match Java Edition
usernames: 1-16 characters of letters, digits, and underscores.
"""
from __future__ import annotations

import re

_USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{1,16}$")
_CONTROL_CHAR_RE = re.compile(r"[\x00-\x1f\x7f]+")

INVALID_MINECRAFT_USERNAME_MSG = (
    "Invalid Minecraft username. Use 1-16 letters, numbers, or underscores."
)


def is_valid_minecraft_username(name: str) -> bool:
    """Return whether ``name`` is a valid Java Edition username."""
    return bool(_USERNAME_RE.match(name))


def normalize_minecraft_username(name: str) -> str:
    """Trim and validate a Minecraft username.

    Raises :class:`ValueError` with :data:`INVALID_MINECRAFT_USERNAME_MSG`
    when the name is empty or contains invalid characters.
    """
    normalized = name.strip()
    if not is_valid_minecraft_username(normalized):
        raise ValueError(INVALID_MINECRAFT_USERNAME_MSG)
    return normalized


def sanitize_moderation_reason(reason: str, *, max_length: int = 200) -> str:
    """Normalize a kick/ban reason for safe RCON interpolation.

    Trims whitespace, collapses control characters and newlines into
    spaces, and caps length while preserving normal spaces and punctuation.
    """
    sanitized = _CONTROL_CHAR_RE.sub(" ", reason.strip()).strip()
    if len(sanitized) > max_length:
        sanitized = sanitized[:max_length]
    return sanitized
