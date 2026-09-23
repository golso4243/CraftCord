"""
Role-based permission checks for slash commands and other bot features.

Discord has two orthogonal notions of "permission" that this module
bridges:

1. **Configured guild boundary** — every privileged check first verifies
   the member belongs to :data:`config.guild_id`. Owners and Discord
   Administrators in *other* guilds never gain access.
2. **Discord-native permissions** — server owner, ``Administrator`` flag
   *within the configured guild*. These always win so staff never lock
   themselves out of their own tools.
3. **Role-id configuration** — admin / mod / member role ids configured
   via environment variables. These let a server opt into fine-grained
   gating without giving every mod the full Administrator bit.

The :func:`has_admin_access`, :func:`has_mod_access`, and
:func:`has_member_access` helpers are reusable outside slash-command
checks — for example, the Discord -> Minecraft chat bridge listener
gates on :func:`has_member_access`.

Usage from a cog::

    @app_commands.command(...)
    @require_admin()
    async def my_command(self, interaction): ...

When a check fails we raise :class:`app_commands.CheckFailure`, which
discord.py surfaces to the user via the interaction error handler as a
polite ephemeral message.
"""
from __future__ import annotations

from typing import Callable, Optional, Union

import discord
from discord import app_commands

from bot.config import config

# Short denial used for both the tree-wide guild check and role checks.
# Must not include guild ids, hosts, or other configuration values.
GUILD_DENIED_MSG = "This bot only works in its configured Discord server."
PERMISSION_DENIED_MSG = "You don't have permission to use this command."
DM_DENIED_MSG = "This command must be used in a server."


def is_configured_guild(
    guild: Optional[Union[discord.Guild, discord.Object, int]],
) -> bool:
    """True when ``guild`` is the installation's configured Discord guild."""
    if guild is None:
        return False
    guild_id = guild if isinstance(guild, int) else guild.id
    return guild_id == config.guild_id


def member_in_configured_guild(member: discord.Member) -> bool:
    """True when ``member`` belongs to the configured guild."""
    return is_configured_guild(member.guild)


def is_privileged_member(member: discord.Member) -> bool:
    """True for the configured-guild owner or a Discord Administrator there.

    Privilege checks never apply outside the configured guild — an owner
    or Administrator of another server must not bypass role gates here.
    """
    if not member_in_configured_guild(member):
        return False
    if member.guild.owner_id == member.id:
        return True
    return member.guild_permissions.administrator


def has_any_configured_role(
    member: discord.Member, *role_ids: Optional[int]
) -> bool:
    """Return ``True`` if ``member`` holds any of the given role ids.

    ``None`` entries are ignored so callers can pass config values that
    might be unset. The member must still be in the configured guild —
    a matching role id in a different guild does not count.
    """
    if not member_in_configured_guild(member):
        return False
    member_role_ids = {r.id for r in member.roles}
    return any(rid in member_role_ids for rid in role_ids if rid is not None)


def has_admin_access(member: discord.Member) -> bool:
    """True for privileged members or holders of the configured admin role."""
    if not member_in_configured_guild(member):
        return False
    return is_privileged_member(member) or has_any_configured_role(
        member, config.admin_role_id
    )


def has_mod_access(member: discord.Member) -> bool:
    """True for privileged members or holders of admin/mod roles."""
    if not member_in_configured_guild(member):
        return False
    return is_privileged_member(member) or has_any_configured_role(
        member, config.admin_role_id, config.mod_role_id
    )


def has_member_access(member: discord.Member) -> bool:
    """True for privileged members or holders of any configured role."""
    if not member_in_configured_guild(member):
        return False
    return is_privileged_member(member) or has_any_configured_role(
        member, config.admin_role_id, config.mod_role_id, config.member_role_id
    )


def require_configured_guild(interaction: discord.Interaction) -> bool:
    """Raise :class:`app_commands.CheckFailure` unless the interaction is
    in the configured guild.

    Used as the tree-wide ``interaction_check`` so ``/ping``, ``/botinfo``,
    and other ungated commands still cannot run from DMs or foreign guilds.
    """
    if interaction.guild is None or not is_configured_guild(interaction.guild):
        raise app_commands.CheckFailure(GUILD_DENIED_MSG)
    return True


def _make_access_check(
    access_fn: Callable[[discord.Member], bool],
) -> Callable[[discord.Interaction], bool]:
    """Build an ``app_commands.check`` that delegates to ``access_fn``."""

    async def predicate(interaction: discord.Interaction) -> bool:
        member = interaction.user
        # DMs yield a User without roles / guild membership. Require a
        # Member-shaped author in a guild context before role checks.
        if interaction.guild is None or not hasattr(member, "roles"):
            raise app_commands.CheckFailure(DM_DENIED_MSG)
        # Guild boundary runs before privilege / role bypasses.
        if not member_in_configured_guild(member):  # type: ignore[arg-type]
            raise app_commands.CheckFailure(GUILD_DENIED_MSG)
        if access_fn(member):  # type: ignore[arg-type]
            return True
        raise app_commands.CheckFailure(PERMISSION_DENIED_MSG)

    return app_commands.check(predicate)


def require_admin():
    """Restrict a command to admins (or server owner / Discord Administrator)."""
    return _make_access_check(has_admin_access)


def require_mod():
    """Restrict a command to admins and mods."""
    return _make_access_check(has_mod_access)


def require_member():
    """Restrict a command to any configured role (admin, mod, or member).

    Useful for read-only commands like ``/list`` and ``/status`` that
    should be available to the general community but still gated behind
    membership.
    """
    return _make_access_check(has_member_access)
