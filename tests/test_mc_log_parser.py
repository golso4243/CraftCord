"""Tests for bot.utils.mc_log_parser.

All fixtures in this file are **synthetic**. None were captured from a
live vanilla Minecraft Java Edition 26.2 server. Phrases match the
English vanilla baseline the parser was written against; live 26.2
confirmation is a separate verification step.
"""
from __future__ import annotations

from bot.utils.mc_log_parser import (
    AdvancementEvent,
    ChatMessage,
    DeathEvent,
    JoinEvent,
    LeaveEvent,
    parse_line,
)

_PREFIX = "[12:00:00] [Server thread/INFO]: "


def test_parse_vanilla_chat() -> None:
    result = parse_line(_PREFIX + "<Steve> hello")
    assert isinstance(result, ChatMessage)
    assert result.player == "Steve"
    assert result.message == "hello"


def test_parse_join_event() -> None:
    result = parse_line(_PREFIX + "Steve joined the game")
    assert isinstance(result, JoinEvent)
    assert result.player == "Steve"


def test_parse_leave_event() -> None:
    result = parse_line(_PREFIX + "Steve left the game")
    assert isinstance(result, LeaveEvent)
    assert result.player == "Steve"


def test_parse_death_event() -> None:
    result = parse_line(_PREFIX + "Steve was slain by Zombie")
    assert isinstance(result, DeathEvent)
    assert result.player == "Steve"
    assert result.message == "Steve was slain by Zombie"


def test_parse_advancement_event() -> None:
    result = parse_line(
        _PREFIX + "Steve has made the advancement [Stone Age]"
    )
    assert isinstance(result, AdvancementEvent)
    assert result.player == "Steve"
    assert result.kind == "advancement"
    assert result.name == "Stone Age"


def test_parse_challenge_event() -> None:
    result = parse_line(
        _PREFIX + "Steve has completed the challenge [Cover Me in Debris]"
    )
    assert isinstance(result, AdvancementEvent)
    assert result.kind == "challenge"
    assert result.name == "Cover Me in Debris"


def test_parse_goal_event() -> None:
    result = parse_line(
        _PREFIX + "Steve has reached the goal [Sky's the Limit]"
    )
    assert isinstance(result, AdvancementEvent)
    assert result.kind == "goal"
    assert result.name == "Sky's the Limit"


def test_styled_chat_is_not_a_special_event() -> None:
    assert parse_line(_PREFIX + "[VIP] Steve » hi there") is None


def test_styled_join_is_not_a_special_event() -> None:
    assert parse_line(_PREFIX + "» Steve entered ExampleServer.") is None


def test_staff_alert_is_not_a_special_event() -> None:
    assert (
        parse_line(
            _PREFIX + '[STAFF_ALERT] sender="Steve" message="test alert"'
        )
        is None
    )


def test_parse_generic_console_line_returns_none() -> None:
    assert parse_line(_PREFIX + "[Server] Starting minecraft server") is None


def test_chat_containing_joined_the_game_stays_chat() -> None:
    """Synthetic: ordinary chat must not forge a join event."""
    result = parse_line(_PREFIX + "<Steve> Alice joined the game")
    assert isinstance(result, ChatMessage)
    assert result.player == "Steve"
    assert result.message == "Alice joined the game"


def test_chat_containing_advancement_phrase_stays_chat() -> None:
    result = parse_line(
        _PREFIX + "<Steve> has made the advancement [Stone Age]"
    )
    assert isinstance(result, ChatMessage)
    assert result.message == "has made the advancement [Stone Age]"


def test_chat_containing_staff_alert_stays_chat() -> None:
    result = parse_line(_PREFIX + "<Steve> [STAFF_ALERT] hello")
    assert isinstance(result, ChatMessage)
    assert result.message == "[STAFF_ALERT] hello"


def test_login_address_line_is_not_a_player_event() -> None:
    """Synthetic vanilla-style login line; must stay unclassified."""
    assert (
        parse_line(
            _PREFIX
            + "Steve[/127.0.0.1:54321] logged in with entity id 12 at (0.0, 64.0, 0.0)"
        )
        is None
    )


def test_uuid_of_player_line_is_not_a_player_event() -> None:
    assert (
        parse_line(
            _PREFIX
            + "UUID of player Steve is 11111111-2222-3333-4444-555555555555"
        )
        is None
    )


def test_unknown_death_line_fails_gracefully() -> None:
    """Death coverage is curated, not exhaustive. Unknown verbs → None."""
    assert parse_line(_PREFIX + "Steve spontaneously combusted mysteriously") is None
    assert parse_line(_PREFIX + "Steve was yeeted into the void by Herobrine") is None
