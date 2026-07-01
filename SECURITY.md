# Security Policy

## Scope

Passive Vigilance is a passive receive-only sensor platform. It does not transmit, does not connect to external networks on behalf of the user beyond configured API endpoints, and does not expose any public-facing services by default.

Security concerns relevant to this project include:

- Credential exposure (.env file contents)
- Kismet REST API exposure on the local network
- Data exposure in log files, shapefiles, or SQLite databases
- Dependency vulnerabilities in Python packages or system daemons

## Supported versions

| Version | Supported |
|---------|-----------|
| main branch | ✅ |
| feat\|fix\|docs\|hotfix\|refactor/* branches | ⚠️ Development only |

## Reporting a vulnerability

Please do not open a public GitHub issue for security vulnerabilities.

Report security issues by opening a private security advisory:
GitHub → Passive-Vigilance → Security → Advisories → New draft security advisory

Include:
- Description of the vulnerability
- Steps to reproduce
- Potential impact
- Suggested fix if known

You can expect an acknowledgement within 72 hours and a fix or mitigation within 14 days for confirmed vulnerabilities.

## Security best practices for deployment

Protect your .env file:
chmod 600 /home/youruser/Passive-Vigilance/.env

Restrict Kismet API to localhost only. Kismet binds to 0.0.0.0:2501 by default. If your Pi is on a shared network, add to /etc/kismet/kismet.conf:
httpd_bind_address=127.0.0.1

Never commit .env — the .gitignore excludes it by default. Verify with:
git status

Rotate your API keys periodically:
- Kismet: Settings → API Keys → Delete old → Create new
- WiGLE: Account page → regenerate API token
- adsb.lol: per their account management

Keep system packages updated:
sudo apt update && sudo apt upgrade

Use Tailscale for remote access. Do not expose SSH directly to the internet.

## Operational security (OPSEC) in a public repo

This is a passive counter-surveillance tool, so keep the deployment operator and
site un-identifiable in anything you commit:

- **Never commit a precise location.** Keep the node's real coordinates, address, or
  town out of code, tests, fixtures, and docs. Use a neutral placeholder — the test
  suite standardizes on `51.5, -0.1` (the app's default map center), and all
  air/AIS geometry is node-relative, so a placeholder reference preserves behavior.
  Real position belongs only in `.env` / the live GPS feed, never in the tree.
- **Keep committed times timezone-neutral.** Prefer UTC in logs and notes; a local
  timezone label narrows the region.
- **Don't commit captured data.** Real SSIDs, MACs, or vessel/aircraft identifiers
  and raw session logs can fingerprint a place or the people near it — commit only
  aggregates.
- **Secrets stay in `.env`** (git-ignored) — never in code, tests, or docs.
