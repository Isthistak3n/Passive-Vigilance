# CONTEXT.md — Passive Vigilance Live State

> **Maintained by:** Grok (updated on every merge to `main`)  
> **Read by:** Claude Code at the start of every session  
> **Last updated:** 2026-05-06  
> **Updated by:** [grok]

---

## Current Sprint Focus

**Active goal:** Harden SHARED SDR mode (P1) + full multi-AI workflow setup  
**Blocking issues:** None  
**Next planned module:** WiGLE uploader + Shapefile writer (P2)

---

## Node Status

| Node | Hostname   | OS            | Python | Last validated | Status     |
|------|------------|---------------|--------|----------------|------------|
| Pi 1 | pv-node-1 | Debian ARM64 | 3.x    | 2026-05-05     | 🟢 Online  |
| Pi 2 | pv-node-2 | Debian ARM64 | 3.x    | 2026-05-05     | 🟢 Online  |

---

## Module Registry

| Module                  | File                          | Node(s)     | Service name      | Status                  | Last tested |
|-------------------------|-------------------------------|-------------|-------------------|-------------------------|-------------|
| SDR Coordinator         | `modules/sdr_coordinator.py`   | Pi 1        | —                 | ✅ Hardened (P1)         | 2026-05-05  |
| Drone RF Detection      | `modules/drone_rf.py`          | Pi 1        | —                 | ✅ Hardened (P1)         | 2026-05-05  |
| Kismet WiFi/BT Scanner  | `modules/kismet.py`            | Pi 1 + Pi 2 | `kismet.service`  | ✅ Stable               | 2026-05-05  |
| ADS-B (readsb)          | `modules/dump1090.py`          | Pi 1        | `readsb.service`  | ✅ Stable               | 2026-05-05  |
| GPS Handler             | `modules/gps.py`               | Pi 1 + Pi 2 | —                 | ✅ Stable               | 2026-05-05  |
| Persistence Engine      | `modules/persistence.py`       | Pi 1 + Pi 2 | —                 | ✅ Stable               | 2026-05-05  |
| Alert Factory           | `modules/alerts.py`            | Pi 1 + Pi 2 | —                 | ✅ Stable               | 2026-05-05  |
| Main Orchestrator       | `main.py`                      | Pi 1 + Pi 2 | `pv-main.service` | 🚧 In progress (P1 + workflow) | 2026-05-06 |

---

## Active Branches

| Branch                          | Owner        | Purpose                              | Pi-tested | PR  |
|---------------------------------|--------------|--------------------------------------|-----------|-----|
| `main`                          | —           | Stable, validated code only          | ✅        | —  |
| `feature/harden-sdr-shared-mode`| [claude-code] + [grok] | P1 SDR hardening + multi-AI workflow | ✅        | #28 |

---

## Known Issues

| Issue                                      | Affects              | Node     | Severity | Status      |
|--------------------------------------------|----------------------|----------|----------|-------------|
| SDR handshake retries break old tests      | `test_sdr_manager.py` | Both     | Low      | ✅ Fixed    |
| Flake8 E306/E701 formatting in main.py     | `main.py`            | Both     | Low      | ✅ Fixed    |
| wlan0 monitor mode drops after ~2hrs       | Kismet               | Pi 2     | Medium   | Open        |

---

## Hardware & Adapter Map

| Node | Adapter       | Interface | Role                  | Driver      |
|------|---------------|-----------|-----------------------|-------------|
| Pi 1 | RTL-SDR       | sdr0      | ADS-B + Drone RF      | rtlsdr      |
| Pi 1 | WiFi adapter  | wlan0     | Kismet monitor mode   | rtl8812au   |
| Pi 2 | WiFi adapter  | wlan0     | Wardriving            | rtl8812au   |
| Pi 2 | Bluetooth     | hci0      | BT scanning           | bluez       |
| Both | GPS module    | /dev/ttyS0| GPS fix               | gpsd        |

---

## Service Port Map

| Service              | Port | Interface | Accessible via |
|----------------------|------|-----------|----------------|
| Kismet Web UI        | 2501 | 0.0.0.0   | Tailscale      |
| readsb / dump1090    | 8080 | 0.0.0.0   | Tailscale      |
| Main orchestrator    | 8088 | 0.0.0.0   | Tailscale      |

---

## Environment & Dependencies

**Key packages:** asyncio, pyserial, python-rtlsdr, gpsdclient, requests, geopandas  
**Requirements file:** `requirements.txt`  
**Config files:** `.env` (gitignored), `config/` (structure only)  
**Data output paths:** `data/` (gitignored)

---

## Claude Code Session Notes

> Claude Code appends to this section at session close.  
> Format: `[YYYY-MM-DD] [Pi 1/2] note`

[2026-05-06] [Pi 1] P1 SDR hardening complete. All 240 tests passing. Lint clean. Ready for PR review.

---

## Grok Merge Notes

> Grok appends a note on every merge to main.

[2026-05-06] Created `AGENTS.md` + `CONTEXT.md` with multi-AI coordination rules. P1 branch now ready for merge.

---

## Handoff Checklist for Claude Code Session Start

- [ ] `git pull origin <active-branch>`
- [ ] Read **Current Sprint Focus** above
- [ ] Check **Known Issues** for your node
- [ ] Confirm your hardware adapters match **Hardware & Adapter Map**
- [ ] Check **Active Branches** — don't touch branches owned by Grok

## Handoff Checklist for Claude Code Session Close

- [ ] Commit all work with `[claude-code] type(scope): description`
- [ ] Note `Tested: Pi 1/2` or `Tested: untested` in commit
- [ ] Append session note to **Claude Code Session Notes** above
- [ ] Push branch
- [ ] Update any module status changes in **Module Registry**
- [ ] If ready for review: comment on the PR or ping in issue thread
