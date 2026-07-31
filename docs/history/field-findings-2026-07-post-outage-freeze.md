# Field Findings — Post-Outage Recovery & First Unsmeared Freeze, July 2026

**Purpose:** capture the operational evidence from the late-July fixed-node run —
an unplanned power loss, a clean cold-boot recovery, and the first baseline that
was learned end-to-end with the active-window smearing fix in effect. This is the
test evidence behind the storage and scoring items in
[design-and-roadmap.md](../design-and-roadmap.md), and it closes the loop on the
"one clean fixed-mode cycle from learn to freeze, no restarts" milestone tracked
in #191. Configuration and hardware facts live in `CLAUDE.md` / `CONTEXT.md` and
are cross-referenced here rather than repeated. Relates to #191, #232, #233.

---

## The run at a glance

| Fact | Value |
|---|---|
| Trigger | Unplanned power loss, cold boot 2026-07-27 |
| Recovery | Fully automatic — no manual intervention |
| Continuous uptime observed | ~85h, orchestrator restarts = 0 |
| Baseline window | Fresh 108h window, learning start 2026-07-27 |
| Freeze | Completed on schedule, clean learn→score transition |
| Sensors | GPS / Kismet / BLE / ADS-B / RemoteID / DroneRF all active throughout |

---

## Cold-boot recovery held

The node lost power without warning and came back on its own. Every service in
the stack — the orchestrator, Kismet, gpsd, readsb, and tar1090 — started at boot
with no hand-holding, and the orchestrator then ran for roughly 85 hours with a
restart count of zero and no failed units. The known post-reboot traps did **not**
bite this time: the node came up in fixed mode with the correct alert backend, the
Bluetooth controller came up enabled, GPS reacquired a 3D fix at the fixed
location, and Kismet's Wi-Fi capture came up without the boot-race greyout. In
short, the boot path is now robust enough to survive a bare power-cut unattended.

## The first unsmeared freeze

The important result is the baseline itself. This 108-hour window was learned with
the active-window fix (#232 / #233) live for its **entire** duration — verified by
the deployed checkout predating the running process, with the recency window at
its corrected default rather than the old "learn everything Kismet still
remembers" behaviour. That matters because every previous fixed-node baseline was
smeared: transient passers-by were being stamped as all-day residents because the
node kept re-learning devices that Kismet still listed long after they had left.

Post-freeze, scoring behaves the way the design intends. Each cycle flags on the
order of twenty-odd devices, overwhelmingly **novelty** (the expected churn of
randomized hardware identifiers), with only one or two off-schedule and rarely an
approaching contact. The suppression guardrails are visibly working — devices with
no usable fingerprint are held back, and access points are correctly excluded from
approaching logic. Critically, the old post-freeze failure mode — a morning wave
of thousands of flags driven by re-scoring departed-but-still-listed devices — is
**gone**. This is the first fixed-node baseline we can treat as trustworthy.

## SDR wedge watchdog, proven

The intermittent SDR wedge (readsb exiting with "SDR wedged") recurred a handful
of times across the run — roughly two to four short episodes per day. Every single
one self-healed: the node-local watchdog reset the dongle within its two-minute
window, and ADS-B was never down for more than about two minutes. The orchestrator
never flinched.

The run also narrowed the cause. Across the whole period the board logged **zero**
undervoltage or thermal-throttle events and ran cool, which rules out the power
supply and heat as the trigger. Whatever is wedging the receiver lives in the
SDR/USB link itself, not in the board's power delivery. The rate is flat to
slightly declining, so no action is needed now; if it ever climbs, the remedy is
physical — reseat the dongle or move it to another port, with no extension cable —
rather than electrical.

---

## What this closes, and what it doesn't

- **Closes:** the #191 milestone of a clean, restart-free learn→freeze cycle on a
  fixed node, now additionally validated as *unsmeared* under #232 / #233.
- **Still open:** storage endurance on the SD card at higher device density
  remains the durable-hardware question (#211) — see the separate endurance note.
  This run stayed comfortably inside the safe envelope, but it does not retire the
  constraint.
