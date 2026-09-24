# Security Policy

CraftCord is a self-hosted Discord bot that connects one configured Discord server to one Minecraft Java Edition server. Because it handles bot credentials, server logs, and privileged Minecraft actions through RCON, please report suspected vulnerabilities privately.

## Supported versions

CraftCord is currently in prerelease development. Security fixes target the latest code on the default `main` branch. Older commits, forks, and modified copies do not receive separate security backports.

When reporting a vulnerability, include the affected commit SHA. Reports about older versions are still welcome, especially if the issue may affect current code. See the [README](README.md) for the current compatibility and testing status; security support does not imply that every deployment environment has been verified.

## Reporting a vulnerability

**Do not disclose vulnerabilities through public issues, pull requests, discussions, or Discord channels.**

Use GitHub's private vulnerability reporting form:

**[Report a vulnerability privately](https://github.com/golso4243/CraftCord/security/advisories/new)**

You can also open the repository's **Security → Advisories** page and select **Report a vulnerability**.

If private reporting is unavailable, open an issue asking the maintainer, **@golso4243**, to enable it or provide a private reporting channel. Include only that request—no vulnerability description, affected component, reproduction steps, logs, or exploit code. Wait for a private channel before sharing details.

### What to include

- A brief description of the vulnerability and its potential impact.
- The affected commit SHA and relevant Python, Minecraft, and operating system versions.
- Relevant configuration setting names and whether you use local logs or Pterodactyl streaming. Redact sensitive values.
- Required access or preconditions, including Discord roles and permissions.
- Clear reproduction steps or a minimal proof of concept using a disposable test environment.
- Expected behavior, observed behavior, and any sanitized supporting output.
- A suggested fix, if you have one.

Never include live Discord tokens, RCON passwords, Pterodactyl API keys, complete `.env` files, or other people's private data—even in a private report. Use placeholders and synthetic examples.

## Security concerns in scope

Examples include:

- Bypassing the configured Discord guild boundary or role checks.
- Running unauthorized Minecraft commands or injecting commands through chat or slash-command input.
- Exposing credentials or sensitive configuration through messages, logs, errors, or committed files.
- Sending private logs or data to unintended channels or Discord servers.
- Bypassing mention suppression to trigger unauthorized mass mentions.
- Triggering significant resource exhaustion or repeated crashes through untrusted input.
- Vulnerable dependencies that create a security risk in CraftCord.

The configured Discord guild's owner and members with Discord **Administrator** permission intentionally bypass role-ID checks within that guild. That behavior alone is not a vulnerability; access from another guild or execution beyond the intended command set may be.

Ordinary bugs, feature requests, and compatibility questions may be reported through public issues after removing sensitive information. If you are unsure whether a problem is security-sensitive, report it privately first.

## Handling and disclosure

Reports are reviewed by the project maintainer on a best-effort basis. There is no guaranteed response or resolution timeline.

The maintainer may request additional information, assess the impact, and coordinate a fix or mitigation through the private report. Please coordinate public disclosure with the maintainer so affected operators have an opportunity to protect their installations. Avoid opening a public fix pull request that reveals an unresolved vulnerability before coordination.

## Responsible testing

Test only systems you own or have explicit permission to assess. Use a disposable Discord guild, Minecraft server, and test credentials. Do not access other users' data, disrupt live communities, or test third-party hosting infrastructure without authorization. If you encounter sensitive data unexpectedly, stop testing and report what happened without copying or sharing that data.

## Protecting your installation

- Keep `.env`, credentials, logs, and database files out of public repositories and support attachments. Revoke or rotate exposed credentials immediately; deleting a message or commit alone does not make them safe again.
- Restrict RCON access to the bot host or a trusted private network. Do not expose RCON directly to the public internet.
- Grant only the Discord permissions documented in the README, and carefully control membership in configured admin and moderator roles.
- Keep `ENABLE_CONSOLE_MIRROR=false` unless needed. If enabled, restrict `CONSOLE_CHANNEL_ID` to trusted staff; raw logs can contain player IP addresses, UUIDs, chat, and operational details.
- Protect the bot host and its configuration, and restrict Pterodactyl credentials to the minimum access needed.
- Review and apply security updates, using the project's hashed dependency lockfiles for installation.

Operators are responsible for securing their own hosts, Discord permissions, Minecraft servers, and credentials. CraftCord's application checks do not replace those controls.
