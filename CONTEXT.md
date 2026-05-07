# CONTEXT.md — Passive Vigilance Live State

> **Maintained by:** Grok (updated on every merge to `main`)  
> **Read by:** Claude Code at the start of every session  
> **Last updated:** 2026-05-06 22:14 HST  
> **Updated by:** [grok] — Added retrospective note on Step 2 branch strategy gap

---

## Current Sprint Focus

**Active goal:** Clean Module Integration + Stabilization  
**Priorities (sequential):**  
1. Update CONTEXT.md (this file)  
2. Implement `drain_detections()` on DroneRFModule + remove private attribute access  
3. Introduce shared `core/exceptions.py` + `core/logging.py` and begin module migration (starting with `drone_rf.py`)  
4. Add `ModuleHealth` tracking  
5. Produce standardization checklist + PR template  

**Blocking issues:** None  
**Next after stabilization:** Expand integration test coverage + prepare multi-node foundation (back burner until single-node is hardened)

---

## Module Registry

| Module                     | File                              | Node(s)     | Service              | Status             | Last tested |
|----------------------------|-----------------------------------|-------------|----------------------|--------------------|-------------|
| GPS Handler                | `modules/gps.py`                   | Both        | —                    | ✅ Stable           | 2026-05-06  |
| Kismet WiFi/BT Scanner     | `modules/kismet.py`                | Both        | `kismet.service`     | ✅ Stable           | 2026-05-06  |
| ADS-B (readsb)             | `modules/dump1090.py`              | Pi 1        | `readsb.service`     | ✅ Stable           | 2026-05-06  |
| Drone RF Detection         | `modules/drone_rf.py`              | Pi 1        | —                    | ✅ Stable           | 2026-05-06  |
| SDR Coordinator            | `modules/sdr_coordinator.py`       | Pi 1        | —                    | ✅ Hardened (P1)    | 2026-05-06  |
| SDR Manager                | `modules/sdr_manager.py`           | Both        | —                    | ✅ Stable           | 2026-05-06  |
| Persistence Engine         | `modules/persistence.py`           | Both        | —                    | ✅ Stable           | 2026-05-06  |
| Alert Factory              | `modules/alerts.py`                | Both        | —                    | ✅ Stable           | 2026-05-06  |
| Ignore List                | `modules/ignore_list.py`           | Both        | —                    | ✅ Stable           | 2026-05-06  |
| MAC Utils                  | `modules/mac_utils.py`             | Both        | —                    | ✅ Stable           | 2026-05-06  |
| Probe Analyzer             | `modules/probe_analyzer.py`        | Both        | —                    | ✅ Stable           | 2026-05-06  |
| Shapefile Writer           | `modules/shapefile.py`             | Both        | —                    | ✅ Stable           | 2026-05-06  |
| KML Writer                 | `modules/kml_writer.py`            | Both        | —                    | ✅ Stable           | 2026-05-06  |
| WiGLE Uploader             | `modules/wigle.py`                 | Pi 2        | —                    | ✅ Stable           | 2026-05-06  |
| Sensor Orchestrator        | `modules/orchestrator.py`          | Both        | —                    | ✅ Stable (merged)  | 2026-05-06  |
| Main Orchestrator          | `main.py`                          | Both        | `pv-main.service`    | ✅ Stable           | 2026-05-06  |
| Web GUI                    | `gui/server.py`                    | Both        | —                    | ✅ Stable           | 2026-05-06  |
| Remote ID Detector         | `modules/remote_id.py`             | Both        | —                    | ✅ Complete         | 2026-05-06  |

**Installation note:** The complete module set (all 18 entries above) is included automatically when you run `git clone https://github.com/Isthistak3n/Passive-Vigilance.git` followed by `pip install -r requirements.txt` or the recommended one-command installer `sudo bash deploy/install.sh`. No additional modules need to be installed separately.

**Test Coverage Note:** 280 tests passing (as of 2026-05-06 Pi 3B+ validation).

---

## Active Branches

| Branch                          | Owner     | Purpose                                      | Protected | Pi-tested | Notes |
|---------------------------------|-----------|----------------------------------------------|-----------|-----------|-------|
| `main`                          | —         | Stable, validated code (orchestrator-refactor + remote-id merged) | Yes      | ✅        | Current production branch |
| `fix/field-hardening`           | —         | Field deployment hardening                   | Yes      | —         | Protected |
| `fix/orchestrator-gui-hardening`| —         | Orchestrator + GUI refinements               | Yes      | —         | Protected |

---

## Known Issues

| Issue                                      | Affects          | Node | Severity | Status                  |
|--------------------------------------------|------------------|------|----------|-------------------------|
| Multi-node coordination missing            | Entire system    | Both | High     | Back burner (after stabilization) |
| Telegram/Discord require manual credentials| Alert backends   | Both | Low      | Config gap              |
| No comprehensive frontend tests for Web GUI| gui/server.py    | Both | Low      | Partial (unit tests exist) |

---

## Verification Note (Added 2026-05-06)

**Mandatory rule (now codified in AGENTS.md):** Any claim that a step is "completed" and references a code commit **must include the commit SHA and target branch**. Downstream work does not proceed until the SHA is independently verified via `git log` on the target branch **and** CI is green.

**Step 2 commits (for reference):**
- `c10f844c` — `modules/drone_rf.py` (added `drain_detections()` + `threading.Lock`)
- `538bcaa0` — `modules/orchestrator.py` (call site update)
- `7c23e0f7` — `tests/test_drone_rf.py` (required `TestDroneRFDDrainDetections`)

**Retrospective note (2026-05-06):** The three code commits for Step 2 landed directly on `main`, bypassing the `feature/* → dev → main` workflow. While technically correct and fully verified (280/280 on Pi 3B+), this violated the documented branch strategy. Future code changes will use proper feature branches. The verification rules added in AGENTS.md are intended to prevent recurrence.

---

## Error Handling Standardization Roadmap (Current Phase — Sequential)

This phase addresses the ad-hoc error patterns observed in `modules/orchestrator.py` (health dicts, degraded counters, reconnect logic) while preserving the excellent existing resilience.

**Planned Steps (executed sequentially):**  
1. Update this CONTEXT.md (completed — current step)  
2. Add `drain_detections()` method to `DroneRFModule` and refactor the single call site in `orchestrator.py`  
3. Create `core/exceptions.py` (custom hierarchy + `ErrorSeverity`) + `core/logging.py` (consistent structured logger)  
4. Introduce lightweight `ModuleHealth` dataclass in `SensorOrchestrator`  
5. Migrate highest-risk modules (`drone_rf.py` → `remote_id.py` → others) to use new contracts  
6. Expand error-path integration tests  
7. Produce final standardization checklist + PR template for team

**Expected Outcomes:**  
- Zero private-attribute access  
- Consistent, machine-readable error logging across all sensors  
- Graceful degradation that never crashes the orchestrator  
- Foundation ready for multi-node error propagation

---

## Hardware & Adapter Map

(unchanged — verified accurate)

| Node | Adapter | Interface | Role | Driver |
|------|---------|-----------|------|--------|
| Pi 1 | RTL-SDR | sdr0 | ADS-B + Drone RF | rtlsdr |
| Pi 1 | WiFi adapter | wlan0 | Kismet monitor mode | rtl8812au |
| Pi 2 | WiFi adapter | wlan0 | Wardriving + Remote ID | rtl8812au |
| Pi 2 | Bluetooth | hci0 | BT scanning + Remote ID | bluez |
| Both | GPS module | /dev/ttyS0 | GPS fix | gpsd |

---

## Service Port Map

(unchanged)

| Service | Port | Interface | Accessible via |
|---------|------|-----------|----------------|
| Kismet Web UI | 2501 | 0.0.0.0 | Tailscale |
| readsb / dump1090 | 8080 | 0.0.0.0 | Tailscale |
| Main orchestrator + GUI | 8088 | 0.0.0.0 | Tailscale |

---

## Environment & Dependencies

(unchanged — verified)

**Key packages:** asyncio, pyserial, python-rtlsdr, gpsdclient, requests, geopandas, flask  
**Requirements file:** `requirements.txt`  
**Config files:** `.env` (gitignored)  
**Data output paths:** `data/` (gitignored)

---

## Claude Code Session Notes

> Claude Code appends to this section at session close.

[2026-05-06] Completed Drone Remote ID detection module (`modules/remote_id.py`) + `test_remote_id.py`. Also added backend coverage in `test_alerts.py`. CollectedEvents switched to dataclass (good decision). gh CLI not installed on Pis — recommend `sudo apt install gh` + `gh auth login`.

---

## Grok Merge / Update Notes

> Grok appends a note on every merge to main **and** at the start of any sequential standardization phase.

[2026-05-06 22:14 HST]  
- Step 2 fully verified on Pi 3B+ (280/280).  
- Added retrospective note acknowledging direct-to-main code commits (process gap).  
- Updated README.md test count to 280.  
- Next: Step 3 (standardized error handling).

---

## Handoff Checklist for Claude Code Session Start

- [ ] `git fetch origin && git checkout main && git pull origin main`  
- [ ] Read **Current Sprint Focus** and **Error Handling Standardization Roadmap** above  
- [ ] Confirm hardware matches **Hardware & Adapter Map**  
- [ ] Check **Module Registry** for latest status  
- [ ] Verify no private `_detections` access remains after Step 2

## Handoff Checklist for Claude Code Session Close

- [ ] Commit with `[claude-code] type(scope): description`  
- [ ] Note `Tested: Pi 1/2` in commit  
- [ ] Append session note to **Claude Code Session Notes**  
- [ ] Push branch  
- [ ] Update any status changes in **Module Registry** if needed
