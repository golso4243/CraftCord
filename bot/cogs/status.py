"""
Live server status embed.

Two responsibilities live in this cog:

1. A background ``tasks.loop`` that refreshes a single pinned embed in the
   configured status channel. The pinned-message pattern means members
   always see the latest snapshot without the channel filling up with
   duplicates.
2. A public ``/status`` slash command that replies with an ad-hoc snapshot.

Pinned-message lifecycle
------------------------
We persist the message id in the database so the bot can find and edit the
same message across restarts:

1. Load the stored message id from ``settings``.
2. If found, fetch the message and edit it in place.
3. Otherwise, post a fresh message, pin it, and store the new id.

If the stored message was deleted manually we transparently fall through
and create a new one.

Data sources
------------
* Basic liveness and player count come from ``McStatusService`` (SLP ping).
* Uptime is gated on **RCON reachability** via the vanilla ``list``
  command, not SLP alone. Managed hosts often front the game port with a
  proxy that keeps answering SLP with a stub while the underlying MC
  process is restarting. This is reachability-based uptime, not an exact
  measurement of Minecraft process uptime. See
  :py:meth:`StatusCog._snapshot` for the hysteresis logic.
"""
from __future__ import annotations

import logging
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks

from bot.config import config
from bot.database.db import Database
from bot.services.mcstatus_service import McStatusService
from bot.services.rcon_service import RconError, RconService
from bot.utils.destinations import resolve_guild_text_channel
from bot.utils.embeds import StatusSnapshot, build_status_embed
from bot.utils.permissions import require_member

log = logging.getLogger(__name__)

# Key under which we persist the pinned status message id in the settings
# table. Centralised here so it's easy to rename or clear manually.
_STATUS_MESSAGE_KEY = "status_message_id"

_NO_MENTIONS = discord.AllowedMentions.none()


class StatusCog(commands.Cog):
    """Publish and maintain the pinned server-status embed."""

    # Number of consecutive RCON failures we tolerate (while SLP still
    # reports the server online) before resetting the persisted uptime.
    # At the default 30 s refresh interval, 2 failures ≈ 1 minute of RCON
    # unreachability — long enough to survive a transient blip, short
    # enough to catch a real restart quickly. Tuned via _snapshot's
    # hysteresis logic; see that method's docstring.
    _RCON_FAILURE_THRESHOLD = 2

    def __init__(
        self,
        bot: commands.Bot,
        db: Database,
        mc: McStatusService,
        rcon: RconService,
    ):
        self.bot = bot
        self.db = db
        self.mc = mc
        self.rcon = rcon
        # Rolling count of RCON failures observed *while SLP is still
        # reporting online*. Reset to zero on every RCON success. Drives
        # the hysteresis in :py:meth:`_snapshot`.
        self._rcon_failures = 0
        # The loop's interval is a user-configurable value, so we override
        # the default declared on the decorator before the loop starts.
        self.update_status.change_interval(seconds=config.status_update_interval)

    async def cog_load(self) -> None:
        self.update_status.start()

    async def cog_unload(self) -> None:
        self.update_status.cancel()

    # ── public command ────────────────────────────────────────────
    @app_commands.command(name="status", description="Show current server status.")
    @require_member()
    async def status_cmd(self, interaction: discord.Interaction) -> None:
        """Reply with a one-off status snapshot.

        Unlike the background loop this does not touch the pinned message —
        it's a transient response in whatever channel the user invoked it.
        """
        await interaction.response.defer(thinking=True)
        snap = await self._snapshot()
        await interaction.followup.send(
            embed=build_status_embed(snap), allowed_mentions=_NO_MENTIONS
        )

    # ── background updater ───────────────────────────────────────
    # The interval on the decorator is a placeholder; the real value is set
    # in __init__ from config. We still have to declare one here because
    # tasks.loop requires a non-zero default.
    @tasks.loop(seconds=30)
    async def update_status(self) -> None:
        """Refresh the pinned status embed. Runs forever on a fixed cadence."""
        if config.status_channel_id is None:
            return
        channel = await resolve_guild_text_channel(
            self.bot,
            config.status_channel_id,
            setting_name="STATUS_CHANNEL_ID",
        )
        if channel is None:
            return

        snap = await self._snapshot()
        embed = build_status_embed(snap)
        try:
            await self._upsert_pinned(channel, embed)
        except discord.DiscordException as e:
            # Any other Discord failure is logged and ignored so the loop
            # keeps ticking — a transient 5xx shouldn't stop future updates.
            log.warning("Failed to update status message: %s", type(e).__name__)

    @update_status.before_loop
    async def _before_update(self) -> None:
        # Wait for the gateway so the very first iteration doesn't race
        # against cache population.
        await self.bot.wait_until_ready()

    # ── helpers ──────────────────────────────────────────────────
    async def _snapshot(self) -> StatusSnapshot:
        """Collect a fresh status reading from all data sources.

        Mutates state on the database side to keep the persisted
        ``online_since`` timestamp aligned with our best understanding
        of whether the MC server process is genuinely up.

        Uptime state machine
        --------------------
        Two signals contribute:

        * **SLP** — SLP reachability drives the user-facing "Server
          Online" title. It can be spoofed by managed-host proxies
          during a real MC restart, so we don't trust it alone for
          uptime accounting.
        * **RCON** — Reaching RCON *proves* the MC process is up
          (proxies never fake RCON). The vanilla ``list`` command is
          the liveness probe.

        The DB is updated as follows:

        ==================  ==================  ==============================
        SLP                 RCON                Action
        ==================  ==================  ==============================
        online              ok                  ``mark_online`` (idempotent);
                                                clear failure counter.
        online              failed, count <     Increment counter; preserve
                            threshold           existing ``online_since``.
        online              failed, count ==    Log once; ``mark_offline``.
                            threshold           ``online_since`` → ``None``.
        offline (SLP down)  n/a (not probed)    ``mark_offline`` immediately;
                                                counter reset.
        ==================  ==================  ==============================

        The hysteresis means a genuine restart shows ``Uptime: —`` for
        roughly one minute before ticking back up from zero — the
        honest "we don't know" signal — while transient RCON hiccups
        that recover within a minute don't perturb the counter.
        """
        ping = await self.mc.ping()

        # Probe RCON when SLP says the server exists. ``list`` is a
        # dirt-cheap vanilla heartbeat.
        rcon_up = False
        if ping.online:
            try:
                await self.rcon.command("list", replay_if_uncertain=True)
                rcon_up = True
            except RconError:
                # Failure handled by the hysteresis block below.
                pass

        # Uptime state transitions.
        online_since: Optional[int] = None
        if rcon_up:
            # Fresh successful probe: clear the counter and (re-)mark
            # online. ``mark_online`` returns the earliest observed
            # timestamp — either preserved from a previous tick or
            # freshly assigned if we just came back from offline.
            self._rcon_failures = 0
            online_since = await self.db.mark_online()
        elif ping.online:
            # SLP up, RCON down: could be a transient blip or a real
            # restart hiding behind a SLP proxy. Only reset uptime
            # after we cross the failure threshold.
            self._rcon_failures += 1
            if self._rcon_failures == self._RCON_FAILURE_THRESHOLD:
                # Log at INFO because this is expected during real
                # restarts. We log exactly once per crossing — the
                # equality check skips repeats while RCON stays down.
                log.info(
                    "RCON unreachable for %d consecutive refreshes while SLP "
                    "still reports the server online — resetting server "
                    "uptime. Expected if the MC server is restarting; "
                    "otherwise check RCON health.",
                    self._rcon_failures,
                )
                await self.db.mark_offline()
            # Preserve the existing ``online_since`` value below the
            # threshold, and reflect the DB (now null) once past it.
            online_since = await self.db.get_online_since()
        else:
            # SLP itself has gone silent — definitively offline from
            # the bot's perspective. No hysteresis needed: SLP failing
            # is already a strong, low-false-positive signal.
            self._rcon_failures = 0
            await self.db.mark_offline()

        return StatusSnapshot(
            online=ping.online,
            host=config.mc_host,
            port=config.mc_port,
            public_address=config.public_address,
            version=ping.version,
            motd=ping.motd,
            players_online=ping.players_online,
            players_max=ping.players_max,
            player_names=ping.player_names,
            latency_ms=ping.latency_ms,
            online_since=online_since,
        )

    async def _upsert_pinned(
        self, channel: discord.TextChannel, embed: discord.Embed
    ) -> None:
        """Edit the existing pinned status message, or create a new one.

        Handles the three realistic failure modes:

        * The stored id exists but the message was deleted (``NotFound``):
          fall through and post a new one.
        * We lack Manage Messages (``Forbidden``) when editing: log and
          abort — there's nothing we can do until permissions are fixed.
        * We lack Manage Messages when pinning: log and continue — the
          message is still published, just not pinned.
        """
        stored = await self.db.get_setting(_STATUS_MESSAGE_KEY)
        if stored:
            try:
                msg = await channel.fetch_message(int(stored))
                # Only edit messages that still live in the configured
                # guild channel we already validated above.
                await msg.edit(embed=embed, allowed_mentions=_NO_MENTIONS)
                return
            except discord.NotFound:
                log.info("Stored status message missing, creating a new one.")
            except discord.Forbidden:
                log.warning("Missing permissions to edit status message.")
                return

        msg = await channel.send(embed=embed, allowed_mentions=_NO_MENTIONS)
        try:
            await msg.pin(reason="CraftCord server-status embed")
        except discord.Forbidden:
            log.warning("Cannot pin status message (missing Manage Messages).")
        await self.db.set_setting(_STATUS_MESSAGE_KEY, str(msg.id))


async def setup(bot: commands.Bot) -> None:
    """discord.py extension entry point — called by ``bot.load_extension``.

    Pulls the shared ``Database``, ``McStatusService`` and ``RconService``
    instances off the bot, where they are attached during startup in
    ``bot.py``.
    """
    await bot.add_cog(
        StatusCog(bot, bot.db, bot.mc, bot.rcon)  # type: ignore[attr-defined]
    )
