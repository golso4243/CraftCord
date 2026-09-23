"""Tests for bot.utils.permissions (including guild boundary)."""
from __future__ import annotations

from dataclasses import dataclass, replace

import pytest

import bot.utils.permissions as perms
from bot.utils.permissions import (
    has_admin_access,
    has_any_configured_role,
    has_member_access,
    has_mod_access,
    is_configured_guild,
    is_privileged_member,
    member_in_configured_guild,
)

CONFIGURED_GUILD = 111111111111111111
OTHER_GUILD = 222222222222222222


@dataclass
class FakeRole:
    id: int


class FakePermissions:
    def __init__(self, administrator: bool = False) -> None:
        self.administrator = administrator


class FakeGuild:
    def __init__(self, guild_id: int, owner_id: int) -> None:
        self.id = guild_id
        self.owner_id = owner_id


class FakeMember:
    def __init__(
        self,
        member_id: int,
        roles: list[FakeRole],
        guild: FakeGuild,
        administrator: bool = False,
    ) -> None:
        self.id = member_id
        self.roles = roles
        self.guild = guild
        self.guild_permissions = FakePermissions(administrator)


@pytest.fixture
def role_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        perms,
        "config",
        replace(
            perms.config,
            guild_id=CONFIGURED_GUILD,
            admin_role_id=100,
            mod_role_id=200,
            member_role_id=300,
        ),
    )


def _member(
    member_id: int = 1,
    role_ids: list[int] | None = None,
    owner_id: int = 999,
    administrator: bool = False,
    guild_id: int = CONFIGURED_GUILD,
) -> FakeMember:
    roles = [FakeRole(rid) for rid in (role_ids or [])]
    return FakeMember(
        member_id, roles, FakeGuild(guild_id, owner_id), administrator
    )


def test_is_configured_guild(role_config: None) -> None:
    assert is_configured_guild(CONFIGURED_GUILD) is True
    assert is_configured_guild(OTHER_GUILD) is False
    assert is_configured_guild(None) is False


def test_is_privileged_member_owner(role_config: None) -> None:
    member = _member(member_id=42, owner_id=42)
    assert is_privileged_member(member) is True


def test_is_privileged_member_administrator(role_config: None) -> None:
    member = _member(administrator=True)
    assert is_privileged_member(member) is True


def test_wrong_guild_administrator_denied(role_config: None) -> None:
    member = _member(administrator=True, guild_id=OTHER_GUILD)
    assert member_in_configured_guild(member) is False
    assert is_privileged_member(member) is False
    assert has_admin_access(member) is False
    assert has_mod_access(member) is False
    assert has_member_access(member) is False


def test_wrong_guild_owner_denied(role_config: None) -> None:
    member = _member(member_id=42, owner_id=42, guild_id=OTHER_GUILD)
    assert is_privileged_member(member) is False
    assert has_admin_access(member) is False


def test_matching_role_in_wrong_guild_denied(role_config: None) -> None:
    member = _member(role_ids=[100], guild_id=OTHER_GUILD)
    assert has_any_configured_role(member, 100) is False
    assert has_admin_access(member) is False
    assert has_member_access(member) is False


def test_has_any_configured_role_ignores_none(role_config: None) -> None:
    member = _member(role_ids=[200])
    assert has_any_configured_role(member, None, 200, None) is True
    assert has_any_configured_role(member, None, None) is False


def test_has_admin_access_with_admin_role(role_config: None) -> None:
    member = _member(role_ids=[100])
    assert has_admin_access(member) is True
    assert has_mod_access(member) is True
    assert has_member_access(member) is True


def test_has_mod_access_with_mod_role_only(role_config: None) -> None:
    member = _member(role_ids=[200])
    assert has_admin_access(member) is False
    assert has_mod_access(member) is True
    assert has_member_access(member) is True


def test_has_member_access_with_member_role_only(role_config: None) -> None:
    member = _member(role_ids=[300])
    assert has_admin_access(member) is False
    assert has_mod_access(member) is False
    assert has_member_access(member) is True


def test_no_roles_no_access(role_config: None) -> None:
    member = _member()
    assert has_admin_access(member) is False
    assert has_mod_access(member) is False
    assert has_member_access(member) is False
