# Field Findings — Fixed-Node Testing, June 2026

**Purpose:** capture the empirical results, decisions, and owed validations from
the fixed-node detection-mode work, so the knowledge survives a clean local
state. This is the test evidence behind
[roadmap-fixed-node-prototype.md](roadmap-fixed-node-prototype.md). Configuration
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

**Disk, not RAM, is the real multi-day budget.** `observations` grew ~14 MB/hr
(3,769 → 472,763 rows; entity DB 1.5 → 57 MB in 4h) by design, with **no pruning
yet**. That's ~1 GB/day → ~2–3 GB for a 3-day baseline. chase has ~38 GB free, so
a 72h run fits, but pruning/rotation is needed before this is a standing
deployment.

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
