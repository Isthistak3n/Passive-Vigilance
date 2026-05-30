# Contributing to Passive Vigilance

Thank you for your interest in contributing. This is an active project — contributions, bug reports, and hardware compatibility reports are all welcome.

## Branch strategy

`main` is the single protected branch. All work branches merge directly to `main` via PR — no intermediate integration branch.

Allowed prefixes: `feat/`, `fix/`, `docs/`, `hotfix/`, `refactor/`. Cut all branches from `main`. No direct commits to `main` (ruleset-enforced).

Gate for work branch → `main`: CI green + at least one Pi validation recorded in the PR + Cody approval.

## Getting started

```bash
git clone git@github.com:Isthistak3n/Passive-Vigilance.git
cd Passive-Vigilance
git checkout main
git checkout -b feat/your-feature-name
```

## Code standards

- All modules must have a corresponding test file in tests/
- All public methods must have type hints
- Use Python logging module — no print() statements
- All config loaded from .env via python-dotenv
- Run tests before pushing: python3 -m pytest tests/ -v

## Module conventions

Each module follows this lifecycle pattern:
- __init__() loads config from .env
- connect() establishes connection, raises ConnectionError if unavailable
- close() cleans up resources
- Module-level logger using logging.getLogger(__name__)

## Commit message format

feat(gps): add fix quality filtering
fix(kismet): handle 401 on expired API key
docs(readme): update hardware table
test(adsb): add enrichment mock tests

Types: feat, fix, docs, test, refactor, chore

## Hardware compatibility reports

If you get Passive Vigilance running on hardware not listed in the README, please open an issue with:
- Pi model and OS version
- WiFi dongle chipset and interface name
- SDR dongle model
- Any driver or config changes needed

This helps build a compatibility matrix for other users.

## Security issues

Do not open public issues for security vulnerabilities. See SECURITY.md for the responsible disclosure process.

## License

By contributing you agree your contributions will be licensed under the MIT License.
