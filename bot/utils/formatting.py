"""
Small pure formatting / parsing helpers.

Everything in this module is deliberately side-effect free so it can be
unit-tested in isolation and reused from anywhere (embeds, cogs,
services). Helpers here strip Minecraft formatting codes, neutralize
code fences, and humanize uptime durations.
"""
from __future__ import annotations

import re


def humanize_uptime(seconds: int) -> str:
    """Render a duration in seconds as a compact ``"1d 2h 3m"`` string.

    Zero and negative inputs are normalised to zero — we'd rather show
    ``"0s"`` than surface an implementation quirk (e.g. clock skew
    yielding a negative diff). The smallest non-zero unit is minutes
    unless the entire duration is under a minute, in which case seconds
    are shown so the display never reads as an empty string.
    """
    if seconds < 0:
        seconds = 0
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, secs = divmod(rem, 60)
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if minutes:
        parts.append(f"{minutes}m")
    # Fall back to seconds only when every larger bucket was empty,
    # otherwise the output would read like "1d 2h 3m 5s" which is noisier
    # than we want for a status embed field.
    if not parts:
        parts.append(f"{secs}s")
    return " ".join(parts)


# Minecraft ``§`` color / formatting codes — one section sign followed by
# a single character from the documented set (0-9, a-f, k-o, r).
_MC_COLOR_RE = re.compile(r"§[0-9a-fk-orA-FK-OR]")
# Standard ANSI CSI sequences (the kind Paper emits when colour is on in
# the console). We don't bother with the more exotic OSC sequences since
# Minecraft doesn't emit them.
_ANSI_RE = re.compile(r"\x1B\[[0-?]*[ -/]*[@-~]")


def clean_mc_text(text: str) -> str:
    """Strip Minecraft / ANSI formatting codes and surrounding whitespace.

    Used both when forwarding console lines to Discord (so the code
    block doesn't contain raw escape sequences) and when parsing RCON
    output (so regexes don't have to account for stray color codes in
    the middle of numbers).
    """
    return _ANSI_RE.sub("", _MC_COLOR_RE.sub("", text)).strip()


# Zero-width space inserted between backticks so an untrusted string
# containing triple backticks cannot close a surrounding Discord code
# fence and turn the rest of the message into mentionable markdown.
_ZWSP = "\u200b"


def neutralize_code_fences(text: str) -> str:
    """Break embedded triple-backtick sequences inside code-block content.

    Preserves readability while preventing fence breakout when the
    result is wrapped in `` ``` `` by the caller.
    """
    return text.replace("```", "`" + _ZWSP + "`" + _ZWSP + "`")


def escape_minecraft_username(name: str) -> str:
    """Escape Discord markdown characters that a Minecraft name may contain.

    Parsed names are ``[A-Za-z0-9_]{1,16}``. Underscore is the only
    markdown metacharacter in that set; backslash-escaping it prevents
    italic spoiling when the name is rendered in a Discord message.
    """
    return name.replace("\\", "\\\\").replace("_", "\\_")
