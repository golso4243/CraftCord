"""
Shared utility helpers used across the CraftCord bot.

The modules here are intentionally narrow and dependency-light so they
can be reused from any cog or service without creating import cycles:

* :mod:`bot.utils.embeds`      — builds the server-status embed from a
  :class:`StatusSnapshot` dataclass.
* :mod:`bot.utils.formatting`  — pure text helpers: uptime humanisation,
  Minecraft / ANSI code stripping, and code-fence neutralization.
* :mod:`bot.utils.permissions` — role-based ``app_commands`` check
  decorators (``require_admin`` / ``require_mod`` / ``require_member``).

Everything in this package should stay side-effect free — no network
calls, no global state — so the helpers remain trivially testable.
"""
