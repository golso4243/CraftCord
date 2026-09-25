# CraftCord

**Your server. Your Discord. Connected.**

Self-hosted Discord bot for one Minecraft Java Edition server. Product domain: craftcordbot.com.

CraftCord targets vanilla Minecraft Java Edition **26.2** with no required Minecraft mods or plugins. Offline serialization and parser tests exist, and the live checklist has been run against a disposable vanilla English 26.2 server (Temurin 25.0.1) and a Discord test guild using Python 3.13.7 on Windows with local log tailing — see [Live test results](#live-test-results). Pterodactyl log streaming has **not** been live-tested. No release has been published yet; treat this tree as a pre-release development checkout. This README is the operator documentation source of truth.

Each instance connects **one** configured Discord guild to **one** Minecraft server.

## Features

- **RCON slash commands** — `/list`, `/say`, `/kick`, `/ban`, `/pardon`, `/whitelist add`, `/whitelist remove` against the single configured Minecraft server. There is no raw arbitrary-command endpoint.
- **Admin / diagnostic commands** — `/ping`, `/botinfo`, `/sync` (configured guild only).
- **Pinned server-status embed** — auto-updating online/offline, version, MOTD, players, player-name sample, ping, and reachability-based uptime (`/status` plus a background refresh loop). Address shown only when `PUBLIC_ADDRESS` is set.
- **Log mirroring** — local file tailing (`MC_LOG_PATH`) or Pterodactyl panel WebSocket (`LOG_SOURCE=pterodactyl`). Generic console output is opt-in via `ENABLE_CONSOLE_MIRROR`.
- **Minecraft → Discord chat** — vanilla `<Player> message` lines forwarded from the server log.
- **Discord → Minecraft chat bridge** — messages in `CHAT_CHANNEL_ID` relayed in-game via RCON `tellraw` using SNBT text components (`{text, color}` only; hover omitted) (optional; requires Message Content intent).
- **Event embeds** — vanilla join, leave, death, and advancement/challenge/goal events as coloured embeds.
- **SQLite persistence** — stores the pinned status message ID and reachability-based `online_since` across restarts.
- **Single-guild enforcement** — `DISCORD_GUILD_ID` is required; commands and destinations outside that guild are refused.
- **Role-based permissions** — admin / mod / member tiers for slash commands and the chat bridge (after the guild check).
- **Automated tests** — `pytest` suite for operators deploying from source.

## Project layout

```
main.py                          entry point
bot/
├── config.py                    env var loader + validation
├── bot.py                       custom Bot class, cog loading
├── cogs/
│   ├── admin.py                 /ping, /botinfo, /sync
│   ├── rcon.py                  /say, /kick, /ban, /pardon, /list, /whitelist …
│   ├── status.py                /status + pinned auto-updating embed
│   ├── console.py               log tailing + MC → Discord routing
│   └── chat.py                  Discord → MC chat bridge
├── services/
│   ├── rcon_service.py          async RCON (lock; reconnect; no silent mutating replay)
│   ├── mcstatus_service.py      async ping/query
│   ├── log_service.py           async file tailer with rotation support
│   └── pterodactyl_service.py   Pterodactyl panel WebSocket log stream
├── database/
│   └── db.py                    aiosqlite schema + helpers
└── utils/
    ├── permissions.py           role-based access checks
    ├── embeds.py                status embed builder
    ├── formatting.py            uptime / text cleaning helpers
    ├── mc_log_parser.py         classify log lines (chat, events, …)
    ├── minecraft.py             username validation + reason sanitization
    └── text_component.py        SNBT tellraw serialization + response class
tests/                           pytest suite
requirements.txt                 direct runtime pins (edit this)
requirements.lock                hashed resolved runtime install
requirements-dev.txt             direct test pins
requirements-dev.lock            hashed test install (constrained by runtime lock)
requirements-audit.txt           pip-audit only (not for runtime)
requirements-audit.lock          hashed audit tooling
.github/workflows/ci.yml         CI workflow (remote execution pending)
```

## Setup

### 1. Create the bot application

1. Go to https://discord.com/developers/applications, create a new application, and add a Bot user.
2. Copy the bot **token** into `DISCORD_TOKEN`.
3. Under **Installation** / **OAuth2**, invite the bot to **only** the Discord server you will put in `DISCORD_GUILD_ID`, with at least these permissions:
   - View Channels
   - Send Messages
   - Embed Links
   - Read Message History
   - Manage Messages (pin and edit the status embed)
   - Add Reactions (only used when a Discord → Minecraft chat message fails to send via RCON; not used for permission denials)
   - Manage Webhooks — **only** if `CHAT_IDENTITY_MODE` or `EVENTS_IDENTITY_MODE` is `player`, and only needed in those channels. Not required with the default `bot` modes.
4. In the Developer Portal, turn **Public Bot** off unless you have a deliberate multi-install product plan. This installation is single-guild by design; runtime checks refuse other guilds, but keeping the application private reduces accidental invites.
5. **Privileged intents (Message Content)** — depends on whether you enable the chat bridge:
   - **`CHAT_CHANNEL_ID` unset:** default intents are enough. You do **not** need Message Content.
   - **`CHAT_CHANNEL_ID` set:** enable **Message Content Intent** under Developer Portal → your application → **Bot** → **Privileged Gateway Intents** → **Message Content Intent**. The Discord → Minecraft bridge reads normal channel messages via `on_message`.

### 2. Configure Minecraft

Enable RCON in your server's `server.properties`:

```
enable-rcon=true
rcon.port=25575
rcon.password=<choose-a-long-random-password>
```

Restart the Minecraft server after changing RCON settings. Set `white-list=true` (or run `/whitelist on`) if you want Discord whitelist commands to be enforced.

For Minecraft → Discord chat and events, the bot needs access to the server log:

- **Local:** run CraftCord on a host that can read `logs/latest.log`, and set `LOG_SOURCE=local` plus `MC_LOG_PATH`.
- **Pterodactyl-compatible panel:** set `LOG_SOURCE=pterodactyl` and the panel URL, server id, and client API key.

### 3. Configure the bot

Copy `.env.example` to `.env` and fill everything in:

```bash
cp .env.example .env
```

Get role and channel IDs by enabling **Developer Mode** in Discord (Settings → Advanced) and right-clicking to **Copy ID**.

#### Configuration reference

| Variable | Purpose |
| -------- | ------- |
| `DISCORD_TOKEN` | Bot token from the Developer Portal |
| `DISCORD_GUILD_ID` | **Required.** Guild this installation is bound to; commands sync here only |
| `ADMIN_ROLE_ID` | Role with admin-tier access |
| `MOD_ROLE_ID` | Role with mod-tier access |
| `MEMBER_ROLE_ID` | Role with member-tier access |
| `STATUS_CHANNEL_ID` | Channel for the pinned status embed (must be in the configured guild) |
| `CONSOLE_CHANNEL_ID` | Staff-only destination for generic console output (used only when mirroring is enabled) |
| `ENABLE_CONSOLE_MIRROR` | `true` / `false` (default `false`). Opt-in raw console mirroring |
| `CHAT_CHANNEL_ID` | Bidirectional chat bridge channel (optional) |
| `EVENTS_CHANNEL_ID` | Join/leave/death/advancement embeds (optional; no console fallback) |
| `CHAT_IDENTITY_MODE` | `bot` (default) or `player`. Sender identity for Minecraft chat. See [Player identities](#player-identities-optional) |
| `EVENTS_IDENTITY_MODE` | `bot` (default) or `player`. Sender identity for join/leave/death/advancement events |
| `MC_HOST`, `MC_PORT` | Server list ping target (never shown on the status embed) |
| `PUBLIC_ADDRESS` | Address shown in `/status` when set (e.g. `play.example.com`); omit field when blank |
| `RCON_HOST`, `RCON_PORT`, `RCON_PASSWORD` | RCON connection for the configured Minecraft server |
| `LOG_SOURCE` | `local` (tail a file) or `pterodactyl` (panel WebSocket) |
| `MC_LOG_PATH` | Path to `latest.log` when `LOG_SOURCE=local` |
| `PTERODACTYL_PANEL_URL` | Panel base URL when `LOG_SOURCE=pterodactyl` |
| `PTERODACTYL_SERVER_ID` | Server identifier from the panel URL |
| `PTERODACTYL_API_KEY` | Client API key from the panel |
| `STATUS_UPDATE_INTERVAL` | Seconds between status embed refreshes (minimum 10) |
| `DATABASE_PATH` | SQLite database file |

**Single-guild enforcement:** every slash command (including `/ping` and `/botinfo`), the chat bridge, and outbound destinations check `DISCORD_GUILD_ID` before acting. An owner or Administrator in another Discord server never gains access. DMs are refused.

**Command sync:** startup and `/sync` register the command tree only for `DISCORD_GUILD_ID`. There is no global-sync fallback. If an older installation left **global** commands registered, they may still appear in Discord clients until cleared. Runtime guild checks still deny unauthorized use. To clear stale global commands deliberately (optional, using your bot token — never commit the token):

```bash
# Replace APPLICATION_ID; pass the bot token via an env var, not the shell history if possible.
curl -X PUT "https://discord.com/api/v10/applications/APPLICATION_ID/commands" \
  -H "Authorization: Bot $DISCORD_TOKEN" \
  -H "Content-Type: application/json" \
  -d "[]"
```

Then restart the bot (or run `/sync` in the configured guild) so guild commands are re-registered.

**Channel routing:** chat and events require their dedicated channel IDs. They do **not** fall back to `CONSOLE_CHANNEL_ID`. Generic console lines are sent only when `ENABLE_CONSOLE_MIRROR=true` and `CONSOLE_CHANNEL_ID` resolves inside the configured guild. Raw console output can include player IPs, UUIDs, chat, and operational information — keep that channel staff-only. Do not treat filtering or mention suppression as making raw logs safe for public channels. A line containing `[STAFF_ALERT]` is not a special event; it is ordinary console text only when mirroring is enabled.

### 4. Install and run

**Tested locally and live:** Python **3.13.7** on Windows 10.0.22631 (reported as Windows 11), against vanilla Minecraft Java 26.2 on Temurin 25.0.1. Other Python minor versions and operating systems are **not** claimed as verified from this tree.

Prefer the hashed lockfile so installs match the audited resolution. Installing dependencies does **not** start the bot.

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.lock
python main.py
```

`requirements.txt` lists direct runtime pins only. To regenerate locks after editing it (use the public PyPI index; inspect the lock for private URLs before committing):

```bash
pip install "pip-tools>=7.4"
set PIP_INDEX_URL=https://pypi.org/simple
set PIP_EXTRA_INDEX_URL=
pip-compile --generate-hashes --allow-unsafe -o requirements.lock requirements.txt
pip-compile --generate-hashes --allow-unsafe --constraint=requirements.lock -o requirements-dev.lock requirements-dev.txt
pip-compile --generate-hashes --allow-unsafe -o requirements-audit.lock requirements-audit.txt
```

### 5. Running tests (optional)

```bash
pip install -r requirements.lock -r requirements-dev.lock
set CRAFTCORD_SKIP_DOTENV=1          # Unix: export CRAFTCORD_SKIP_DOTENV=1
python -m pytest tests/ -q --tb=short
```

Test credentials are set automatically in `tests/conftest.py`; no real Discord token or Minecraft server is required. Dotenv loading is skipped when `CRAFTCORD_SKIP_DOTENV` is set.

### 6. Continuous integration

[`.github/workflows/ci.yml`](.github/workflows/ci.yml) installs the lockfiles, runs pytest with `CRAFTCORD_SKIP_DOTENV=1`, and runs a separate `dependency-audit` job with `pip-audit`. It needs no repository secrets. **Remote GitHub Actions execution has not been verified** from this tree; there is no passing CI badge to display yet.

## Deploying on PebbleHost

1. Upload the project files (everything except `.venv/`, `__pycache__/`, `data/`, `.env`).
2. Create the `.env` file on the host using their file manager, or set the same variables in the **Startup** / **Environment** tab.
3. Set the startup command to:

   ```
   python main.py
   ```

4. **Local log tailing** — if the bot runs on the same machine as the Minecraft server, set `LOG_SOURCE=local` and point `MC_LOG_PATH` at the log file. On PebbleHost Minecraft hosts this is typically `/home/container/logs/latest.log`.
5. **Pterodactyl WebSocket** — if the bot cannot read `latest.log` on disk (bot and game server on different hosts), set `LOG_SOURCE=pterodactyl` and fill in `PTERODACTYL_PANEL_URL`, `PTERODACTYL_SERVER_ID`, and `PTERODACTYL_API_KEY`.

## Permissions model

Slash commands use role-based checks (`bot/utils/permissions.py`) **after** the configured-guild boundary. The configured-guild owner and anyone with the Discord **Administrator** permission in that guild bypass role-id checks for the named commands below. That does **not** grant a raw RCON console — only the listed slash commands exist. Owners and Administrators in other Discord servers are denied.

### Slash commands

| Command | Minimum access |
| ------- | -------------- |
| `/ping`, `/botinfo` | Anyone in the configured guild (guild check only) |
| `/status`, `/list` | Member tier |
| `/say`, `/kick`, `/ban`, `/pardon`, `/whitelist add`, `/whitelist remove` | Mod tier |
| `/sync` | Admin tier (always syncs the configured guild) |

`/whitelist add` and `/whitelist remove` run once against the configured Minecraft server and return a single result.

### Non-slash features

| Feature | Who can trigger | Access rule |
| ------- | --------------- | ----------- |
| Minecraft → Discord chat | Any in-game player | Automatic from the server log; no Discord role gate |
| Discord → Minecraft chat | Discord users in `CHAT_CHANNEL_ID` | Configured-guild owner, Discord Administrator, or configured admin/mod/member role; bots/webhooks/other guilds ignored |
| Console mirroring | Automatic when enabled | Requires `ENABLE_CONSOLE_MIRROR=true`, a log source, and a staff-only `CONSOLE_CHANNEL_ID` in the configured guild |
| Event embeds | Automatic | Requires `EVENTS_CHANNEL_ID` in the configured guild |

## Chat bridge

When `CHAT_CHANNEL_ID` is set, the bot runs a bidirectional chat bridge:

- **Minecraft → Discord** — the console cog parses chat lines from the server log and posts them to the chat channel as `**[Minecraft]** **Player**: message` (or as `Player • Minecraft` with only the message text when `CHAT_IDENTITY_MODE=player`; see [Player identities](#player-identities-optional)).
- **Discord → Minecraft** — the chat cog relays messages from the chat channel into the game via RCON `tellraw` with an SNBT list of `{text, color}` compounds (no hover). Names and message bodies are literal text. Oversized payloads are shortened on Unicode boundaries before serialization, or delivery fails with the warning reaction. Known English command-error responses and the exact phrase `No player was found` are treated as delivery failures; an empty RCON body means no error text was observed, not that a player saw the message.

**Who can send Discord → Minecraft messages:** the configured-guild owner, anyone with Discord **Administrator** in that guild, or a member with the configured admin, mod, or member role. Users who can type in the channel but lack one of those roles are silently ignored (no reaction, no in-game message). Messages from other guilds, bots, and webhooks are ignored.

**Mention safety (Minecraft → Discord):** forwarded chat, events, console chunks, status updates, and slash replies use `AllowedMentions.none()` by default. Text like `@Admin`, `@everyone`, or `<@123456789>` still appears in the message, but Discord will not turn it into a real ping.

**RCON failures:** if an authorized user's message fails to reach the game (RCON error), the bot adds a warning reaction to their Discord message. This requires the **Add Reactions** permission. The chat cog does not retry after a failed `command()` call or after a known command-error response. Separately, the RCON service reconnects for future requests; it does **not** silently replay mutating commands or `tellraw` when delivery is uncertain (response lost after the command may have reached Minecraft). Read-only `list` probes may opt into one uncertain-delivery replay. This is not exactly-once delivery.

**Message Content intent:** required only when `CHAT_CHANNEL_ID` is set. See [Create the bot application](#1-create-the-bot-application).

## Player identities (optional)

By default, CraftCord posts Minecraft chat and events under its own name and avatar. Two independent settings can switch them to the player's identity:

```env
CHAT_IDENTITY_MODE=player
EVENTS_IDENTITY_MODE=player
```

Each accepts only `bot` (default) or `player`; any other value stops startup with a configuration error. Unset or `bot` keeps the old behavior exactly, and CraftCord makes no webhook calls for that route.

**What player mode changes:**

| Message | Sender |
| ------- | ------ |
| Minecraft chat | The player |
| Join / leave | The affected player |
| Death | The player who died |
| Advancement / challenge / goal | The player who earned it |
| Slash-command replies (`/list`, `/status`, `/sync`, etc.) | CraftCord |
| Pinned server status | CraftCord |
| Console mirror, broadcasts, and other server output | CraftCord |

The sender is shown as `PlayerName • Minecraft` with the player's skin head. The `• Minecraft` suffix sets these posts apart from messages typed in Discord. In chat, the body is only the message text (no `[Minecraft] Player:` prefix). Events keep their colored embeds, icons, and wording, so the player's name may also appear inside the embed. Player identity comes only from parsed vanilla chat and events. CraftCord never guesses a player from other server output. The bot account's own name and avatar are not changed.

**How it works:** CraftCord creates (or reuses) one webhook named `CraftCord` in each channel whose mode is `player`, and overrides the sender name and avatar on each message. It only reuses a webhook that the bot account itself created. A webhook merely named `CraftCord` is never adopted. If chat and events share a channel, they share one webhook.

**Permissions:** grant the bot **Manage Webhooks** in each channel used in player mode. Channel-level overrides are enough, so it does not need to be server-wide.

**Fallback and troubleshooting:**

- **Missing Manage Webhooks, or Discord rejects the webhook post:** the message is posted in the normal CraftCord format in the same channel, with the same player and content. A rate-limited warning names the setting (for example `CHAT_CHANNEL_ID: missing Manage Webhooks permission`). Setup is retried at most once a minute.
- **Webhook deleted:** CraftCord recreates it once. If it is deleted again within a minute, messages use the CraftCord format until that minute passes, and then recreation is tried again. To stop recreation completely, set the mode back to `bot`.
- **Timeout or Discord server error during a webhook post:** CraftCord does **not** retry or fall back, because the message may already be visible. In rare cases this can drop one message, but it will not post a duplicate. Delivery waits for Discord to confirm that it saved the message. discord.py handles rate limits.
- Player names containing `discord` or `clyde` cannot be webhook senders (Discord rule). They use the CraftCord format.
- Warnings never include webhook URLs, tokens, or chat text. All deliveries, including fallbacks, suppress mentions.

**Avatars and privacy:** for each player name, CraftCord looks up the official profile with the Minecraft Services API (`GET https://api.minecraftservices.com/minecraft/profile/lookup/name/{username}`). Then it uses a [Minotar](https://minotar.net/) head URL (face plus hat layer), `https://minotar.net/helm/{uuid}/128.png`. Only the Minecraft username is sent to Minecraft Services, and only the resulting UUID appears in the Minotar URL. Crafatar is not used because it blocks Discord's image proxy ([crafatar#322](https://github.com/crafatar/crafatar/issues/322)), which makes Discord show its default avatar. Discord fetches the image; chat text is never sent to either service. Successful lookups are cached for 1 hour and misses for 10 minutes (256 names max). Lookups time out after 5 seconds.

**Limitations:**

- Minotar serves head images with a cache lifetime of up to 6 hours, and Discord may cache avatars too, so a new skin can take a while to show.
- If the sender shows Discord's default logo instead of a head, check that `https://minotar.net/helm/<uuid>/128.png` loads in a browser. A bot without its own avatar also shows as the Discord logo when the profile lookup fails.
- Offline-mode servers and custom or server-side skins cannot be matched reliably to official profiles. A name that happens to match a real account shows that account's head. An unknown name shows CraftCord's avatar.
- If a lookup fails, CraftCord still sends the message, using its own avatar on that message so the previous player's head does not carry over.

## Log formats (vanilla English baseline)

The parser expects English vanilla Java server log lines after the last `]: `:

- Chat: `<Player> message`
- Join: `Player joined the game`
- Leave: `Player left the game`
- Advancements: `Player has made the advancement [Name]`, `completed the challenge`, or `reached the goal`
- Deaths: a curated set of common English death verbs (not exhaustive; new release wording may fall through)

Altered log formats from mods or plugins are outside the initial supported scope. Unknown lines fail gracefully and reach Discord only when console mirroring is explicitly enabled. Login-address and UUID lines are not classified as player events. All unit-test fixtures are synthetic unless a results document explicitly labels a line as captured from a live 26.2 server.

## Notes / trade-offs

- **Uptime** — gated on **RCON reachability** via the vanilla `list` command, not just SLP. Managed hosts (PebbleHost, BisectHosting, Apex, …) often front the game port with a proxy that keeps answering SLP with a stub while the underlying MC process is restarting. Every status refresh probes RCON with `list`; uptime advances while the probe succeeds and resets after two consecutive probe failures (~1 minute at the default 30 s refresh interval). Persisted in SQLite so it survives bot restarts. This is **observed RCON reachability**, not an exact measurement of Minecraft process uptime.
- **Player-name sample** — the status embed’s “Who’s online” field shows the SLP sample when the server provides one. Vanilla typically truncates that sample; it is not necessarily every online player.
- **RCON startup check** — at startup the bot opens an authenticated connection to the configured Minecraft server and logs `RCON ready: configured Minecraft server` or `RCON unreachable: configured Minecraft server — …`. Failures don't abort startup. Auth errors point at the three common causes: `RCON_PASSWORD`, `enable-rcon=true`, and the configured RCON port.
- **Named RCON commands only** — there is no raw `/rcon` slash command. Mods use `/say`, `/kick`, `/ban`, `/pardon`, and `/whitelist`. Discord Administrator in the configured guild is not unrestricted Minecraft console access.
- **Moderation input validation** — `/kick`, `/ban`, `/pardon`, and whitelist commands validate Minecraft usernames (1–16 letters, numbers, or underscores) before calling RCON. `/kick` and `/ban` reason strings are trimmed, control characters collapsed, and capped at 200 characters.
- **Pterodactyl auth failures** — invalid API credentials log one clear error and disable log mirroring until the bot is restarted or credentials are fixed. Transient network or panel errors still retry with exponential backoff.
- **Message Content intent** — only requested when `CHAT_CHANNEL_ID` is configured. Without the chat bridge, slash commands and log mirroring work with default intents.
- **Remaining verification** — live vanilla 26.2 acceptance of SNBT `tellraw`, local-log wording, SLP via `mcstatus==11.1.1`, and Discord end-to-end checks passed (see [Live test results](#live-test-results)). Still **not** live-verified: player-identity webhooks (`CHAT_IDENTITY_MODE` / `EVENTS_IDENTITY_MODE=player`), Pterodactyl log streaming, other Python versions and operating systems, and runtime foreign-guild denial. License selection, private security reporting, remote CI, and publication remain separate owner decisions. No release has been published yet.

## Verification

Do **not** paste credentials into tickets, chats, or issues. Configure them only in a private `.env`.

### Evidence tiers

| Phrase | Meaning |
| ------ | ------- |
| **No error text observed** | Empty / whitespace / color-only RCON body. Not proof the command was accepted, and not proof a player saw anything. |
| **Command accepted** | Minecraft returned a documented success or empty body **and** the operator checked server/player-facing effects. |
| **Player saw intended rendering** | A consenting player in-game confirmed the visible chat appearance. |

### Offline automated tests

Covered under [Running tests](#5-running-tests-optional). Expected coverage includes guild isolation, destination validation, mention suppression, console opt-in, sanitized errors, SNBT tellraw serialization, tellraw response classification, RCON uncertain-delivery (mutating commands not replayed; `list` may opt into one replay), synthetic vanilla log fixtures, status/uptime hysteresis, and player-identity webhook delivery (reuse, ownership, fallback, bounded recovery, avatar caching). No live Discord, Minecraft, or panel connections.

### Live-test install checklist

Use disposable credentials and an explicitly identified disposable vanilla **26.2** environment — not production.

1. Python 3.13.x (record `python --version`).
2. `python -m venv .venv`, activate, `pip install -r requirements.lock` (install alone must not start the bot).
3. Copy `.env.example` → private `.env`. Required setting **names**: `DISCORD_TOKEN`, `DISCORD_GUILD_ID`, `RCON_PASSWORD`; Minecraft `enable-rcon=true`; English vanilla 26.2; test-guild channel/role IDs. Prefer `ENABLE_CONSOLE_MIRROR=false`, blank `PUBLIC_ADDRESS`, and a test-only `DATABASE_PATH`.
4. Expected startup lines: `Logged in as …`, `Synced N commands to the configured guild`, and `RCON ready: configured Minecraft server` or the sanitized unreachable warning.

### Disposable Minecraft checks (local log)

With `LOG_SOURCE=local` and `MC_LOG_PATH` pointing at `latest.log` on a disposable English vanilla 26.2 server (keep authentication on; do not expose RCON publicly):

- Status / SLP reports the jar version; RCON `list` and server-list ping succeed.
- Discord → MC tellraw: empty RCON body means **no error text observed** only; a player online must confirm blue `[Discord]`, white name, `: `, white message (hover omitted).
- Unusual chat (quotes, backslashes, long Unicode) shortens safely or fails with the warning reaction.
- MC → Discord chat, join/leave, sample death, and advancement/challenge/goal phrases route as documented.
- Login IP / UUID lines are not join events; with console mirroring off they do not appear in Discord.
- Whitelist / kick / ban / pardon only against a designated consenting test account.
- After an MC restart, RCON reconnects for later requests; uptime resets after ~2 consecutive `list` failures. Mutating commands are not silently double-sent after an uncertain mid-flight loss.

Passing local-log checks does **not** verify Pterodactyl.

### Discord test-guild checks

Use a non-production Discord application and dedicated test guild (Public Bot off preferred). Message Content intent only if the chat bridge is on.

Guild-only sync means slash commands may be **absent** in a second guild. That absence is registration, not a live test of runtime foreign-guild denial. Do **not** globally register production commands to provoke denial. Cross-guild runtime enforcement is offline-tested; live verification stays pending.

Confirm authorized-guild commands and chat relay, mention non-ping on forwarded text, sanitized failure messages, and that `ENABLE_CONSOLE_MIRROR=false` keeps generic console silent.

#### Player identity checks (optional)

With `CHAT_IDENTITY_MODE=player`, `EVENTS_IDENTITY_MODE=player`, and Manage Webhooks granted in the chat and events channels:

1. Two different players each send a chat message. Each appears as `Name • Minecraft` with their own head, the body is the text only, and the second message does not reuse the first player's head.
2. One player joins, leaves, dies, and earns an advancement. Each event embed appears under that player's name and head.
3. Run `/list` or `/status`. The reply comes from CraftCord, as does the pinned status embed.
4. Server Settings → Integrations → Webhooks lists one `CraftCord` webhook per channel (one total if chat and events share a channel).
5. Remove Manage Webhooks and send chat. The message appears in the old `[Minecraft]` format and the bot logs a warning.
6. A player types `@everyone` in chat. It shows but does not ping, and webhook posts are not relayed back into Minecraft.

### Optional Pterodactyl checks

Only with a disposable panel instance (`LOG_SOURCE=pterodactyl`). Confirm the stream connects without logging tokens/payloads, auth failures stay sanitized, and routing matches local-tail classification.

### Live test results

Recorded by the operator against disposable credentials and a disposable environment:

| Item | Value |
| ---- | ----- |
| Minecraft | Vanilla Java Edition 26.2, English |
| Java | Temurin 25.0.1 |
| Python | 3.13.7 on Windows 10.0.22631 |
| Log source | `LOG_SOURCE=local` |

| Check | Result |
| ----- | ------ |
| Live-test install checklist (install, startup log lines) | PASS |
| Disposable Minecraft checks (local log) | PASS |
| Discord → MC `tellraw` rendering confirmed by an online player | PASS |
| Discord test-guild checks | PASS |
| Optional Pterodactyl checks | NOT RUN |

### Recording results

Keep PASS / FAIL / NOT RUN notes in a private operator file. Record Python version, jar/Java versions, whether a player was online for tellraw rendering, and never record tokens or passwords.
