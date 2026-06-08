# Contributing to Passive Vigilance

Thank you for your interest in contributing. This is an active project —
contributions, bug reports, and hardware compatibility reports are all welcome.

## How we work

Branch strategy, the merge gate, and commit / PR conventions live in
**[AGENTS.md](AGENTS.md)** — read it before opening a PR. In short:

- Cut a `feat|fix|docs|hotfix|refactor/<name>` branch from `main`; open a PR
  (direct pushes to `main` are blocked by the ruleset).
- The gate is CI green + at least one Pi validation recorded in the PR + approval.
- **Commits must be signed** — the `main` ruleset requires verified signatures, so
  an unsigned commit shows the PR as BLOCKED. Configure GPG or SSH signing first.

Follow AGENTS.md for the exact commit-message format — do **not** use bare
`feat(scope):` subjects; the project standard is plain-English subjects.

## Getting started

```bash
git clone git@github.com:Isthistak3n/Passive-Vigilance.git
cd Passive-Vigilance
git checkout main
git checkout -b feat/your-feature-name
```

Run the tests before pushing:

```bash
python3 -m pytest tests/ -v
```

## Code standards

Coding conventions and the per-module lifecycle (`__init__` / `connect` / `close`,
`logging` over `print`, type hints, `.env` config, a test file per module) are
documented in **[CLAUDE.md](CLAUDE.md) → Coding Conventions**. Match them.

## Hardware compatibility reports

If you get Passive Vigilance running on hardware not listed in the README, please
open an issue with:

- Pi model and OS version
- WiFi dongle chipset and interface name
- SDR dongle model
- Any driver or config changes needed

This helps build a compatibility matrix for other users.

## Security issues

Do not open public issues for security vulnerabilities. See
**[SECURITY.md](SECURITY.md)** for the responsible-disclosure process.

## License

By contributing you agree your contributions will be licensed under the MIT License.
