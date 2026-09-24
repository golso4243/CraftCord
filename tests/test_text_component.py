"""Structural tests for Discord → Minecraft SNBT text-component serialization.

Fixtures here are **synthetic**. None were captured from a live vanilla
26.2 server. Assertions recover literal ``text`` values via the module's
restricted SNBT decoder rather than relying only on full-string snapshots.
"""
from __future__ import annotations

import pytest

from bot.utils.text_component import (
    RCON_MAX_BODY_BYTES,
    TellrawOutcome,
    build_broadcast_snbt,
    build_broadcast_tellraw_command,
    build_discord_chat_snbt,
    build_tellraw_command,
    classify_tellraw_response,
    extract_literal_texts,
    fit_broadcast_message,
    fit_message_for_tellraw,
    snbt_quote,
    snbt_unescape,
)


def _texts(snbt: str) -> list[str]:
    return [text for text, _color in extract_literal_texts(snbt)]


def test_ordinary_message_literals_round_trip() -> None:
    snbt = build_discord_chat_snbt("Alice", "hello world")
    texts = extract_literal_texts(snbt)
    assert texts == [
        ("[Discord] ", "blue"),
        ("Alice", "white"),
        (": ", "white"),
        ("hello world", "white"),
    ]
    assert "hover" not in snbt.lower()
    assert "click" not in snbt.lower()


def test_quotes_and_backslashes_stay_literal() -> None:
    author = 'Bob\\"x'
    body = r'say "hi" and \u0041 and \\'
    snbt = build_discord_chat_snbt(author, body)
    assert _texts(snbt)[1] == author
    assert _texts(snbt)[3] == body
    # User ``\u0041`` must remain backslash + u0041, not become ``A``.
    assert snbt_unescape(snbt_quote(body)) == body
    assert "A" not in _texts(snbt)[3]
    assert "\\u0041" in _texts(snbt)[3]


def test_apostrophes_stay_literal_inside_double_quotes() -> None:
    body = "it's a test"
    snbt = build_discord_chat_snbt("Carol", body)
    assert _texts(snbt)[3] == body
    # Apostrophe is not escaped; the string delimiter is double-quote.
    assert "it's a test" in snbt


def test_newlines_and_control_characters_are_escaped() -> None:
    body = "line1\nline2\t\x01\x7f"
    quoted = snbt_quote(body)
    assert "\\n" in quoted
    assert "\\t" in quoted
    assert "\\u0001" in quoted
    assert "\\u007f" in quoted
    assert "\n" not in quoted[1:-1].replace("\\n", "")
    assert snbt_unescape(quoted) == body


def test_emoji_and_non_ascii_names_pass_through() -> None:
    author = "名前🎮"
    body = "café 🎉 日本語"
    snbt = build_discord_chat_snbt(author, body)
    assert _texts(snbt)[1] == author
    assert _texts(snbt)[3] == body
    # Emoji must appear as UTF-8 text, not forced \\u escapes.
    assert "🎮" in snbt
    assert "🎉" in snbt
    assert "\\u" not in snbt_quote(author)


def test_selector_and_command_like_strings_stay_literal_text() -> None:
    body = "@a @p tellraw /op Steve"
    snbt = build_discord_chat_snbt("Dave", body)
    texts = _texts(snbt)
    assert texts[3] == body
    # The only selector in the eventual command is the fixed ``@a`` after
    # ``tellraw``; user content lives inside quoted text fields.
    assert texts[3] == "@a @p tellraw /op Steve"
    for text, _color in extract_literal_texts(snbt):
        # No component may promote user content into structure.
        assert "click_event" not in text
        assert "hover_event" not in text


def test_component_field_lookalikes_stay_literal() -> None:
    body = '{text:"injected",color:"red",hover_event:{action:show_text}}'
    snbt = build_discord_chat_snbt("Eve", body)
    assert _texts(snbt)[3] == body
    # After decode there are exactly four compounds; the lookalike is one
    # string value, not nested structure.
    assert len(extract_literal_texts(snbt)) == 4


def test_long_unicode_message_round_trips() -> None:
    body = ("漢字" * 80) + "🎉" * 20
    snbt = build_discord_chat_snbt("Frank", body)
    assert _texts(snbt)[3] == body


def test_build_tellraw_command_prefix() -> None:
    cmd = build_tellraw_command("Grace", "ping")
    assert cmd.startswith("tellraw @a ")
    payload = cmd[len("tellraw @a ") :]
    assert _texts(payload)[3] == "ping"


def test_fit_shortens_before_serialization_not_mid_snbt() -> None:
    author = "Hank"
    # Oversized ASCII body relative to a modest artificial cap that still
    # leaves room for the fixed tellraw prefix + author compound.
    body = "x" * 200
    fitted = fit_message_for_tellraw(author, body, max_body_bytes=250)
    assert len(fitted) < len(body)
    assert fitted.endswith("…")
    cmd = "tellraw @a " + build_discord_chat_snbt(author, fitted)
    assert len(cmd.encode("utf-8")) <= 250
    # Payload remains valid structured SNBT with recovered literal text.
    assert extract_literal_texts(cmd[len("tellraw @a ") :])[3][0] == fitted


def test_fit_boundary_ascii_just_under_and_over_cap() -> None:
    author = "Ivy"
    # Grow body until one more character overflows the real RCON cap.
    body = "a"
    while (
        len(("tellraw @a " + build_discord_chat_snbt(author, body)).encode("utf-8"))
        <= RCON_MAX_BODY_BYTES
    ):
        body += "a"
    over = body
    under = body[:-1]
    under_cmd = build_tellraw_command(author, under)
    assert len(under_cmd.encode("utf-8")) <= RCON_MAX_BODY_BYTES
    over_cmd = build_tellraw_command(author, over)
    assert len(over_cmd.encode("utf-8")) <= RCON_MAX_BODY_BYTES
    over_texts = _texts(over_cmd[len("tellraw @a ") :])
    assert over_texts[3].endswith("…")
    assert len(over_texts[3]) < len(over)


def test_fit_never_splits_multibyte_code_point() -> None:
    author = "Jade"
    # U+1F600 is a 4-byte UTF-8 character. Place many of them so the cut
    # lands on the character boundary under a tight byte budget.
    body = "😀" * 40
    fitted = fit_message_for_tellraw(author, body, max_body_bytes=200)
    # Round-trip through UTF-8 must succeed (no orphan surrogate / mid-byte).
    fitted.encode("utf-8")
    # Fitted string must be a clean prefix of body (plus optional ellipsis).
    core = fitted[:-1] if fitted.endswith("…") else fitted
    assert body.startswith(core)
    cmd = "tellraw @a " + build_discord_chat_snbt(author, fitted)
    assert len(cmd.encode("utf-8")) <= 200
    assert _texts(cmd[len("tellraw @a ") :])[3] == fitted


def test_fit_raises_when_author_alone_overflows() -> None:
    author = "K" * 500
    with pytest.raises(ValueError, match="RCON body limit"):
        fit_message_for_tellraw(author, "hi", max_body_bytes=80)


def test_broadcast_literals_match_discord_bracket_style() -> None:
    snbt = build_broadcast_snbt("Test Broadcast")
    assert extract_literal_texts(snbt) == [
        ("[Broadcast] ", "gold"),
        ("Test Broadcast", "white"),
    ]
    assert "hover" not in snbt.lower()
    assert "click" not in snbt.lower()
    assert "Rcon" not in snbt
    assert "RCON" not in snbt


def test_broadcast_quotes_stay_literal_text() -> None:
    body = 'say "hi" and \\'
    snbt = build_broadcast_snbt(body)
    assert _texts(snbt)[1] == body
    cmd = build_broadcast_tellraw_command(body)
    assert cmd.startswith("tellraw @a ")
    assert not cmd.startswith("say ")
    assert _texts(cmd[len("tellraw @a ") :])[1] == body


def test_broadcast_fit_shortens_on_code_point_boundary() -> None:
    body = "😀" * 40
    fitted = fit_broadcast_message(body, max_body_bytes=180)
    fitted.encode("utf-8")
    core = fitted[:-1] if fitted.endswith("…") else fitted
    assert body.startswith(core)
    cmd = build_broadcast_tellraw_command(fitted, max_body_bytes=180)
    assert len(cmd.encode("utf-8")) <= 180
    assert _texts(cmd[len("tellraw @a ") :])[0] == "[Broadcast] "


def test_classify_empty_is_accepted_no_output() -> None:
    assert classify_tellraw_response("") is TellrawOutcome.ACCEPTED_NO_OUTPUT
    assert classify_tellraw_response("   ") is TellrawOutcome.ACCEPTED_NO_OUTPUT
    assert classify_tellraw_response("§a") is TellrawOutcome.ACCEPTED_NO_OUTPUT


def test_classify_known_command_rejection() -> None:
    assert (
        classify_tellraw_response("Incorrect argument for command")
        is TellrawOutcome.COMMAND_REJECTED
    )
    assert (
        classify_tellraw_response(
            "Unknown or incomplete command, see below for error"
        )
        is TellrawOutcome.COMMAND_REJECTED
    )


def test_classify_no_player_was_found() -> None:
    assert (
        classify_tellraw_response("No player was found")
        is TellrawOutcome.NO_PLAYERS
    )


def test_classify_unknown_non_empty_is_not_rejection() -> None:
    assert (
        classify_tellraw_response("Some unexpected plugin chatter")
        is TellrawOutcome.UNKNOWN_OUTPUT
    )
