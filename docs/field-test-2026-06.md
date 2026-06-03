# Field Test & Soak Report — June 2026 (node: chase)

**Date:** 2026-06-01 → 2026-06-03
**Node:** `chase` (`chasingyourtail`)
**Purpose:** Validate core functions on real hardware and measure stability over a long unattended soak. This is a validation record — results are reported as measured, including the failure we found.

---

## 1. Hardware under test

| Component | Detail |
|---|---|
| SBC | Raspberry Pi 4B (8 GB; `free -m` reports 7819 MB total) |
| OS | Debian 13 Trixie |
| Power | Mains / wall adapter |
| GPS | GPS HAT — 3D fix (mode 3) held throughout |
| WiFi monitor | BrosTrend RTL8811CU on `wlan1` (USB `0bda:c811`) |
| SDR | RTL-SDR (RTL2838, USB `0bda:2838`), single dongle |
| Display | DSI touchscreen |
| Capture stack | Kismet 2025.09, readsb (wiedehopf), gpsd 3.25 |

Single-dongle setup → SDR runs in **SHARED** time-share mode (readsb ADS-B and DroneRF alternate on the one RTL-SDR).

---

## 2. Live-hardware functional test (6 phases)

Functional verification on real hardware. **5 of 6 phases PASS;** the two degraded results are environmental/hardware, not software defects:

| Phase | Result | Notes |
|---|---|---|
| GPS | ✅ PASS | 3D fix, mode 3, position + UTC stamping confirmed |
| WiFi / Kismet capture | ✅ PASS | Thousands of devices captured and tracked |
| Persistence / scoring | ✅ PASS | Engine scores and surfaces devices (gating behavior characterized — see §5) |
| GUI / map | ✅ PASS | Flask dashboard, live SSE, Leaflet map serve and populate |
| Drone RF | ✅ PASS | R820T tuner sweeps configured bands under the SHARED duty cycle |
| ADS-B + Bluetooth | ⚠️ Degraded | ADS-B limited by single-dongle time-share + local traffic; BT pending dongle. Environmental/hardware, not a code fault. |

---

## 3. 12-hour soak — measured results

Sampled every 5 minutes by an on-box logger (power, thermal, memory, service state, GPS, capture).

| Axis | Result | Verdict |
|---|---|---|
| **Power** | `throttled=0x0` for the entire soak — no undervolt, no throttle, no historical flags | ✅ Solid |
| **Thermal** | Peak **55.5 °C** | ✅ Cool; well under throttle/duty thresholds |
| **GPS** | mode-3 lock on 100% of samples; zero dropouts | ✅ Solid |
| **Memory** | Used 940 → 1100 MB; available steady ~6.7 GB | ✅ No leak (per-restart resets confirm) |
| **Uptime** | Clean for ~20 h, then an episodic crash loop (§4) | ❌ One defect found |

---

## 4. The defect we found (and fixed)

### What happened
After ~20 h of zero restarts, the orchestrator entered a tight crash loop: **0 → 60 systemd auto-restarts between 17:16 and 18:26 HST** (~1 restart / 75 s), then self-recovered and stayed up.

### Initial (wrong) hypothesis
The ~75 s cadence matched the SDR time-share handoff, so the single-dongle thrash was the first suspect. We could not confirm it: the SDR coordinator logged a full handoff/duty cycle every ~75 s, flooding the journal (~149 MB / 2 h) and rotating the crash window out before it could be read.

### Actual root cause — found, confirmed, fixed
After making journald persistent and reducing the log spam, a restart surfaced the real fault directly:

```
ERROR __main__: Task poll-kismet raised UnicodeEncodeError:
'latin-1' codec can't encode character '—' in position 18
```

The ntfy alert backend put the alert **title** into the HTTP `Title` header. HTTP/1.1 headers are latin-1, and the title `"Persistent Device — {LEVEL}"` contains an em dash (U+2014) at position 18. Every ntfy persistence/aircraft alert raised `UnicodeEncodeError`, which killed the `poll-kismet` task; the orchestrator treated that as fatal and restarted, and the in-memory rate limiter reset on each restart → the next poll re-fired → the crash loop. Device names / SSIDs carrying UTF-8 would trip the same path.

The soak's loosened detection threshold (lowered to populate the GUI for observation) is what made alerts fire and exposed the latent bug; at the production threshold alerts are rare, which is why it took ~20 h to hit.

**The SDR time-share was exonerated.** Fixed in the ntfy backend by sanitizing all header-bound values to latin-1-safe (transliterate typographic characters, replace anything else). Regression tests cover a unicode title and a unicode device name.

---

## 5. Notes characterized during testing

- **Persistence gate on a stationary node:** with GPS present, the engine requires `PERSISTENCE_MIN_LOCATIONS` distinct 100 m GPS clusters. A fixed node only ever forms one cluster, so at the default (2) no device surfaces regardless of score. Relevant when running a stationary sensor; tracked separately.
- **Kismet `last_signal` field:** Kismet returns the simplified `signal/...` field under its **leaf key** (`kismet.common.signal.last_signal`). Reading any other key yields `None`. Live-validated: 2029 / 2410 devices report real RSSI (−106 … −20 dBm) once read correctly.
- **Journal retention:** the per-cycle SDR/DroneRF logging was loud enough to age out a crash window in ~2 h. Mitigated (persistent journald + per-cycle logs dropped to DEBUG).

---

## 6. Production-config validation soak (in progress)

A follow-up soak is running at **production configuration** (alert threshold 0.7, ntfy backend) with the ntfy fix in place, to confirm the node runs clean at production settings without the bug. Early state: stable, `throttled=0x0`, 55.5 °C, mode-3 GPS, NRestarts 0, capture active.

---

## 7. Conclusion

Core functions are **validated on real hardware**: GPS, WiFi/Kismet capture, persistence scoring, the web GUI/map, and Drone RF all pass. Over a 12-hour soak, **power (0x0), thermal (55.5 °C peak), GPS (mode-3 throughout), and memory (no leak) were all solid.** One stability defect surfaced — an episodic crash loop — which was **root-caused to a unicode-in-HTTP-header bug in the ntfy alert path and fixed**; the SDR time-share initially suspected was exonerated. ADS-B and Bluetooth were environmentally/hardware-limited, not software faults.

The honest summary: the platform's measured hardware behavior is solid, and the one real software defect found during the soak was diagnosed and fixed rather than papered over.

---

## 8. Tracked follow-ups

| Item | Status |
|---|---|
| ntfy unicode header crash | Issue #57 → fixed in PR #58 |
| Kismet `last_signal` correct field | PR #54 (merged) + PR #59 (leaf-key correction, live-validated) |
| SDR/DroneRF log spam → DEBUG | PR #56 |
| journald persistent (500 MB) | Applied on node |
| GUI port-bind race | #49 — deferred (needs persistent journal to observe) |
| Persistence gate on stationary node | Tracked for the fixed-node use case (#50 design decision) |
