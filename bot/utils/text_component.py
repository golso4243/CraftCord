"""
SNBT text-component helpers for Discord → Minecraft ``tellraw``.

Minecraft Java Edition (since 1.21.5 / 25w02a) accepts text components as
SNBT command arguments rather than JSON strings. This module builds a
small, fixed-shape payload for the chat bridge:

* A list of ``{text, color}`` compounds only — no hover, click, selector,
  translate, or nbt content types.
* Discord display names and message bodies are always literal ``text``
  values, never command syntax.

Escape rules follow the SNBT string expansions restored in snapshot
25w09a (MC-279229 / MC-279250). Ordinary Unicode is emitted as UTF-8
code points inside double-quoted strings; user-supplied backslash
sequences are escaped so they cannot become SNBT escapes.

Tellraw response classification is intentionally narrow: empty and a
few documented English command-parser phrases. It is not a general
RCON or locale-aware parser.
"""
from __future__ import annotations

import re
from enum import Enum, auto
from typing import List, Tuple

from bot.utils.formatting import clean_mc_text

# Matches the client-side RCON body cap in :mod:`bot.services.rcon_service`.
# Kept here so the chat bridge can size the full command before send.
RCON_MAX_BODY_BYTES = 1446

_TELLRAW_PREFIX = "tellraw @a "

# Soft visual cap used when sizing the message before SNBT wrapping.
# The byte-fit pass may shorten further so the full command stays
# within :data:`RCON_MAX_BODY_BYTES`.
DEFAULT_MESSAGE_CHAR_LIMIT = 200

# English command-parser phrases observed on Java Edition for bad
# ``tellraw`` arguments. Matching is case-sensitive on the cleaned
# response. This is not locale-aware and is not claimed for every
# Minecraft release without live confirmation.
_COMMAND_ERROR_PHRASES = (
    "Incorrect argument for command",
    "Unknown or incomplete command",
    "Expected whitespace to end one argument",
    "Malformed '",
    "Could not parse data",
    "Expected ",
    "Unknown command",
)

_NO_PLAYER_PHRASE = "No player was found"

# Decode the restricted SNBT string subset this module emits (tests).
_SNBT_STRING_RE = re.compile(
    r'"((?:\\.|[^"\\])*)"'
)
_TEXT_FIELD_RE = re.compile(
    r'\{text:("(?:\\.|[^"\\])*"),color:("(?:\\.|[^"\\])*")\}'
)


class TellrawOutcome(Enum):
    """How the chat bridge treats an RCON ``tellraw`` response body."""

    ACCEPTED_NO_OUTPUT = auto()
    COMMAND_REJECTED = auto()
    NO_PLAYERS = auto()
    UNKNOWN_OUTPUT = auto()


def snbt_quote(value: str) -> str:
    """Return ``value`` as a double-quoted SNBT string literal.

    Escapes backslash and double-quote so user content cannot introduce
    SNBT escapes, keys, or structure. Newlines, tabs, and CR become
    ``\\n`` / ``\\t`` / ``\\r``. Other C0 controls and DEL become
    ``\\u00XX``. Ordinary Unicode (including emoji) passes through
    unchanged. Apostrophes are literal inside double quotes.
    """
    out: List[str] = ['"']
    for ch in value:
        code = ord(ch)
        if ch == "\\":
            out.append("\\\\")
        elif ch == '"':
            out.append('\\"')
        elif ch == "\n":
            out.append("\\n")
        elif ch == "\t":
            out.append("\\t")
        elif ch == "\r":
            out.append("\\r")
        elif code < 0x20 or code == 0x7F:
            out.append(f"\\u{code:04x}")
        else:
            out.append(ch)
    out.append('"')
    return "".join(out)


def snbt_unescape(quoted: str) -> str:
    """Decode a double-quoted SNBT string produced by :func:`snbt_quote`.

    Used by tests to recover literal text from the serialized payload.
    Supports the escape subset this module emits (not every SNBT form).
    """
    if len(quoted) < 2 or quoted[0] != '"' or quoted[-1] != '"':
        raise ValueError("expected a double-quoted SNBT string")
    body = quoted[1:-1]
    out: List[str] = []
    i = 0
    while i < len(body):
        if body[i] != "\\":
            out.append(body[i])
            i += 1
            continue
        if i + 1 >= len(body):
            raise ValueError("trailing backslash in SNBT string")
        esc = body[i + 1]
        if esc == "\\":
            out.append("\\")
            i += 2
        elif esc == '"':
            out.append('"')
            i += 2
        elif esc == "n":
            out.append("\n")
            i += 2
        elif esc == "t":
            out.append("\t")
            i += 2
        elif esc == "r":
            out.append("\r")
            i += 2
        elif esc == "u" and i + 5 < len(body):
            hex_digits = body[i + 2 : i + 6]
            out.append(chr(int(hex_digits, 16)))
            i += 6
        else:
            raise ValueError(f"unsupported escape \\{esc}")
    return "".join(out)


def snbt_text_component(text: str, *, color: str) -> str:
    """Serialize a plain ``{text, color}`` compound as SNBT."""
    return f"{{text:{snbt_quote(text)},color:{snbt_quote(color)}}}"


def build_discord_chat_snbt(author: str, body: str) -> str:
    """Build the SNBT list for a Discord → Minecraft chat message.

    Shape (no hover, no click)::

        [
          {text:"[Discord] ",color:"blue"},
          {text:"<author>",color:"white"},
          {text:": ",color:"white"},
          {text:"<body>",color:"white"}
        ]

    ``author`` and ``body`` are treated as literal text only.
    """
    parts = [
        snbt_text_component("[Discord] ", color="blue"),
        snbt_text_component(author, color="white"),
        snbt_text_component(": ", color="white"),
        snbt_text_component(body, color="white"),
    ]
    return "[" + ",".join(parts) + "]"


def extract_literal_texts(snbt_list: str) -> List[Tuple[str, str]]:
    """Return ``(text, color)`` pairs from a list built by this module.

    Structural test helper — rejects payloads that are not the fixed
    ``{text,color}`` list shape this serializer emits.
    """
    if not (snbt_list.startswith("[") and snbt_list.endswith("]")):
        raise ValueError("expected an SNBT list")
    inner = snbt_list[1:-1]
    if not inner:
        return []
    matches = list(_TEXT_FIELD_RE.finditer(inner))
    if not matches:
        raise ValueError("no text components found")
    # Ensure the list is exactly the concatenation of matched compounds
    # separated by commas — no extra keys or nesting.
    rebuilt = ",".join(m.group(0) for m in matches)
    if rebuilt != inner:
        raise ValueError("unexpected SNBT structure outside {text,color} compounds")
    return [
        (snbt_unescape(m.group(1)), snbt_unescape(m.group(2))) for m in matches
    ]


def classify_tellraw_response(response: str) -> TellrawOutcome:
    """Classify a ``tellraw`` RCON body for delivery accounting.

    Assumptions (documented, not proven for every locale / version):

    * Successful ``tellraw`` typically returns an empty body.
    * Known English command-parser phrases indicate rejection.
    * The exact English string ``No player was found`` is treated as
      a selector miss (e.g. nobody online for some selector forms).
      Whether vanilla ``tellraw @a`` emits that when the server is empty
      is **not** established for 26.2 without live testing.
    * Any other non-empty body is left unclassified — neither success
      nor rejection is claimed.
    """
    cleaned = clean_mc_text(response or "")
    if not cleaned:
        return TellrawOutcome.ACCEPTED_NO_OUTPUT
    if cleaned == _NO_PLAYER_PHRASE:
        return TellrawOutcome.NO_PLAYERS
    for phrase in _COMMAND_ERROR_PHRASES:
        if phrase in cleaned:
            return TellrawOutcome.COMMAND_REJECTED
    return TellrawOutcome.UNKNOWN_OUTPUT


def _command_bytes(author: str, body: str) -> int:
    return len(
        (_TELLRAW_PREFIX + build_discord_chat_snbt(author, body)).encode("utf-8")
    )


def fit_message_for_tellraw(
    author: str,
    body: str,
    *,
    max_body_bytes: int = RCON_MAX_BODY_BYTES,
) -> str:
    """Return ``body`` shortened so the full ``tellraw`` command fits.

    Shortens only the literal message on Unicode code-point boundaries,
    then re-serializes. Never truncates mid-character or mid-SNBT.
    Appends an ellipsis (``…``) when the message is shortened.

    Raises:
        ValueError: If even an empty message cannot fit with ``author``
            inside ``max_body_bytes``.
    """
    if _command_bytes(author, body) <= max_body_bytes:
        return body

    # Binary-search the largest prefix of ``body`` that fits with an
    # ellipsis marker. Empty body is tried last as a hard floor.
    lo, hi = 0, len(body)
    best = ""
    while lo <= hi:
        mid = (lo + hi) // 2
        candidate = body[:mid] + ("…" if mid < len(body) else "")
        if _command_bytes(author, candidate) <= max_body_bytes:
            best = candidate
            lo = mid + 1
        else:
            hi = mid - 1

    if _command_bytes(author, best) > max_body_bytes:
        raise ValueError("tellraw command exceeds RCON body limit")
    # If even the empty message overflows (oversized author), fail.
    if best == "" and _command_bytes(author, "") > max_body_bytes:
        raise ValueError("tellraw command exceeds RCON body limit")
    return best


def build_tellraw_command(
    author: str,
    body: str,
    *,
    max_body_bytes: int = RCON_MAX_BODY_BYTES,
) -> str:
    """Build ``tellraw @a <snbt>``, shortening ``body`` if needed.

    Raises:
        ValueError: If the command cannot be made to fit.
    """
    fitted = fit_message_for_tellraw(
        author, body, max_body_bytes=max_body_bytes
    )
    return _TELLRAW_PREFIX + build_discord_chat_snbt(author, fitted)


def build_broadcast_snbt(body: str) -> str:
    """Build the SNBT list for an in-game broadcast.

    Shape (no hover, no click)::

        [
          {text:"[Broadcast] ",color:"gold"},
          {text:"<body>",color:"white"}
        ]

    ``body`` is literal text. Vanilla ``say`` is not used: an RCON
    ``say`` is rendered by Minecraft as ``[Rcon]``, which is the label
    this payload replaces.
    """
    parts = [
        snbt_text_component("[Broadcast] ", color="gold"),
        snbt_text_component(body, color="white"),
    ]
    return "[" + ",".join(parts) + "]"


def _broadcast_command_bytes(body: str) -> int:
    return len((_TELLRAW_PREFIX + build_broadcast_snbt(body)).encode("utf-8"))


def fit_broadcast_message(
    body: str,
    *,
    max_body_bytes: int = RCON_MAX_BODY_BYTES,
) -> str:
    """Return ``body`` shortened so a broadcast ``tellraw`` command fits.

    Shortens only the literal message on Unicode code-point boundaries.
    Appends an ellipsis (``…``) when the message is shortened.

    Raises:
        ValueError: If even an empty message cannot fit inside
            ``max_body_bytes``.
    """
    if _broadcast_command_bytes(body) <= max_body_bytes:
        return body

    lo, hi = 0, len(body)
    best = ""
    while lo <= hi:
        mid = (lo + hi) // 2
        candidate = body[:mid] + ("…" if mid < len(body) else "")
        if _broadcast_command_bytes(candidate) <= max_body_bytes:
            best = candidate
            lo = mid + 1
        else:
            hi = mid - 1

    if _broadcast_command_bytes(best) > max_body_bytes:
        raise ValueError("tellraw command exceeds RCON body limit")
    if best == "" and _broadcast_command_bytes("") > max_body_bytes:
        raise ValueError("tellraw command exceeds RCON body limit")
    return best


def build_broadcast_tellraw_command(
    body: str,
    *,
    max_body_bytes: int = RCON_MAX_BODY_BYTES,
) -> str:
    """Build ``tellraw @a <snbt>`` for a ``[Broadcast]`` message.

    Raises:
        ValueError: If the command cannot be made to fit.
    """
    fitted = fit_broadcast_message(body, max_body_bytes=max_body_bytes)
    return _TELLRAW_PREFIX + build_broadcast_snbt(fitted)
