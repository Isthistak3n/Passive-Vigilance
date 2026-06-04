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

Functional verification on real hardware. **4 of 6 phases PASS.** Drone RF **fails** under sustained operation (a native SIGSEGV — §4, #63); ADS-B + Bluetooth are environmentally/hardware-degraded, not code faults:

| Phase | Result | Notes |
|---|---|---|
| GPS | ✅ PASS | 3D fix, mode 3, position + UTC stamping confirmed |
| WiFi / Kismet capture | ✅ PASS | Thousands of devices captured and tracked |
| Persistence / scoring | ✅ PASS | Engine scores and surfaces devices (gating behavior characterized — see §5) |
| GUI / map | ✅ PASS | Flask dashboard, live SSE, Leaflet map serve and populate |
| Drone RF | ❌ FAIL (sustained) | Tuner initializes and a sweep starts, but the scan path segfaults natively (`SIGSEGV`) within ~1 cycle under sustained operation — see §4 / #63. Disabled via `DRONE_RF_ENABLED=false` (#64). |
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

### Root cause — corrected 2026-06-03

**Correction.** An earlier version of this report named the ntfy unicode bug (below) as the
crash-loop cause and "exonerated" the SDR time-share. **That was wrong.** The ntfy bug is real
and is fixed, but it was *not* what crash-looped the node. Once journald was genuinely
persistent, the actual fault was captured: a native segfault, not a Python exception.

**The red herring (a real but separate bug).** A first pass surfaced a Python exception:
`UnicodeEncodeError: 'latin-1' codec can't encode character '—'` in `poll-kismet`. The ntfy
backend put the alert title (em dash, U+2014) into a latin-1 HTTP `Title` header, raising on
any persistence/aircraft alert. It is genuinely broken and is fixed (#58 — header values
sanitized; regression tests for a unicode title and a unicode device name). But it is a
separate, lower-frequency fault, not the crash loop.

**The actual cause.** Persistent journald never engaged at first — a Raspberry Pi OS
`40-rpi-volatile-storage` drop-in silently overrode the setting (fixed in #61). Once it was
truly persistent, a clean production-config soak crash-looped to **278 restarts** and the
journal captured the real signal — **`SIGSEGV` (status=11/SEGV), not a Python exception** —
every ~73 s at the SDR time-share cadence:

```
Found Rafael Micro R820T tuner
[R82XX] PLL not locked!
INFO modules.drone_rf: Drone RF scan started
libusb: debug [libusb_submit_transfer] transfer 0x...
systemd: Main process exited, code=killed, status=11/SEGV
```

This is a **native segfault in the RTL-SDR / libusb / pyrtlsdr stack during DroneRF scanning**
(issue #63). Being native, it produces no Python traceback, can't be caught in Python, kills
the single-process orchestrator, and systemd restarts it → loop. **The SDR time-share
hypothesis was correct, not exonerated.**

**Mitigation.** A `DRONE_RF_ENABLED=false` switch (#64) lets the node run capture + GUI +
ADS-B stably without the crashing scan path (validated live: NRestarts 0, zero SEGV). The
durable fix (#63) is to isolate DroneRF in a subprocess so a native crash can't take down the
orchestrator.

**Lesson recorded honestly:** the first diagnosis was confidently wrong because the
observability to see the real signal (`SIGSEGV`) wasn't actually in place yet. Fixing the
journal-persistence gap is what surfaced the truth — and is exactly why it was worth fixing.

---

## 5. Notes characterized during testing

- **Persistence gate on a stationary node:** with GPS present, the engine requires `PERSISTENCE_MIN_LOCATIONS` distinct 100 m GPS clusters. A fixed node only ever forms one cluster, so at the default (2) no device surfaces regardless of score. Relevant when running a stationary sensor; tracked separately.
- **Kismet `last_signal` field:** Kismet returns the simplified `signal/...` field under its **leaf key** (`kismet.common.signal.last_signal`). Reading any other key yields `None`. Live-validated: 2029 / 2410 devices report real RSSI (−106 … −20 dBm) once read correctly.
- **Journal retention:** the per-cycle SDR/DroneRF logging was loud enough to age out a crash window in ~2 h. Mitigated (persistent journald + per-cycle logs dropped to DEBUG).

---

## 6. Production-config validation soak — crash-looped (this exposed the real cause)

The follow-up soak ran at **production configuration** (alert threshold 0.7, ntfy fix in place)
to confirm clean operation. It did **not** stay clean. It held power `0x0` and showed no memory
leak, but crash-looped to **278 `SIGSEGV` restarts** over ~13 h — and that is precisely what
captured the true root cause (§4): the native DroneRF/libusb segfault (#63), now mitigated by
`DRONE_RF_ENABLED=false` (#64). An earlier draft of this section ("stable, NRestarts 0")
reflected only the first ~15 minutes and was premature.

---

## 7. Conclusion

Core capture functions are **validated on real hardware**: GPS, WiFi/Kismet capture, persistence scoring, and the web GUI/map all pass. Over the soak, **power (0x0), thermal (55.5 °C peak), GPS (mode-3 throughout), and memory (no leak) were all solid.**

The stability story is the honest one. An episodic crash loop was first **misdiagnosed** as an ntfy unicode bug — a real but *separate* fault (fixed in #58) — and the SDR time-share was wrongly exonerated. Once journald persistence was actually working (#61, after an RPi drop-in had silently overridden it), the true cause was captured: a **native `SIGSEGV` in the RTL-SDR / libusb DroneRF scan path** (#63). **DroneRF is therefore the one function that does not currently pass on this hardware**; it is disabled via `DRONE_RF_ENABLED=false` (#64) so the rest of the node runs stably, pending the durable fix (isolating DroneRF in a subprocess so a native crash can't kill the orchestrator). ADS-B and Bluetooth were environmentally/hardware-limited.

The honest summary: capture, GPS, power, thermal, and memory are solid; the DroneRF scan path has a native crash under investigation (#63); and this report's *first* crash diagnosis was wrong and is corrected here rather than quietly dropped.

---

## 8. Tracked follow-ups

| Item | Status |
|---|---|
| **DroneRF native SIGSEGV crash loop (actual crash-loop cause)** | **Issue #63 — open; mitigated by `DRONE_RF_ENABLED=false` (PR #64); durable fix = subprocess isolation** |
| ntfy unicode header crash (separate bug, not the crash loop) | Issue #57 → fixed in PR #58 |
| Kismet `last_signal` correct field | PR #54 (merged) + PR #59 (leaf-key correction, live-validated) |
| SDR/DroneRF log spam → DEBUG | PR #56 (merged) |
| journald persistent (500 MB) — RPi volatile drop-in override | PR #61 (merged); this is what captured #63 |
| GUI port-bind race | #49 — deferred |
| Fixed vs. mobile detection modes / stationary persistence gate | Design spec PR #62; tracked in #50 |
