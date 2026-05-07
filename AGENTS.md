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

| Node | Hostname     | Primary Responsibilities                          |
|------|--------------|---------------------------------------------------|
| Pi 1 | `pv-node-1` | Primary sensor orchestration, ADS-B, Drone RF, SDR coordination |
| Pi 2 | `pv-node-2` | Wardriving, Bluetooth scanning, WiGLE staging     |

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
- Active `feature/*` branches that another agent is actively working on
- `main` branch directly (always works on `feature/*` branches)

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
- Force-pushes to any `feature/*` branch Claude Code has active
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
main
 └── feature/<module-name>       ← Claude Code works here
 └── refactor/<scope>            ← Grok proposes here
 └── docs/<topic>                ← Grok only
 └── hotfix/<description>        ← Human or Claude Code, fast-tracked
```

- `main` is protected — only merges via PR with human approval
- Claude Code opens its own PRs OR pushes and asks Grok to open the PR
- Grok reviews all PRs for cross-module impact before human approval

### Docs-Only Exception for `main`

Grok may push **docs-only changes** (CONTEXT.md, README.md, AGENTS.md, SECURITY.md, CONTRIBUTING.md, or files under `docs/`) **directly to `main`** without a full `feature/* → dev → main` cycle **provided**:

- The change is purely documentation or coordination metadata.
- No code, tests, or configuration that affects runtime behavior is modified.
- The commit message is tagged `[grok] docs(main): …`
- Human (Cody) is notified immediately and retains veto power.
- This exception exists only because CONTEXT.md must remain the single source of truth for every agent session; it does **not** extend to any implementation work.

All non-docs changes continue to require the standard Pi-validated PR path.

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
- [ ] At least one `Tested: Pi 1` or `Tested: Pi 2` in commit history
- [ ] No hardcoded credentials, API keys, or local paths
- [ ] New modules registered in `CONTEXT.md` module table
- [ ] systemd unit file included if module runs as a service
- [ ] No x86-only dependencies (check against `requirements.txt`)
- [ ] `CONTEXT.md` updated to reflect new state post-merge
- [ ] CI green on the target branch/commit

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
