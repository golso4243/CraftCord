"""
Classify lines emitted by a Minecraft server's ``latest.log``.

The console cog used to mirror every line verbatim into a single Discord
channel. This module lets us recognise *what* a line actually represents
(player chat, join/leave, death, advancement) so the cog can route each
category to a dedicated channel and render it nicely (player chat as
``**Player**: text``, events as embeds) instead of dumping everything
as a code block.

Design notes
------------
* **Pure and stateless.** Every function takes a string and returns a
  value — no I/O, no module-level mutable state — so the parser is
  trivially unit-testable and safe to call from any task.
* **Vanilla English baseline.** Patterns match the phrases vanilla
  Java Edition emits in English after the last ``]:``. Altered formats
  from mods or plugins are outside the initial supported scope.
* **Fail-soft.** ``parse_line`` returns ``None`` for anything it
  doesn't recognise; the caller treats those as generic console noise.
  We deliberately err on the side of "don't classify" — a false
  positive (misrouting a console line as chat) is much more annoying
  than missing an exotic death message. Death coverage is curated, not
  exhaustive, and is not verified against every Minecraft release.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal, Optional, Union

# Minecraft player names are 1–16 chars of ``[A-Za-z0-9_]``. Anchoring on
# this lets us avoid matching arbitrary text (e.g. a plugin tag) as a
# player name.
_NAME = r"[A-Za-z0-9_]{1,16}"

# Every line we care about has the same "``…]: <payload>``" shape after
# the server's timestamp/thread prefix. We only ever care about the
# payload, so the shared lead-in is pulled out into a fragment.
_PREFIX = r"(?:^|.*?\]:\s*)"


# ─── Parsed line variants ────────────────────────────────────────────


@dataclass(frozen=True)
class ChatMessage:
    """A regular in-game chat message (``<Player> text``)."""

    player: str
    message: str


@dataclass(frozen=True)
class JoinEvent:
    """A player connected to the server (``X joined the game``)."""

    player: str


@dataclass(frozen=True)
class LeaveEvent:
    """A player disconnected from the server (``X left the game``)."""

    player: str


@dataclass(frozen=True)
class DeathEvent:
    """A player died.

    ``message`` is the full vanilla death message (e.g.
    ``"Steve was slain by Zombie"``) so the renderer can show the
    flavour text without having to recreate it from ``player`` alone.
    """

    player: str
    message: str


@dataclass(frozen=True)
class AdvancementEvent:
    """A player earned an advancement / challenge / goal.

    ``kind`` mirrors the three sub-types vanilla emits so the renderer
    can pick an appropriate label and colour.
    """

    player: str
    kind: Literal["advancement", "challenge", "goal"]
    name: str


ParsedLine = Union[
    ChatMessage,
    JoinEvent,
    LeaveEvent,
    DeathEvent,
    AdvancementEvent,
]


# ─── Regex table ─────────────────────────────────────────────────────

# Vanilla chat: "<Name> text".
_CHAT_RE = re.compile(_PREFIX + rf"<({_NAME})>\s*(.*)$")

_JOIN_RE = re.compile(_PREFIX + rf"({_NAME}) joined the game\s*$")
_LEAVE_RE = re.compile(_PREFIX + rf"({_NAME}) left the game\s*$")

# Vanilla advancement lines have three canonical phrasings. Capture the
# phrase so the renderer can pick the right label without re-matching.
_ADVANCEMENT_RE = re.compile(
    _PREFIX
    + rf"({_NAME}) has (made the advancement|completed the challenge|reached the goal) "
    + r"\[([^\]]+)\]\s*$"
)

# Death messages are a curated alternation of the verb phrases vanilla
# Minecraft uses. This is not exhaustive — new release messages and
# datapack additions will fall through — but it covers common English
# death lines the bot was written against.
_DEATH_VERBS = (
    r"was (?:shot|pricked|slain|fireballed|stung|killed|squished|impaled|"
    r"blown up|eaten|struck|frozen|doomed|poked|roasted|burned|pummeled|"
    r"squashed|skewered|shot by a skull|killed by magic|killed by "
    r"\[.+?\]|obliterated|crushed|finished off)"
    r"|drowned|suffocated|starved to death|froze to death|died|blew up|"
    r"withered away|experienced kinetic energy|"
    r"hit the ground too hard|went up in flames|burned to death|"
    r"tried to swim in lava|walked into fire|walked into a cactus|"
    r"walked into danger zone|fell (?:from|off|out of|into|too far)|"
    r"went off with a bang|discovered the floor was lava|"
    r"didn't want to live|fell into a patch"
)
_DEATH_RE = re.compile(
    _PREFIX + rf"(?P<player>{_NAME})\s+(?P<verb>(?:{_DEATH_VERBS}).*)$"
)


# ─── Public API ──────────────────────────────────────────────────────


def parse_line(line: str) -> Optional[ParsedLine]:
    """Classify a single cleaned log line.

    Expects ``line`` to have already been run through
    :func:`bot.utils.formatting.clean_mc_text` — i.e. no colour codes,
    no ANSI escapes, no trailing whitespace. The full line (including
    the ``[timestamp] [thread/LEVEL]:`` prefix) is fine; patterns are
    anchored after the last ``]:``.

    Match order matters because death messages start with the same
    "player name + verb" shape as chat. Chat is checked first (it
    requires the literal ``<>`` wrapping) and structural events
    (join/leave/advancement) are checked before deaths because they
    have more specific terminators.

    Returns:
        A :class:`ParsedLine` subclass if the line fits a known
        category, or ``None`` to signal "this is generic console
        output, route it accordingly".
    """
    m = _CHAT_RE.search(line)
    if m:
        message = m.group(2).strip()
        if message:
            return ChatMessage(player=m.group(1), message=message)

    m = _JOIN_RE.search(line)
    if m:
        return JoinEvent(player=m.group(1))

    m = _LEAVE_RE.search(line)
    if m:
        return LeaveEvent(player=m.group(1))

    m = _ADVANCEMENT_RE.search(line)
    if m:
        phrase = m.group(2)
        kind: Literal["advancement", "challenge", "goal"]
        if "challenge" in phrase:
            kind = "challenge"
        elif "goal" in phrase:
            kind = "goal"
        else:
            kind = "advancement"
        return AdvancementEvent(player=m.group(1), kind=kind, name=m.group(3))

    m = _DEATH_RE.search(line)
    if m:
        return DeathEvent(
            player=m.group("player"),
            message=f"{m.group('player')} {m.group('verb')}".rstrip("."),
        )

    return None
