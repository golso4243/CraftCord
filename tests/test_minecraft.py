"""Tests for bot.utils.minecraft."""
from __future__ import annotations

import pytest

from bot.utils.minecraft import (
    INVALID_MINECRAFT_USERNAME_MSG,
    is_valid_minecraft_username,
    normalize_minecraft_username,
    sanitize_moderation_reason,
)


@pytest.mark.parametrize(
    "name",
    ["Steve", "Player_123", "abc123"],
)
def test_valid_usernames(name: str) -> None:
    assert is_valid_minecraft_username(name)
    assert normalize_minecraft_username(name) == name


def test_normalize_trims_whitespace() -> None:
    assert normalize_minecraft_username("  Steve  ") == "Steve"


@pytest.mark.parametrize(
    "name",
    ["", "   ", "Steve Jr", ";stop", "a/b", "@user", '"', "abc\n123"],
)
def test_invalid_usernames(name: str) -> None:
    assert not is_valid_minecraft_username(name.strip() if name != "   " else name)
    with pytest.raises(ValueError, match=INVALID_MINECRAFT_USERNAME_MSG):
        normalize_minecraft_username(name)


def test_username_too_long() -> None:
    name = "a" * 17
    assert not is_valid_minecraft_username(name)
    with pytest.raises(ValueError, match=INVALID_MINECRAFT_USERNAME_MSG):
        normalize_minecraft_username(name)


def test_sanitize_reason_collapses_newlines() -> None:
    assert sanitize_moderation_reason("  hello\nworld  ") == "hello world"


def test_sanitize_reason_collapses_control_characters() -> None:
    assert sanitize_moderation_reason("bad\x00actor\x1fhere") == "bad actor here"


def test_sanitize_reason_truncates_long_input() -> None:
    reason = "x" * 250
    assert len(sanitize_moderation_reason(reason)) == 200


def test_sanitize_reason_preserves_punctuation() -> None:
    assert sanitize_moderation_reason("Banned: griefing!") == "Banned: griefing!"
