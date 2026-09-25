"""
Custom :class:`discord.ext.commands.Bot` subclass that wires up
CraftCord's shared services and loads cogs based on configuration.

Responsibilities in order of startup:

1. Construct intents and the underlying :class:`commands.Bot`.
   The privileged ``message_content`` intent is requested only when
   ``CHAT_CHANNEL_ID`` is set (see :data:`bot.config.config`).
2. Build the shared service instances (:class:`Database`,
   :class:`RconService`, :class:`McStatusService`) and attach them to
   the bot so cogs can reach them via ``self.bot.<name>``.
3. In :py:meth:`setup_hook`, connect to the database, load core cogs,
   optionally load the chat bridge cog when configured, install the
   global slash-command error handler, and sync the command tree to
   the configured guild only.
4. On shutdown, close the RCON and database handles before letting the
   parent ``close()`` tear down the Discord connection.

The bot is entirely driven by the :data:`bot.config.config` singleton —
nothing here reads ``os.environ`` directly.
"""
from __future__ import annotations

import logging
import time

import discord
from discord import app_commands
from discord.ext import commands

from bot.config import config
from bot.database.db import Database
from bot.services.mcstatus_service import McStatusService
from bot.services.player_identity import PlayerIdentityService
from bot.services.rcon_service import RconError, RconService
from bot.utils.permissions import require_configured_guild

log = logging.getLogger(__name__)

# Core cog load order. Admin first so its commands are registered even
# if a later cog fails to load, which keeps /sync available for recovery.
# The chat bridge cog (bot.cogs.chat) is loaded separately in
# setup_hook when CHAT_CHANNEL_ID is configured.
_COGS = [
    "bot.cogs.admin",
    "bot.cogs.rcon",
    "bot.cogs.status",
    "bot.cogs.console",
]

_CHAT_COG = "bot.cogs.chat"

_NO_MENTIONS = discord.AllowedMentions.none()


class GuildBoundCommandTree(app_commands.CommandTree):
    """Command tree that enforces the configured-guild boundary first.

    ``interaction_check`` runs before per-command permission decorators,
    so ``/ping`` and ``/botinfo`` (which have no role check) still cannot
    be used from DMs or a foreign guild. CheckFailure is delivered to
    :meth:`CraftCordBot._on_app_command_error` by discord.py.
    """

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return require_configured_guild(interaction)


class CraftCordBot(commands.Bot):
    """discord.py bot with CraftCord's services pre-wired."""

    def __init__(self) -> None:
        # Default intents are enough for slash commands and most cogs.
        # `message_content` is a privileged intent (Developer Portal ->
        # Bot -> Privileged Gateway Intents) and is only requested when
        # the Discord -> Minecraft chat bridge is configured via
        # CHAT_CHANNEL_ID, because bot.cogs.chat must read ordinary
        # message text to relay it in-game.
        intents = discord.Intents.default()
        if config.chat_channel_id is not None:
            intents.message_content = True
        # ``command_prefix`` is required by commands.Bot but unused
        # because we never register text commands; help_command=None
        # disables the default ``!help`` command that would otherwise
        # try to answer in prefix mode.
        super().__init__(
            command_prefix="!",
            intents=intents,
            help_command=None,
            tree_cls=GuildBoundCommandTree,
            allowed_mentions=_NO_MENTIONS,
        )

        # Used by /botinfo to compute uptime. Recorded at construction
        # time rather than login so it reflects process lifetime.
        self.start_time: float = time.time()

        # Shared services. Constructed once so every cog reuses the
        # same RCON connection, DB handle, and mcstatus client instead
        # of each one opening its own.
        self.db: Database = Database(config.database_path)
        self.rcon: RconService = RconService(
            config.rcon_host, config.rcon_port, config.rcon_password
        )
        self.mc: McStatusService = McStatusService(config.mc_host, config.mc_port)
        # Lazy: no webhook or HTTP work happens unless an identity mode
        # is ``player`` and a player chat/event is actually delivered.
        self.player_identity: PlayerIdentityService = PlayerIdentityService(self)

        # Always sync to the required configured guild.
        self._guild_obj: discord.Object = discord.Object(id=config.guild_id)

    async def setup_hook(self) -> None:
        """Run once after login, before the bot starts receiving events.

        This is discord.py's canonical place for async startup work —
        we connect the database, load extensions, register error
        handling, and sync the command tree from here.
        """
        await self.db.connect()

        for cog in _COGS:
            # load_extension calls each module's ``setup(bot)`` which
            # attaches the cog. If one fails we want the traceback,
            # not a silent skip — so we let the exception propagate
            # and abort startup.
            await self.load_extension(cog)
            log.info("Loaded cog: %s", cog)

        if config.chat_channel_id is not None:
            # Load after console so both sides of the chat bridge come
            # up together and the user never sees one direction working
            # without the other in a partially-started state.
            await self.load_extension(_CHAT_COG)
            log.info(
                "Chat bridge enabled (channel configured); loaded %s",
                _CHAT_COG,
            )
        else:
            log.info(
                "Chat bridge disabled: CHAT_CHANNEL_ID is unset; "
                "skipping %s and not requesting message_content intent",
                _CHAT_COG,
            )

        # Best-effort connectivity check for the configured RCON
        # target before the command tree comes online.
        await self._ping_rcon()

        # Route every unhandled slash-command exception through our
        # formatter so users get a polite ephemeral reply instead of
        # "The application did not respond".
        self.tree.on_error = self._on_app_command_error  # type: ignore[assignment]

        # Guild-scoped sync replaces the configured guild's command
        # list. There is no global-sync fallback: DISCORD_GUILD_ID is
        # required. Stale global commands from an older install may
        # still appear remotely until an operator clears them manually;
        # runtime guild checks still deny unauthorized invocations.
        self.tree.copy_global_to(guild=self._guild_obj)
        synced = await self.tree.sync(guild=self._guild_obj)
        log.info("Synced %d commands to the configured guild", len(synced))

    async def _ping_rcon(self) -> None:
        """Verify the configured Minecraft RCON target can authenticate.

        Failures are logged as warnings — not raised — because the
        runtime path re-authenticates on every command after a failure,
        so a transient network blip at startup should not prevent the
        bot from coming up.

        ``ping()`` closes the socket after a successful auth (see its
        docstring for why) so the first user command always opens a
        fresh connection.
        """
        try:
            await self.rcon.ping()
            log.info("RCON ready: configured Minecraft server")
        except RconError as e:
            # ``e`` is already a sanitized category string from
            # RconService (no host, port, or socket detail).
            log.warning(
                "RCON unreachable: configured Minecraft server — %s. "
                "Commands targeting this server will fail until it is fixed.",
                e,
            )

    async def on_ready(self) -> None:
        """Log a one-line confirmation once the gateway handshake is done.

        ``on_ready`` can fire more than once (after reconnects) so do
        **not** put startup-only logic here — it belongs in
        :py:meth:`setup_hook`.
        """
        log.info("Logged in as %s (id=%s)", self.user, self.user.id if self.user else "?")

    async def close(self) -> None:
        """Tear down owned resources, then defer to the parent close.

        We close RCON and the database explicitly because they hold OS
        resources (a TCP socket and a SQLite file handle) that we'd
        rather release cleanly than leave to garbage collection.
        Failures are logged and swallowed so a partial teardown still
        reaches ``super().close()`` and disconnects from Discord.
        """
        try:
            await self.rcon.close()
        except Exception:
            log.exception("Error closing RCON")
        try:
            await self.db.close()
        except Exception:
            log.exception("Error closing database")
        try:
            await self.player_identity.close()
        except Exception:
            log.exception("Error closing player identity service")
        await super().close()

    @staticmethod
    async def _on_app_command_error(
        interaction: discord.Interaction,
        error: app_commands.AppCommandError,
    ) -> None:
        """Default handler for slash-command errors.

        Two code paths:

        * :class:`~discord.app_commands.CheckFailure` — raised by our
          permission decorators with a human-readable message. We
          forward that message verbatim.
        * Anything else — unexpected; we log the exception class for
          operators and show the user a generic apology so we never
          leak internals or stack traces into chat.
        """
        if isinstance(error, app_commands.CheckFailure):
            msg = str(error) or "You can't use this command."
        else:
            log.error(
                "Slash command error: %s",
                type(error).__name__,
            )
            msg = "Something went wrong running that command."

        # Whether we've already responded depends on where the error
        # fired (pre- or post-defer), so check and use the right API.
        # The outer try/except swallows secondary failures (e.g. the
        # interaction token has expired) because there's nothing useful
        # we can do about them.
        try:
            if interaction.response.is_done():
                await interaction.followup.send(
                    msg, ephemeral=True, allowed_mentions=_NO_MENTIONS
                )
            else:
                await interaction.response.send_message(
                    msg, ephemeral=True, allowed_mentions=_NO_MENTIONS
                )
        except discord.DiscordException:
            pass
