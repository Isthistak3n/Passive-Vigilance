# Field Findings — Fixed-Node Testing, June 2026

**Purpose:** capture the empirical results, decisions, and owed validations from
the fixed-node detection-mode work, so the knowledge survives a clean local
state. This is the test evidence behind
[design-and-roadmap.md](../design-and-roadmap.md). Configuration
and hardware facts live in `CLAUDE.md` / `CONTEXT.md` (refreshed separately) and
are cross-referenced here rather than repeated.

---

## Memory soaks — the headline numbers

| Run | Mode | Duration | RSS slope | Shape | Notes |
|---|---|---|---|---|---|
| Prior | mobile (PersistenceEngine) | 6h | ~+12 MB/hr | linear, monotonic | Driven by the unbounded `all_events` list growing with detection events. This is the **wrong engine for a fixed node** and not what runs in production. |
| 2026-06-06 | fixed (FixedScoring) | 4h | +4.84 MB/hr full; +2.85 (last 2h); +1.95 (last 1h) | **decelerating** | Learning-phase only (72h baseline, never froze → WiFi:0 detection events). Archived at `~/soak-archives/20260607-fixed/`. |

**What the fixed soak proved.** The bounded tables flatten: `baseline_profiles`
climbed 130 → ~3,140 in the first hour (loading the device population) then only
+155 over the next 3h — it **plateaus at ~3,300** (the real keyable population).
`probe_evidence` (775→815), `entities` / `device_fingerprint` (3769→3998) are
essentially flat, drifting only with genuine new arrivals. The upserts key
correctly.

**The fingerprint-key fear did NOT materialise.** The concern was that keying
randomized MACs by `fp:<probe_ssids>` could spawn near-unique keys and grow
baseline profiles unbounded like the old in-memory leak, on disk. It didn't —
profile rows track the actual device count and level off.

**What the fixed soak did NOT prove (critical).** It never crossed the baseline
freeze, so post-freeze scoring was never exercised. Post-freeze, `FixedScoring`
emits novelty/off-schedule events every poll for every qualifying device, and the
orchestrator appends each to the unbounded `all_events` list — the mobile leak
re-incarnated, just deferred. **Post-freeze memory is unproven.** This is roadmap
P0 and the reason the multi-day soak is gated behind it.

---

## Soak #2 — first post-freeze read (2026-06-17)

The validation soak finally **crossed the freeze** (48h→63h window, extended after
a power-cycle; baseline froze 2026-06-17T02:00Z at ~2,723 devices — `mac:` 2,671 /
`wifi-fp:` 71 / `ble-fp:` 1). Read ~40 min post-freeze.

**Validated (the wins).**
- **The fingerprint fix held.** Post-freeze *novelty* is ~2 % of flags. The
  ~36/cycle randomized-MAC *novelty* flood the BLE/WiFi fingerprinting targeted
  stayed dead across the freeze — its first real test.
- **Baseline durability survived abuse.** A power outage, multiple restarts, and a
  mid-soak window extension — `learning_start` persisted and the baseline resumed
  every time. The P0 crash-safety property holds under genuinely hostile conditions.

**The finding (the flood moved).** ~50 WiFi `suspicious` flags **per poll**
(~101/min), and **97 % are `off_schedule`**, not novelty — on **held-MAC randomized
clients** (`mac:`-keyed, one randomized address held 16 h+, 1,000+ obs) first seen
*during* learning, each flagging the moment it appears in a single new hour-of-day.
All at score 0.50; 5,766 suspicious vs **2** high. `alerts_dropped` 3,690 (the
backlog bound shedding under the flood — masking, not fixing).

**Root causes.**
1. **Eligibility asymmetry.** A randomized-no-fp device was novelty-ineligible but
   still *off-schedule-eligible* — we flagged "seen in a new hour" on an identity we
   cannot track across its own rotation. Off-schedule on a weak identity is noise by
   construction. **Fix:** make randomized-no-fp off-schedule-ineligible too
   (symmetry with novelty).
2. **No gradation.** One hour outside the mask = full signal = 0.50 = page. Off-
   schedule has no "how far off / how many hours / tolerance" — a cliff.
3. **Paging at the suspicious floor.** Fixed mode paged at 0.50, so the lowest tier
   drowned the operator. **Fix:** page likely+ (≥0.7); suspicious is display-only.
4. **Disrupted learning degrades off-schedule.** Outage/restarts/LE-off left thin,
   biased hour-masks (the `MIN_BASELINE_HOURS=12` gate was not enough). A clean
   uninterrupted learn matters; the detector should tolerate a partial baseline.

**Cross-cutting lessons.**
- Validating one signal doesn't validate the detector — the noise simply moved to
  the next signal. Each signal needs its own post-freeze FP read.
- **P3 (rolling adaptation) is not the bottleneck** — it manages novelty, which is
  already tiny. Lower priority than off-schedule work.
- **Hardware is a first-class reliability factor.** The single-SDR wedge cost ~a day
  of ADS-B; the LE-off bug cost BLE capture; the USB3/power/dongle arrangement
  caused both. Physical fixes rank with code fixes.
- **BLE-as-identity is confirmed weak here** — 1 `ble-fp` of 2,743, a bare-advertiser
  environment exactly as the design predicted (worsened by the LE-off window).
- We had to mine JSONL by hand for the FP rate — **instrument it** (flags/poll by
  signal × severity) so the next soak's read is a glance.

**Actions taken (this PR).** (1) randomized-no-fp made off-schedule-ineligible;
(2) WiFi paging gated to likely+ (`WIFI_ALERT_MIN_SCORE=0.7`, suspicious display-
only, counted in `alerts_below_threshold`). Deferred to validate on soak #3:
off-schedule gradation, FP-rate instrumentation, P3.

**Disk, not RAM, is the real multi-day budget.** `observations` grew ~14 MB/hr
(3,769 → 472,763 rows; entity DB 1.5 → 57 MB in 4h) by design, with **no pruning
yet**. That's ~1 GB/day → ~2–3 GB for a 3-day baseline. chase has ~38 GB free, so
a 72h run fits, but pruning/rotation is needed before this is a standing
deployment.

## Soak #3 — post-fix validation (2026-06-17 → 2026-06-19)

The soak-#2 fixes (PR #138, plus the deploy/SDR/BLE hardening of #131–#141) ran on
the **reused frozen baseline** for ~42.5 h post-freeze with all five sensors up
(WiFi / BLE / ADS-B / DroneRF / GPS) and DroneRF re-enabled. Concluded ~7 h early to
swap in a longer-range BT dongle — the validation goals were already met.

**Validated (the fixes held over a multi-hour run, not a spot check).**
- **The off-schedule flood is gone.** The ~50-per-poll off-schedule flood on
  held-MAC randomized clients — the soak-#2 finding — did not recur. Routine flagging
  is now display-only: ~74,600 display-only detections against **240 paged** over the
  run, **0 dropped**. The likely-only paging gate and off-schedule ineligibility for
  un-trackable randomized devices behaved exactly as designed.
- **Paging is meaningful again.** Every unique device that flagged in-session sat at
  the **suspicious (display-only) tier**, and the signal mix was novelty-dominant —
  i.e. genuinely new *fingerprints* appearing post-freeze, which is legitimate.
  Nothing routine reached the pager.
- **Fingerprinted-device off-schedule is correct behavior.** A randomized device that
  carries a strong fingerprint still flags off-schedule, and that is intended: it can
  be tracked across rotation, so its schedule is meaningful. Only un-trackable
  (no-fingerprint) randomized devices are suppressed.
- **SDR time-share validated.** ADS-B and DroneRF shared the single SDR for the whole
  run with **zero "SDR wedged" events** — the settle-barrier handoff (#131) held over
  many hours.
- **Returning-aircraft detection is live.** ~105 returning-airframe re-acquisitions
  were flagged over the run, as feed/display rather than pager noise.
- **Memory is bounded.** Resident size fluctuated ~116–190 MB (~16 % of 8 GB) with no
  upward drift — the in-memory detection log de-duplicates per device, not per poll.
  Not a leak (an earlier mid-soak reading suggested growth; the fuller picture is a
  bounded fluctuation).

**The standing finding (unchanged): BLE is data-starved here.** Only **one** BLE
fingerprint exists in the baseline, against ~100 WiFi fingerprints — a
bare-advertiser environment worsened by a short-range dongle. An environment/hardware
limit, not a detector fault.

**Action at conclusion — BT dongle upgrade.** Swapped the short-range controller for
a longer-range one and restarted onto it (the scanner auto-detects the live
controller; the durable baseline was reused). BLE capture rose immediately — several
advertisers in the live feed within the first minute, against ~0–1 before. This is
the prerequisite for actually banking BLE fingerprints.

**Proposed next steps.**

*Testing.* (1) A fresh **BLE-focused read** now that capture works — how many distinct
advertisers, how many yield a stable fingerprint, and whether they persist into the
entity store rather than only the live feed. (2) **FP-rate instrumentation** (deferred
from soak #2): flags-per-poll by signal × severity surfaced in status, so the read is
a glance, not a hand-mined log. (3) **Off-schedule gradation** (deferred): how-far-off
/ how-many-hours / tolerance, so a single new hour is not a full-signal cliff — then
re-read the FP rate.

*Features.* (1) **Cross-PHY correlation** — link a BLE advertiser to a WiFi client by
co-presence / timing / signal strength, so a contact survives when one radio rotates;
pairs directly with the new BLE capture. (2) **Disk pruning/rotation** before any
standing multi-day deployment (the observations table grows by design). (3) **P3
rolling adaptation** stays lower priority — it manages novelty, which is already
small; soak #3 reinforces off-schedule and correlation as the higher-value work.

## Detection-signal behavior on real RF

- **Off-schedule false-positive storm at thin baselines.** A 1-distinct-hour
  baseline flagged ~**100%** of known devices the instant the clock crossed into
  any unbaselined hour. Fix shipped: the per-device activation guard
  (`OFF_SCHEDULE_MIN_BASELINE_HOURS`, default 12 distinct hours). Re-validated on
  chase: same thin baseline flags 0% at the default, while a ≥12-hour baseline
  still flags a genuinely off-pattern sighting.
- **Approaching trigger: 0% false positives, but the positive case is still
  owed.** A short fixed-mode check saw 0 of 1,841 eligible devices false-fire at
  the default guards (min 10 baseline + 5 recent samples; rise ≥ max(2·σ, 6 dB)).
  But with no one deliberately approaching, the trigger was never exercised in the
  firing direction. **A controlled operator walk-test is still owed** — "didn't
  false-fire" is not "confirmed it fires." This is roadmap P1.
- **Zero-RSSI is a placeholder.** ~15–18% of Kismet `last_signal` readings are
  exactly `0` — "tracked but no real signal sample", not 0 dBm. Treated like a
  missing reading in baseline RSSI stats; the approaching path (P1) must do the
  same.
- **Graduated severity.** Novelty alone is now a low (`suspicious`) flag, not
  hardcoded high; each additional active signal escalates. In a dense environment
  with a thin baseline, hours=0 produces an alert storm (everything novel) — a
  test-only forcing knob, never a production config.

## Correlator sparsity — what can anchor a randomized device

- **WiFi probe SSIDs are sparse.** Of ~2,634 Wi-Fi clients, only ~26% emit at
  least one *named* probe SSID; the rest broadcast only the wildcard `""` (which
  is excluded). Kismet's own `dot11.device.probe_fingerprint` distinguishes
  devices even when SSID strings are empty and may be the more robust key.
- **Bluetooth is an even weaker correlator here.** Of 207 BT devices (all BTLE),
  only ~7% (15) expose a stable anchor (a name or public OUI), and those are
  stationary appliances (a smart TV, medical devices, IoT, BT
  speakers) — not the mobile phones we'd want to tie to a WiFi probe fingerprint.
  ~206/207 use randomized BLE addresses. Kismet exposes **no** cross-PHY link
  (`kismet.device.base.related_devices` is empty on every device), so WiFi↔BT
  correlation would have to be inferred heuristically and collapses when both
  randomize. **Conclusion: do not wire BT into the fingerprint key in this
  environment** (roadmap P4 leans on WiFi probe fingerprints).

## Kismet field gotchas (confirmed live on 2025.09.0)

- **Leaf-key rule (recurring):** for slash-path "a/b" fields, Kismet returns the
  value under the *leaf* key. `last_signal` → `kismet.common.signal.last_signal`;
  probe list requested as `dot11.device/dot11.device.probed_ssid_map`, read back
  under `dot11.device.probed_ssid_map` (a **list** of records, SSID at
  `dot11.probedssid.ssid`; the `""` entry is the wildcard, excluded);
  `dot11.device.probe_fingerprint` / `dot11.device.num_probed_ssids`. BT device
  data is under `bluetooth.device.*`. Always verify a new path against the live
  daemon before building on it.

## Bluetooth enablement (chase, 2026-06-06)

BT/BLE is captured via a **USB dongle** (`hci0`), not onboard Bluetooth (which
shares the GPS-HAT UART, issue #48). The dongle ships rfkill soft-blocked;
`sudo rfkill unblock bluetooth` (persisted by systemd-rfkill) plus
`source=hci0:name=bluetooth,type=linuxbluetooth` in `/etc/kismet/kismet_site.conf`
makes it durable. Leave `bluetoothd` disabled — Kismet's BT capture talks to the
controller directly. Captured 207 BT devices flowing into `poll_devices()`
alongside WiFi. (Full operational detail in `CLAUDE.md` / `CONTEXT.md`.)

## SDR / DroneRF reliability — isolation exercised in the field (2026-06-19)

The native `librtlsdr`/`libusb` stack segfaults occasionally mid-USB-transfer
(issue #63) — an upstream C bug we can't patch. The fix (#85) **contains** it: the
RTL-SDR sampling runs in an isolated "spawn" child process, so a native crash kills
only the disposable child and the parent respawns it (with backoff) or auto-disables
DroneRF after repeated crashes in a window.

This is no longer just unit-tested — it has been **exercised live and held**. Over one
~10 h session (single shared SDR, time-shared with readsb): 388 worker spawns (normal
per-window cadence; a worker confirmed running as a child of the main process),
**2 contained crash-respawns** (the native stack exited a child unexpectedly; the
parent caught it and recovered), **0 auto-disables**, and — the proof — **0
main-process restarts** (`systemctl … NRestarts: 0`). DroneRF stayed active with live
detections throughout. Pre-#85, each of those 2 native exits would have crash-looped
the whole node. The segfault still happens; it is now a harmless, self-healing event.

## Owed validations (do not claim these as done)

1. **Post-freeze memory** — never exercised (roadmap P0; forced-freeze test).
2. **Approaching positive case** — never fired on a real approach (roadmap P1
   walk-test).
3. **Multi-day endurance across the freeze boundary** — never run (the soak
   stayed in learning).
4. **Egregious-during-baseline** — not built; the node is currently blind during
   the learning window (roadmap P2).

## Process / repo state (as of 2026-06)

- Merged to `main`: Phase 1 (#66), GUI toggle (#67), Phase 2 off-schedule +
  graduated severity (#68), activation guard (#69), probe extraction (#70),
  entity/observation store (#71), docs refresh (#72).
- **Not merged:** Phase 2.5 approaching trigger (+ zero-signal filter +
  AP-exclusion) — branch `feat/fixed-node-phase2.5-approaching`, pending the
  walk-test.
- **Every recent PR shows BLOCKED on the verified-signatures ruleset** (unsigned
  commits). Resolving commit signing is a prerequisite for unblocking merges.
