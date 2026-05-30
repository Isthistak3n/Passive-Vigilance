# AGENTS.md — Passive Vigilance Agent Coordination

This file governs how AI agents (Claude Code, Grok) and human contributors
collaborate on this repository. All agents must read this file before taking
any action on the codebase.

---

## Repository Overview

**Project:** Passive Vigilance — passive RF/WiFi/Bluetooth/ADS-B sensor platform  
**Hardware:** Raspberry Pi (Debian ARM64, two nodes)  
**Runtime:** Python 3.x, asyncio, systemd services  
**Goal:** Counter-surveillance and situational awareness via passive sensing

---

## Node Roles

| Node   | Hostname   | Logical Alias | Primary Responsibilities                                        |
|--------|------------|---------------|-----------------------------------------------------------------|
| Pi 3B+ | `survkis`  | `pv-node-1`   | Active dev + WiFi/GPS spoke (Kismet, GPS, persistence)          |
| Pi 4B+ | `chase`    | `pv-node-2`   | Intended base station — SDR/ADS-B/Drone RF (unpowered)          |

When writing or testing code, always note which node it was validated on.

---

## Agent Roles & Boundaries

### Claude Code (Pi nodes)
**Identity:** `[claude-code]` in commit messages  
**Runs on:** Pi 1 and/or Pi 2 directly  

**Owns:**
- All implementation and execution of Python modules
- Hardware interaction (RTL-SDR, GPS, Kismet, readsb)
- `modules/`, `main.py`, `tests/`, `scripts/`, systemd unit files
- Runtime testing and validation on ARM64 hardware

**Never modifies without coordination:**
- Active `feat/*` branches that another agent is actively working on
- `main` directly — always works on prefixed work branches via PR

**Session discipline:**
- Pull latest branch and read `CONTEXT.md` at the start of every session
- On session close: commit with full context, push, and update run notes in `CONTEXT.md`
- Tag untested code clearly: `# UNTESTED — needs Pi validation`

---

### Grok (GitHub)
**Identity:** `[grok]` in commit messages  
**Runs via:** GitHub plugin  

**Owns:**
- Repo-wide architecture review and refactoring proposals
- PR creation, review, and merge coordination
- Cross-module consistency checks (naming, interfaces, error handling)
- `README.md`, `AGENTS.md`, `CONTEXT.md`, `.github/`, `docs/`
- Maintaining and updating `CONTEXT.md` on every merge to `main`

**Never:**
- Force-pushes to any `feat/*` branch Claude Code has active
- Merges to `main` without at least one confirmed Pi validation in the PR
- Modifies hardware-specific logic without flagging for Claude Code review

---

### Human (Cody)
**Identity:** `[human]` in commit messages  
**Role:** Final approver, hardware access, architectural decisions

**Owns:**
- Approving all PRs before merge
- Physical hardware changes, wiring, adapter assignments
- Security decisions (keys, credentials, network config)
- Resolving any agent conflict or ambiguity

---

## Branch Strategy

```
feat|fix|docs|hotfix|refactor/<name>  →  main   (via PR)
```

**Flow:** all work branches cut from `main`, merged back to `main` via PR. No intermediate integration branch.

**Allowed prefixes:** `feat/`, `fix/`, `docs/`, `hotfix/`, `refactor/`

**Gate: work branch → `main`**
- CI must be green
- At least one confirmed Pi validation recorded in the PR
- Cody approval
- PR required (ruleset-enforced — direct pushes to `main` are blocked)

- Claude Code opens its own PRs; Grok reviews all PRs for cross-module impact before Cody approval
- There is no docs-only exception — all changes go through the normal PR path

---

## Verification Rules (Mandatory)

**Any claim that a step or task is "completed" and references a code commit must include both:**
- The commit SHA
- The target branch

**Downstream work does not proceed** until **all** of the following are true:
- The SHA is independently verified via `git log` on the target branch
- CI is green on that commit/branch

**CI green is a hard gate before any merge.** No PR may be merged until the CI pipeline passes. This is the institutional enforcement of "verify after every push."

This rule applies to all agents and all claims of completion. Vague or incomplete claims (missing SHA, missing branch, or unverified CI) are treated as invalid.

---

## Commit Message Format

> This format governs **AI-agent commit subject lines** (machine-parseable, per-agent attribution). For PR titles, release notes, and human contributor commits, see `CLAUDE.md` → Commit & Release Standards.

``` 
[agent] type(scope): short description

Body (optional): what changed and why
Tested: Pi 1 / Pi 2 / untested
Refs: #issue-number
```

**Examples:**
``` 
[claude-code] feat(sdr): harden SHARED mode with lock + handshake (P1)
Tested: Pi 1
Refs: #22

[grok] refactor(main): integrate SDR health into orchestrator
Tested: untested — needs Claude Code validation on Pi
Refs: #23

[human] fix(wlan0): manual patch for monitor mode bind on Pi 2
Tested: Pi 2
```

---

## GitHub Issues as Task Queue

| Label          | Assigned To   | Meaning                                      |
|----------------|---------------|----------------------------------------------|
| `claude-code`  | Claude Code   | Implementation or hardware testing task      |
| `grok`         | Grok          | Review, refactor, or documentation task      |
| `human`        | Cody          | Requires physical access or final decision   |
| `blocked`      | —            | Waiting on another agent or hardware         |
| `needs-pi-test`| Claude Code   | Code written but not yet validated on hardware |
| `ready-to-merge`| Grok + Human | PR reviewed, Pi-tested, awaiting approval    |

---

## Merge Checklist (Grok enforces on every PR)

- [ ] Commit messages follow `[agent] type(scope):` format
- [ ] At least one `Tested: Pi 1` or `Tested: Pi 2` in commit history (required on every PR to `main`)
- [ ] No hardcoded credentials, API keys, or local paths
- [ ] New modules registered in `CONTEXT.md` module table
- [ ] systemd unit file included if module runs as a service
- [ ] No x86-only dependencies (check against `requirements.txt`)
- [ ] `CONTEXT.md` updated to reflect new state post-merge
- [ ] CI green on the target branch/commit
- [ ] PR required — direct pushes to `main` are blocked by the repository ruleset
- [ ] CodeQL code-scanning must pass — high+ severity security alerts block merge (ruleset-enforced)

---

## Conflict Resolution

If two agents have modified the same file on different branches:
1. Grok identifies the conflict in PR review and flags it
2. Claude Code resolves on the feature branch (runtime/hardware context wins)
3. Human approves resolution before merge

**Hardware truth beats repo truth.** If code works in theory but fails on Pi, the Pi is right.

---

## Security Rules for All Agents

- Never commit secrets, API keys, WiGLE credentials, or SSH keys
- Never expose sensor service ports beyond `tailscale0` interface
- GPS data and MAC address logs are sensitive — no sample data in commits
- `.gitignore` must cover: `*.log`, `*.gpx`, `*.shp`, `data/`, `captures/`
