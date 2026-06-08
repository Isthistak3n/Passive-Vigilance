# CONTEXT.md — Passive Vigilance Live State

> **Maintained by:** Claude Code + Cody (updated at session close and on merges to `main`)
> **Read by:** Claude Code at the start of every session
> **Last updated:** 2026-06-07 HST
> **Updated by:** [claude-code] — doc dedup: roles point to AGENTS.md, module list to CLAUDE.md, security to SECURITY.md
>
> **Scope note:** this file holds *live state* only (hardware, ports, branches,
> handoffs). How-the-code-works lives in `CLAUDE.md`; how-we-work (roles, branch
> strategy, commit format) lives in `AGENTS.md`.

---

## Current Sprint Focus

> ⚠️ **Stale:** this section, **Architectural Direction**, **Active Branches**, and
> the dated session notes below pre-date the chase fixed-node bring-up and the
> P0–P5 work. They need a freshness pass — tracked separately from this
> doc-dedup change.

**Active goal:** Base-station bring-up (`chase`) — prerequisite for all multi-sensor and multi-node work
**Near-term priorities (sequential):**
1. ~~Update CONTEXT.md~~ ✅ (this file)
2. ~~Implement `drain_detections()` on DroneRFModule + remove private attribute access~~ ✅ (PR #31, Step 2)
3. ~~Create `core/exceptions.py` + `core/logging.py` + migrate `drone_rf.py`~~ ✅ (PR #31, Step 3)
4. Add `ModuleHealth` dataclass to `SensorOrchestrator` — **NOT YET BUILT** (verified: no `ModuleHealth` in codebase as of 2026-05-30)
5. Stand up `chase` (Pi 4B+) as base station — power on, OS install, SDR + LoRaWAN/GNSS HAT bring-up
6. Migrate highest-risk modules to use `core/` contracts
7. Expand error-path integration tests
8. Produce final standardization checklist + PR template

**Blocking issues:** **Multi-node coordination** remains unbuilt — the key
enabling work for the tiered architecture (see Architectural Direction below).
(`chase` base station is up and running fixed-mode scoring; see Hardware &
Adapter Map.)

---

## Architectural Direction (INTENDED — not yet built)

> This section describes the target architecture. It is direction, not current state.

**Tiered base-station + spoke model** (replaces the original single mega-node plan):

```
chase (Pi 4B+) — base station
  ├── RTL-SDR (ADS-B + Drone RF)
  ├── LoRaWAN/GNSS HAT (GPS)
  ├── DSI touchscreen (local display)
  └── coordination hub for spoke nodes

survkis (Pi 3B+) — spoke node (WiFi + GPS)
  ├── RTL8811CU WiFi adapter (monitor mode, Kismet)
  └── u-blox 8 GNSS (ttyACM0)

future spoke nodes — niche sensor subsets per Pi hardware/power limits
```

**Rationale:** A single Pi cannot carry full SDR + WiFi + BT + GPS + LoRa + display simultaneously at field power budgets. The base station handles heavy RF compute (ADS-B, Drone RF); spoke nodes carry focused sensors and report back.

**What's not built yet:**
- Multi-node coordination / message passing between nodes
- Spoke node registration and health reporting to base
- Distributed session aggregation

**Near-term focus:** Stand up a solid base station before adding spoke nodes. Single-node on `survkis` continues as the development and validation platform.

---

## Node Roles

Logical roles and aliases live in **`AGENTS.md` → Node Roles**. Live per-node
hardware and verified status are in **Hardware & Adapter Map** below — that is the
authority for what is actually present and working on each node.

> `pv-node-1` / `pv-node-2` are logical aliases only; the real hostnames
> (`survkis`, `chase`) are what Tailscale/mDNS resolve. Use real hostnames in any
> network or SSH config.

---

## Hardware & Adapter Map

### survkis (Pi 3B+) — active, verified 2026-05-30

| Component | Detail | Interface / Path | Status |
|-----------|--------|-----------------|--------|
| Onboard WiFi | Pi 3B+ built-in (BCM43438) | `wlan0` — UP (SSH/network) | ✅ Active |
| RTL8811CU WiFi adapter | Realtek RTL8811CU (0bda:c811), driver rtl8821cu/rtw88, 1×1 dual-band | `wlan1` — monitor mode (Kismet) | ✅ Active |
| GNSS receiver | u-blox 8 (1546:01a8), cdc_acm driver | `/dev/ttyACM0` | ✅ Active |
| Ethernet | — | `eth0` — DOWN (no cable) | — |
| RTL-SDR | — | not present | — (intended for chase) |

> **GPS device path note:** Native-USB GNSS receivers (u-blox, like this one) enumerate as `ttyACM*` via cdc_acm. USB-serial-bridge dongles enumerate as `ttyUSB*`. HAT-over-UART uses `ttyAMA*` or `ttyS*`. The code default `GPS_DEVICE=/dev/ttyUSB0` does NOT match this Pi — set `GPS_DEVICE=/dev/ttyACM0` in `.env` (already set on survkis).

### chase (Pi 4B+) — active, verified 2026-06-06

| Component | Detail | Interface / Path | Status |
|-----------|--------|-----------------|--------|
| RTL8811CU WiFi adapter | Realtek (0bda:c811), driver rtw88_8821cu | `wlan1` — monitor mode (Kismet) | ✅ Active |
| USB Bluetooth dongle | BD_ADDR 08:BE:AC:4D:3A:5B, USB | `hci0` — Kismet `linuxbluetooth` source | ✅ Active (rfkill unblocked) |
| GNSS (LoRaWAN/GNSS HAT) | L76K GNSS over UART | `/dev/ttyAMA0` (`GPS_DEVICE` in `.env`) | ✅ Active (3D fix) |
| RTL-SDR | RTL2838 (0bda:2838) | readsb (ADS-B), port 8080 | ✅ Active |
| DSI touchscreen | — | — | ⏸ Non-functional, blocking field readiness |

> **Bluetooth note:** onboard Pi Bluetooth shares the GPS-HAT UART (issue #48), so
> BT/BLE is captured via a **USB dongle** as a Kismet `linuxbluetooth` source on
> `hci0`. The dongle ships rfkill soft-blocked; `sudo rfkill unblock bluetooth`
> (persisted by systemd-rfkill) plus
> `source=hci0:name=bluetooth,type=linuxbluetooth` in `/etc/kismet/kismet_site.conf`
> makes it durable. Leave `bluetoothd` off.
>
> **Node mode:** chase runs `NODE_MODE=fixed` (set in `.env`) — fixed-node
> baseline-deviation scoring.

---

## Capabilities vs. Deployed Hardware

| Capability | Code status | Deployed on survkis | Deployed on chase |
|---|---|---|---|
| WiFi/BT scan (Kismet) | ✅ Complete | ✅ Hardware present | ✅ wlan1 + hci0 (BT dongle) active |
| GPS stamping | ✅ Complete | ✅ u-blox 8 on ttyACM0 | ✅ L76K on ttyAMA0 (3D fix) |
| ADS-B (readsb) | ✅ Complete | ❌ No SDR dongle | ✅ RTL-SDR present, ADS-B flowing |
| Drone RF | ✅ Complete | ❌ No SDR dongle | ⚠️ Disabled (`DRONE_RF_ENABLED=false`, SDR segfault #63) |
| FAA Remote ID | ✅ Complete | ⚠️ Requires Kismet (API key not set) | ✅ Active |
| Fixed/mobile detection modes | ✅ Phase 2 (main) | — | ✅ `NODE_MODE=fixed` |
| Entity/observation store | ✅ Complete | — | ✅ Recorded at poll site |
| Persistence scoring | ✅ Complete | ✅ (runs in orchestrator) | ✅ (fixed-mode FixedScoring) |
| Alert engine | ✅ Complete | ⚠️ Console only (no backend configured) | ⏸ Pending |
| Web GUI | ✅ Complete | ❌ Disabled (GUI_ENABLED=false) | ⏸ Pending |

---

## Module Registry

The canonical module list — file, class, and responsibility — is **`CLAUDE.md` →
Module Map**. Maintain it there; don't keep a second copy here. Systemd services
per module are in **`CLAUDE.md` → Deploy Directory**.

---

## Active Branches

Single-tier model: all work branches merge directly to `main` via PR. No integration branch.

| Branch | Owner | Purpose | Protected | Pi-tested | Notes |
|--------|-------|---------|-----------|-----------|-------|
| `main` | — | Stable releases — single protected target | Yes | ✅ | All PRs base here |
| `dev` | — | ~~Integration branch~~ | Yes | — | Retired 2026-05-30 — bypassed in practice; pending deletion after current PR cycle |
| `fix/field-hardening` | — | Field hardening (stale) | Yes | — | 0 unique commits — **abandoned** |
| `fix/orchestrator-gui-hardening` | — | GUI hardening (stale) | Yes | — | 0 unique commits — **abandoned** |

---

## Service Port Map

| Service | Port | Interface | Notes |
|---------|------|-----------|-------|
| Kismet Web UI | 2501 | 0.0.0.0 | Auth via cookie token |
| readsb / dump1090 | 8080 | localhost | JSON aircraft feed (`/data/aircraft.json`) |
| Main orchestrator + Web GUI | **8080** | 0.0.0.0 | ⚠️ **PORT COLLISION** — GUI and readsb both default to 8080 |

> **Port collision:** `modules/dump1090.py:18` defaults to `http://localhost:8080/data/aircraft.json`; `main.py:40` and `gui/server.py:36` both default `GUI_PORT=8080`. No conflict currently (GUI disabled, no SDR on survkis), but enabling both on the same node will fail. Resolution options: (a) set `GUI_PORT=8088` in `.env` when co-locating GUI and readsb on chase, or (b) bind readsb to a different port via `READSB_URL`. Document the chosen port in this table once chase is configured.

---

## Known Issues

| Issue | Affects | Node | Severity | Status |
|-------|---------|------|----------|--------|
| Multi-node coordination missing | Entire system | Both | High | Next major milestone — see Architectural Direction |
| GUI/readsb port collision (both default 8080) | chase deployment | chase | Medium | No conflict until GUI enabled alongside SDR — resolve at chase bring-up |
| Branch-creation ruleset blocks non-admin contributors | All contributors | — | Low | Admin bypass in use; bypass-list decision pending |
| Unsigned Pi commits hard-rejected if signature rule tightened | survkis, chase | Both | Medium | Fix: `git config gpg.format ssh` + `git config user.signingkey ~/.ssh/id_ed25519` — reuses existing deploy key, no new GPG setup needed |
| Telegram/Discord require manual credentials | Alert backends | Both | Low | Config gap only |
| No comprehensive frontend tests for Web GUI | gui/server.py | Both | Low | Partial unit tests exist |
| DroneRF disabled — RTL-SDR/libusb SIGSEGV during scan (#63) | Drone detection | chase | Medium | `DRONE_RF_ENABLED=false`; readsb-only. Re-enable blocked on #63 |
| DSI touchscreen non-functional | Local display | chase | Medium | Blocks field readiness |

---

## Security Note

Security policy and vulnerability reporting live in **`SECURITY.md`**.
Live operational specifics for this deployment: GitHub auth on each Pi uses per-Pi
SSH ed25519 keys in `~/.ssh/` **outside the repo** (`id_ed25519`, mode 600); no
key material has ever entered git history (verified 2026-05-30 via full history
scan). Commit signing is not yet configured on the Pis — see Known Issues.

---

## Error Handling Standardization Roadmap

**Steps:**
1. ~~Update CONTEXT.md~~ ✅
2. ~~`drain_detections()` on DroneRFModule + refactor call site~~ ✅ (Step 2, PR #31, SHA c10f844/538bcaa/7c23e0f — landed on main 2026-05-06)
3. ~~`core/exceptions.py` + `core/logging.py` + `drone_rf.py` migration~~ ✅ (Step 3, PR #31, merged 2026-05-12)
4. `ModuleHealth` dataclass in `SensorOrchestrator` — **NEXT** (not yet in codebase)
5. Migrate highest-risk modules to `core/` contracts
6. Expand error-path integration tests
7. Produce standardization checklist + PR template

**Expected Outcomes:**
- Zero private-attribute access
- Consistent, machine-readable error logging across all sensors
- Graceful degradation that never crashes the orchestrator
- Foundation ready for multi-node error propagation

---

## Verification Note (Added 2026-05-06)

**Mandatory rule (codified in AGENTS.md):** Any claim that a step is "completed" and references a code commit **must include the commit SHA and target branch**. Downstream work does not proceed until the SHA is independently verified via `git log` on the target branch **and** CI is green.

**Step 2 commits (for reference):**
- `c10f844c` — `modules/drone_rf.py` (added `drain_detections()` + `threading.Lock`)
- `538bcaa0` — `modules/orchestrator.py` (call site update)
- `7c23e0f7` — `tests/test_drone_rf.py` (required `TestDroneRFDDrainDetections`)

**Retrospective note (2026-05-06):** The three code commits for Step 2 landed directly on `main`, bypassing the `feature/* → dev → main` workflow. While technically correct and fully verified (280/280 on Pi 3B+), this violated the documented branch strategy. Future code changes will use proper feature branches. The verification rules added in AGENTS.md are intended to prevent recurrence.

---

## Environment & Dependencies

**Key packages:** asyncio, pyrtlsdr (pinned 0.2.93), python3-gps (apt only), gpsd, requests, aiohttp, geopandas, flask
**Requirements file:** `requirements.txt`
**Config files:** `.env` (gitignored) — per-node; see `.env.example` for all variables
**Data output paths:** `data/` (gitignored)

---

## Claude Code Session Notes

> Claude Code appends to this section at session close.

[2026-05-06] Completed Drone Remote ID detection module (`modules/remote_id.py`) + `test_remote_id.py`. Also added backend coverage in `test_alerts.py`. CollectedEvents switched to dataclass. gh CLI not installed on Pis — recommend `sudo apt install gh` + `gh auth login`.

[2026-05-30] Context refresh session (survkis). Diagnosed Grok's gutted CLAUDE.md (stub restored from 20e6484, PR #38 merged). Created fresh `dev` from main (old `dev/improvements` retired — its only unique commit 4b6fa69 gutted CI workflows). Reconciled branch strategy docs across AGENTS.md/CONTRIBUTING.md/README.md/CLAUDE.md/SECURITY.md (PRs merged to dev, then to main). Verified: no secrets in history, u-blox 8 on ttyACM0 (not ttyUSB0), GPS_DEVICE set in .env. Port collision noted: GUI + readsb both default 8080. ModuleHealth (Step 4) confirmed not yet built.

[2026-05-30] Branch model simplified to single-tier: all work branches → main directly. `dev` was bypassed in practice (all PRs during this session went to main); retired. Commit-style conflict resolved: AGENTS.md governs agent commit subjects ([agent] type(scope):); CLAUDE.md governs PR titles, release notes, human commits. Version string bumped to 0.4.3-alpha. README/setup.md/CLAUDE.md architecture tree synced. `dev` pending deletion after this PR cycle.

---

## Grok Merge / Update Notes

> Grok appends a note on every merge to main and at the start of any sequential standardization phase.

[2026-05-06 22:14 HST]
- Step 2 fully verified on Pi 3B+ (280/280).
- Added retrospective note acknowledging direct-to-main code commits (process gap).
- Updated README.md test count to 280.
- Next: Step 3 (standardized error handling).

---

## Handoff Checklist for Claude Code Session Start

- [ ] `git fetch origin && git checkout main && git pull origin main`
- [ ] Read **Current Sprint Focus** and **Error Handling Standardization Roadmap**
- [ ] Confirm hardware matches **Hardware & Adapter Map** for this node
- [ ] Check **`CLAUDE.md` → Module Map** for the module list
- [ ] Check **Known Issues** for any new blockers

## Handoff Checklist for Claude Code Session Close

- [ ] Commit with `[claude-code] type(scope): description` + `Tested: Pi/untested`
- [ ] Append session note to **Claude Code Session Notes**
- [ ] Push branch to `origin`
- [ ] Open PR base `main` (or report compare URL if gh unavailable)
- [ ] Update **`CLAUDE.md` → Module Map** if modules changed
