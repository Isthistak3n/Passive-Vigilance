# Contributing

Thank you for your interest in contributing to Passive Vigilance.

## Branch Strategy

```
feature/* → dev → main
```

- **`main`** — stable, release-ready code. No direct commits.
- **`dev`** — integration branch. All feature work is merged here first.
- **`feature/<short-description>`** — branch off `dev` for each piece of work.

## Workflow

1. Fork the repository and create your branch from `dev`:
   ```bash
   git checkout dev
   git pull origin dev
   git checkout -b feature/your-feature-name
   ```
2. Make your changes with clear, focused commits.
3. Open a Pull Request targeting **`dev`** (not `main`).
4. A maintainer will review and merge into `dev`.
5. Merges from `dev` into `main` are made by maintainers at release time.

## Guidelines

- Keep PRs focused — one feature or fix per PR.
- Include or update tests where relevant.
- Do not commit `.env`, credentials, capture data, or output files.
- Follow existing code style (PEP 8).
