"""Tests for bot.utils.formatting helpers."""
from __future__ import annotations

from bot.utils.formatting import (
    clean_mc_text,
    escape_minecraft_username,
    humanize_uptime,
    neutralize_code_fences,
)


def test_humanize_uptime_minutes() -> None:
    assert humanize_uptime(125) == "2m"


def test_humanize_uptime_hours_and_minutes() -> None:
    assert humanize_uptime(3723) == "1h 2m"


def test_humanize_uptime_days() -> None:
    assert humanize_uptime(90061) == "1d 1h 1m"


def test_humanize_uptime_seconds_only() -> None:
    assert humanize_uptime(45) == "45s"


def test_humanize_uptime_negative_is_zero() -> None:
    assert humanize_uptime(-5) == "0s"


def test_clean_mc_text_strips_section_codes() -> None:
    assert clean_mc_text("§aHello §rWorld") == "Hello World"


def test_clean_mc_text_strips_ansi() -> None:
    assert clean_mc_text("\x1b[32mGreen\x1b[0m") == "Green"


def test_neutralize_code_fences() -> None:
    out = neutralize_code_fences("before ``` after")
    assert "```" not in out
    assert "\u200b" in out


def test_escape_minecraft_username_escapes_underscore() -> None:
    assert escape_minecraft_username("Steve_123") == "Steve\\_123"
