# CONTEXT.md — Passive Vigilance Live State

> **Maintained by:** Grok (updated on every merge to `main`)  
> **Read by:** Claude Code at the start of every session  
> **Last updated:** 2026-05-06  
> **Updated by:** [grok]

---

## Current Sprint Focus

**Active goal:** Review and merge orchestrator-refactor branch first  
**Blocking issues:** None  
**Next after merge:** Integrate and review feature/remote-id (Drone Remote ID detection)

---

## Node Status

| Node | Hostname   | OS            | Python | Last validated | Status     |
|------|------------|---------------|--------|----------------|------------|
| Pi 1 | pv-node-1 | Debian ARM64 | 3.x    | 2026-05-06     | 🟢 Online  |
| Pi 2 | pv-node-2 | Debian ARM64 | 3.x    | 2026-05-06     | 🟢 Online  |

---

## Module Registry

| Module                     | File                              | Node(s)     | Service              | Status             | Last tested |
|----------------------------|-----------------------------------|-------------|----------------------|--------------------|-------------|
| GPS Handler                | `modules/gps.py`                   | Both        | —                    | ✅ Stable           | 2026-05-06  |\n| Kismet WiFi/BT Scanner     | `modules/kismet.py`                | Both        | `kismet.service`     | ✅ Stable           | 2026-05-06  |\n| ADS-B (readsb)             | `modules/dump1090.py`              | Pi 1        | `readsb.service`     | ✅ Stable           | 2026-05-06  |\n| Drone RF Detection         | `modules/drone_rf.py`              | Pi 1        | —                    | ✅ Stable           | 2026-05-06  |\n| SDR Coordinator            | `modules/sdr_coordinator.py`       | Pi 1        | —                    | ✅ Hardened (P1)    | 2026-05-06  |\n| SDR Manager                | `modules/sdr_manager.py`           | Both        | —                    | ✅ Stable           | 2026-05-06  |\n| Persistence Engine         | `modules/persistence.py`           | Both        | —                    | ✅ Stable           | 2026-05-06  |\n| Alert Factory              | `modules/alerts.py`                | Both        | —                    | ✅ Stable           | 2026-05-06  |\n| Ignore List                | `modules/ignore_list.py`           | Both        | —                    | ✅ Stable           | 2026-05-06  |\n| MAC Utils                  | `modules/mac_utils.py`             | Both        | —                    | ✅ Stable           | 2026-05-06  |\n| Probe Analyzer             | `modules/probe_analyzer.py`        | Both        | —                    | ✅ Stable           | 2026-05-06  |\n| Shapefile Writer           | `modules/shapefile.py`             | Both        | —                    | ✅ Stable           | 2026-05-06  |\n| KML Writer                 | `modules/kml_writer.py`            | Both        | —                    | ✅ Stable           | 2026-05-06  |\n| WiGLE Uploader             | `modules/wigle.py`                 | Pi 2        | —                    | ✅ Stable           | 2026-05-06  |\n| Sensor Orchestrator        | `modules/orchestrator.py`          | Both        | —                    | ✅ Stable (new)     | 2026-05-06  |\n| Main Orchestrator          | `main.py`                          | Both        | `pv-main.service`    | ✅ Stable           | 2026-05-06  |\n| Web GUI                    | `gui/server.py`                    | Both        | —                    | ✅ Stable           | 2026-05-06  |\n| Remote ID Detector         | `modules/remote_id.py`             | Both        | —                    | ✅ Complete (local) | 2026-05-06  |\n
---

## Active Branches

| Branch                    | Owner          | Purpose                              | Pi-tested | PR |
|---------------------------|----------------|--------------------------------------|-----------|----|
| `main`                    | —              | Stable, validated code               | ✅        | —  |
| `feature/remote-id`       | [claude-code]  | Drone Remote ID detection (complete) | ✅ (local)| —  |
| `orchestrator-refactor`   | [claude-code]  | SensorOrchestrator split + SDR tests | ✅ (local)| —  |

---

## Known Issues

| Issue | Affects | Node | Severity | Status |
|-------|---------|------|----------|--------|
| Multi-node coordination missing | Entire system | Both | High | Not started |
| Telegram/Discord require credentials | Alert backends | Both | Low | Config gap |
| No frontend tests for Web GUI | gui/server.py | Both | Low | Not started |

---

## Hardware & Adapter Map

| Node | Adapter | Interface | Role | Driver |
|------|---------|-----------|------|--------|
| Pi 1 | RTL-SDR | sdr0 | ADS-B + Drone RF | rtlsdr |
| Pi 1 | WiFi adapter | wlan0 | Kismet monitor mode | rtl8812au |
| Pi 2 | WiFi adapter | wlan0 | Wardriving + Remote ID | rtl8812au |
| Pi 2 | Bluetooth | hci0 | BT scanning + Remote ID | bluez |
| Both | GPS module | /dev/ttyS0 | GPS fix | gpsd |

---

## Service Port Map

| Service | Port | Interface | Accessible via |
|---------|------|-----------|----------------|
| Kismet Web UI | 2501 | 0.0.0.0 | Tailscale |
| readsb / dump1090 | 8080 | 0.0.0.0 | Tailscale |
| Main orchestrator + GUI | 8088 | 0.0.0.0 | Tailscale |

---

## Environment & Dependencies

**Key packages:** asyncio, pyserial, python-rtlsdr, gpsdclient, requests, geopandas, flask  
**Requirements file:** `requirements.txt`  
**Config files:** `.env` (gitignored)  
**Data output paths:** `data/` (gitignored)

---

## Claude Code Session Notes

> Claude Code appends to this section at session close.

[2026-05-06] Completed Drone Remote ID detection module (`modules/remote_id.py`) + `test_remote_id.py`. Also added backend coverage in `test_alerts.py`. CollectedEvents switched to dataclass (good decision). gh CLI not installed on Pis — recommend `sudo apt install gh` + `gh auth login`.

---

## Grok Merge Notes

> Grok appends a note on every merge to main.

[2026-05-06] Updated CONTEXT.md with orchestrator-refactor and feature/remote-id branches. Ready for review of orchestrator-refactor first.

---

## Handoff Checklist for Claude Code Session Start

- [ ] `git fetch origin && git checkout main && git pull origin main`
- [ ] Read **Current Sprint Focus** and **Known Issues** above
- [ ] Confirm hardware matches **Hardware & Adapter Map**
- [ ] Check **Module Registry** for latest status

## Handoff Checklist for Claude Code Session Close

- [ ] Commit with `[claude-code] type(scope): description`
- [ ] Note `Tested: Pi 1/2` in commit
- [ ] Append session note to **Claude Code Session Notes**
- [ ] Push branch
- [ ] Update any status changes in **Module Registry** if needed
