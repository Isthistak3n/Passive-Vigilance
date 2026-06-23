# CONTEXT.md — Passive Vigilance Live State

> **Maintained by:** Claude Code + Cody (updated at session close and on merges to `main`)
> **Read by:** Claude Code at the start of every session
> **Last updated:** 2026-06-14
> **Updated by:** [claude-code] (survkis) — mobile GUI Nearby tab live, Web GUI capability row refreshed
>
> **Scope note:** this file holds *live state* only (hardware, ports, branches,
> handoffs). How-the-code-works lives in `CLAUDE.md`; how-we-work (roles, branch
> strategy, commit format) lives in `AGENTS.md`.

---

## Current Sprint Focus

**Active goal:** Validate the fixed-node prototype end to end. `chase` is up and
running fixed-mode scoring; the gate is a multi-day soak that crosses the baseline
freeze. See `docs/design-and-roadmap.md` (design plan + roadmap + soak validation).

**Recently landed (weekend of 2026-06-21/22):**
- **SDR pivot** (#158/#160/#159) — DroneRF retired; single dongle runs an N-band
  time-share of ADS-B + optional AIS + ACARS (on-Pi stress-testing). AIS/ACARS are
  VHF and antenna-gated.
- **Dashboard map reverted to plain online OSM** (#161/#162) — the offline-basemap
  feature was unreliable in the field and was removed in full.
- **Aircraft live-map push fix** (#163) — positioned contacts reach the map even when
  the fix isn't advancing.
- **GPS reader-thread fix** (#165/#166) — gpsd is consumed on a background thread so
  the stamped position no longer drifts behind real time (was minutes-to-hours on long
  runs; cosmetic on a fixed node, a real fix for mobile).

**`chase` config note:** **AIS disabled** (`AIS_ENABLED=false`, `SDR_CYCLE_SLICES=adsb:120`)
— the 1090 antenna can't receive VHF AIS and the time-share was blanking ADS-B; revisit
with a 2nd dongle + VHF antenna. Dashboard map = direct online OSM.

**Next:** P3 rolling baseline (alert-fatigue), P4 cross-session entity resolution, P5
fixed-mode GUI framing, and the SDR-pivot multi-day validation — see the roadmap. One
open residual: a bounded `_current_fix` oscillation when the asyncio GPS poll stalls
(separate from the gps.py reader; self-correcting).

**Blocking the broader vision:** multi-node coordination remains unbuilt — the key
enabling work for the tiered base-station + spoke architecture below.

---

## Architectural Direction (target — base station up, multi-node pending)

> The target architecture. `chase` as a base station is now up and running; the
> multi-node coordination layer below is still direction, not current state.

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

**Near-term focus:** `chase` base station is up; the focus is validating fixed-node
detection on it (the soak) before building the multi-node layer. `survkis` remains
a development and validation platform.

---

## Node Roles

Logical roles and aliases live in **`AGENTS.md` → Node Roles**. Live per-node
hardware and verified status are in **Hardware & Adapter Map** below — that is the
authority for what is actually present and working on each node.

> `pi3` / `pi4` are logical aliases only

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
| RTL8811CU WiFi adapter | Realtek , driver rtw88_8821cu | `wlan1` — monitor mode (Kismet) | ✅ Active |
| USB Bluetooth dongle | USB | `hci0` — Kismet `linuxbluetooth` source | ✅ Active (rfkill unblocked) |
| GNSS (LoRaWAN/GNSS HAT) | GNSS over UART | `/dev/ttyAMA0` (`GPS_DEVICE` in `.env`) | ✅ Active (3D fix) |
| RTL-SDR | RTL2838  | readsb (ADS-B), port 8080 | ✅ Active |
| DSI touchscreen | — | — | - |

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
| Drone RF | 🗄️ Retired | — | Replaced by the SDR decode cycle; `DRONE_RF_ENABLED=false`, code kept for reversibility |
| AIS (marine) | 🧩 Phase 1 (software) | — | Optional/off; needs a VHF antenna + AIS-catcher (`INSTALL_AIS=true`) — won't receive on the 1090 antenna |
| ACARS (aviation datalink) | 🔜 Phase 2 | — | >30s-held ADS-B trigger → decode + tail/flight-id correlation; VHF antenna best-effort |
| FAA Remote ID | ✅ Complete | ⚠️ Requires Kismet (API key not set) | ✅ Active |
| Fixed/mobile detection modes | ✅ Phase 2 (main) | — | ✅ `NODE_MODE=fixed` |
| Entity/observation store | ✅ Complete | — | ✅ Recorded at poll site |
| Persistence scoring | ✅ Complete | ✅ (runs in orchestrator) | ✅ (fixed-mode FixedScoring) |
| Alert engine | ✅ Complete | ⚠️ Console only (no backend configured) | ✅ Ntfy active |
| Web GUI | ✅ Complete | ✅ Active — standalone mobile GUI (`NODE_MODE=mobile`, `GUI_PORT=8088`), Nearby proximity tab, no Leaflet | ✅ Active (`GUI_PORT=8088`) |

---

## Module Registry

The canonical module list — file, class, and responsibility — is **`CLAUDE.md` →
Module Map**. Maintain it there; don't keep a second copy here. Systemd services
per module are in **`CLAUDE.md` → Deploy Directory**.

---

## Active Branches

Single-tier model: all work branches cut from `main` and merge back via PR — no
integration branch. In-flight branches and their status are the **open PRs on
GitHub**; this file no longer mirrors that list (it drifts). The one long-lived
work branch worth noting here is `feat/egregious-during-baseline` — the P0+P1+P2
stack currently deployed on `chase` for the soak; it PRs after the walk-test.

---

## Service Port Map

| Service | Port | Interface | Notes |
|---------|------|-----------|-------|
| Kismet Web UI | 2501 | 0.0.0.0 | Auth via cookie token |
| readsb / dump1090 | 8080 | localhost | JSON aircraft feed (`/data/aircraft.json`) |
| Main orchestrator + Web GUI | 8088 | 0.0.0.0 | `GUI_PORT=8088` on both nodes — avoids the readsb :8080 collision on chase |

> **Port note:** readsb/tar1090 and the GUI both *default* to 8080, so co-locating
> them needs the GUI moved. On `chase` this is resolved with `GUI_PORT=8088` in
> `.env` (GUI reachable at `http://<chase>:8088`). On `survkis` there is no SDR, so
> no collision, but `GUI_PORT=8088` is set the same way for consistency —
> `GUI_ENABLED=true`, serving the standalone mobile GUI at `http://<survkis>:8088`.

---

## Known Issues

| Issue | Affects | Node | Severity | Status |
|-------|---------|------|----------|--------|
| Multi-node coordination missing | Entire system | Both | High | Next major milestone — see Architectural Direction |
| GUI/readsb port collision (both default 8080) | chase deployment | chase | Low | ✅ Resolved — `GUI_PORT=8088` on chase |
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

> Status (2026-06): Steps 1–3 are done. Step 4 (`ModuleHealth`) is still unbuilt,
> though the sensor **stall watchdog** (#79) now covers part of the same need
> (flagging a silently-stalled sensor). The active product roadmap is
> `docs/design-and-roadmap.md` (P0–P7); this error-handling track is a
> background cleanup, not the current sprint.

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

[2026-06-07] chase fixed-node session. Landed P0 endurance hardening (#74), the approaching trigger (#75), Bluetooth reboot durability (#76), reliability fixes — stall watchdog + GUI bind-retry (#79), and the dashboard baseline header + aircraft-panel fixes (#80). Diagnosed and fixed two chase outages: a silent sensor stall (counters frozen while the health banner stayed green — hence the watchdog) and a post-reboot crash loop (NODE_MODE missing from `.env`). Restarted chase clean on a wiped baseline; a 48h soak is running the P0+P1+P2 stack (`feat/egregious-during-baseline`), freezing ~2026-06-09 22:55 UTC, after which the P1 walk-test runs. Bluetooth re-enabled and made durable. Test suite at 417. Docs de-duplicated (#81) and refreshed (this pass); `_VERSION` bumped to 0.6.0-alpha. Open finding: DSI touchscreen still non-functional; DroneRF still disabled on #63.

[2026-06-14] survkis mobile-GUI session. `NODE_MODE=mobile` + `GUI_ENABLED=true`/`GUI_PORT=8088` now active on survkis (previously disabled). Landed a standalone mobile GUI (#113): `gui/templates/mobile.html` + `gui/static/mobile.js`, served instead of the Leaflet `index.html` when `NODE_MODE=mobile`; new `/api/nearby` live proximity feed (independent of the persistence/GPS-cluster gate), with a "Nearby" tab showing RSSI-sorted device cards, proximity dots, and alert-tier accents cross-referenced from the persistence feed. Followed up (#116) with a CSS selector bug fix (`.nearby-feed` never matched `#nearby-feed`, so the feed couldn't scroll past the first screenful) and added floating up/down paging buttons as a touchscreen fallback. Verified end-to-end: service restarted clean, `/api/nearby` serving live data, kiosk on the 800x480 DSI screen rendering and scrolling correctly (`grim` screenshots). Test suite at 537. Planned: a drive test to validate Nearby-feed behavior at speed and mobile persistence scoring (location-diversity) against real traffic.

---

## Grok Merge / Update Notes

> Grok appends a note on every merge to main and at the start of any sequential standardization phase.

[2026-05-06]
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
