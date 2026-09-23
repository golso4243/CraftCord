"""Tests for the uptime state machine in :class:`bot.cogs.status.StatusCog`.

The cog is exercised end-to-end (real ``_snapshot`` code, mocked
services) so the hysteresis, the ``list`` reachability probe, and the
DB call pattern are all covered by the same tests.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Optional, Tuple
from unittest.mock import AsyncMock, MagicMock

from bot.cogs.status import StatusCog
from bot.services.mcstatus_service import PingResult
from bot.services.rcon_service import RconError


def _fake_config(**overrides: object) -> SimpleNamespace:
    """Return a stand-in for :data:`bot.config.config` with only the
    fields ``_snapshot`` reads.
    """
    defaults = {
        "mc_host": "127.0.0.1",
        "mc_port": 25565,
        "public_address": None,
        "status_update_interval": 30,
        "status_channel_id": None,
    }
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _make_cog(
    *,
    slp_online: bool = True,
    rcon_result: Optional[str] = "There are 0 of a max of 20 players online: ",
    online_since_in_db: Optional[int] = None,
    mark_online_returns: int = 1_000_000,
) -> Tuple[StatusCog, MagicMock, MagicMock, MagicMock]:
    """Construct a StatusCog with fully mocked service dependencies.

    ``rcon_result=None`` makes the RCON command raise :class:`RconError`
    (simulates the "server up per SLP, RCON down" case). Otherwise the
    string is returned from ``rcon.command`` verbatim.
    """
    bot = MagicMock()

    db = MagicMock()
    db.mark_online = AsyncMock(return_value=mark_online_returns)
    db.mark_offline = AsyncMock()
    db.get_online_since = AsyncMock(return_value=online_since_in_db)

    mc = MagicMock()
    mc.ping = AsyncMock(return_value=PingResult(online=slp_online))

    rcon = MagicMock()
    if rcon_result is None:
        rcon.command = AsyncMock(side_effect=RconError("simulated"))
    else:
        rcon.command = AsyncMock(return_value=rcon_result)

    cog = StatusCog(bot, db, mc, rcon)
    return cog, db, mc, rcon


def test_slp_and_rcon_up_marks_online_and_resets_counter() -> None:
    cog, db, _mc, _rcon = _make_cog(slp_online=True, rcon_result="ok")
    cog._rcon_failures = 1

    snap = asyncio.run(cog._snapshot())

    db.mark_online.assert_awaited_once()
    db.mark_offline.assert_not_awaited()
    assert snap.online is True
    assert snap.online_since == 1_000_000
    assert cog._rcon_failures == 0


def test_slp_up_rcon_down_preserves_uptime_below_threshold() -> None:
    cog, db, _mc, _rcon = _make_cog(
        slp_online=True,
        rcon_result=None,
        online_since_in_db=999_999,
    )

    snap = asyncio.run(cog._snapshot())

    db.mark_online.assert_not_awaited()
    db.mark_offline.assert_not_awaited()
    assert snap.online is True
    assert snap.online_since == 999_999
    assert cog._rcon_failures == 1


def test_slp_up_rcon_down_marks_offline_at_threshold() -> None:
    cog, db, _mc, _rcon = _make_cog(
        slp_online=True,
        rcon_result=None,
        online_since_in_db=None,
    )
    cog._rcon_failures = cog._RCON_FAILURE_THRESHOLD - 1

    snap = asyncio.run(cog._snapshot())

    db.mark_offline.assert_awaited_once()
    db.mark_online.assert_not_awaited()
    assert snap.online is True
    assert snap.online_since is None
    assert cog._rcon_failures == cog._RCON_FAILURE_THRESHOLD


def test_slp_up_rcon_down_past_threshold_does_not_repeat_log_write() -> None:
    cog, db, _mc, _rcon = _make_cog(
        slp_online=True,
        rcon_result=None,
        online_since_in_db=None,
    )
    cog._rcon_failures = cog._RCON_FAILURE_THRESHOLD + 5

    snap = asyncio.run(cog._snapshot())

    db.mark_offline.assert_not_awaited()
    db.mark_online.assert_not_awaited()
    assert snap.online_since is None
    assert cog._rcon_failures == cog._RCON_FAILURE_THRESHOLD + 6


def test_slp_down_marks_offline_immediately_and_resets_counter() -> None:
    cog, db, _mc, rcon = _make_cog(slp_online=False, rcon_result="ok")
    cog._rcon_failures = 5

    snap = asyncio.run(cog._snapshot())

    rcon.command.assert_not_awaited()
    db.mark_offline.assert_awaited_once()
    db.mark_online.assert_not_awaited()
    assert snap.online is False
    assert snap.online_since is None
    assert cog._rcon_failures == 0


def test_probe_uses_list(monkeypatch) -> None:
    from bot.cogs import status as status_module

    monkeypatch.setattr(status_module, "config", _fake_config())

    cog, _db, _mc, rcon = _make_cog(
        slp_online=True,
        rcon_result="There are 3 of a max of 20 players online: alice, bob, charlie",
    )
    snap = asyncio.run(cog._snapshot())

    rcon.command.assert_awaited_once_with("list", replay_if_uncertain=True)
    assert snap.online is True
    assert snap.online_since == 1_000_000


def test_rcon_failure_threshold_unchanged() -> None:
    """Do not retune hysteresis merely to make tests easier."""
    assert StatusCog._RCON_FAILURE_THRESHOLD == 2


def test_blank_public_address_never_reveals_connection_host(
    monkeypatch,
) -> None:
    from bot.cogs import status as status_module
    from bot.utils.embeds import build_status_embed

    monkeypatch.setattr(
        status_module,
        "config",
        _fake_config(mc_host="10.0.0.9", mc_port=25565, public_address=None),
    )
    cog, _db, mc, _rcon = _make_cog(slp_online=True, rcon_result="ok")
    mc.ping = AsyncMock(
        return_value=PingResult(
            online=True,
            version="26.2",
            players_online=1,
            players_max=20,
            player_names=["Steve"],
        )
    )
    snap = asyncio.run(cog._snapshot())
    embed = build_status_embed(snap)
    names = [f.name for f in embed.fields]
    assert "Address" not in names
    blob = (embed.description or "") + "".join(f.value for f in embed.fields)
    assert "10.0.0.9" not in blob


def test_player_name_sample_is_preview_not_exhaustive_label() -> None:
    """Embed shows sample names under 'Who's online'; not 'all players'."""
    from bot.utils.embeds import StatusSnapshot, build_status_embed

    snap = StatusSnapshot(
        online=True,
        host="127.0.0.1",
        port=25565,
        players_online=50,
        players_max=100,
        player_names=["Steve", "Alex"],
        online_since=1_000_000,
    )
    embed = build_status_embed(snap)
    whos = [f for f in embed.fields if f.name == "Who's online"]
    assert len(whos) == 1
    assert "Steve" in whos[0].value
    assert "Alex" in whos[0].value
    # Field title must not claim an exhaustive roster.
    assert whos[0].name != "All players"
    assert "all players" not in whos[0].name.lower()
