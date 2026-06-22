# Fixed-Node Counter-Surveillance — Design & Roadmap

**Status:** Active, living document (2026-06). The single source for the fixed-node
detector's design plan *and* its phased roadmap. Update phase status as work lands.
**Audience:** Implementers (Claude Code), reviewers, operators.
**Companion (test evidence):** [field-findings-2026-06.md](field-findings-2026-06.md),
[field-test-2026-06.md](field-test-2026-06.md), and the project `setup.md`.

This document consolidates the former `design-detection-modes`, `design-entity-fingerprinting`,
`design-ble-advertisement-capture`, `design-contact-designators`, `design-aircraft-of-interest`,
`roadmap-fixed-node-prototype`, and `soak-plan-2026-06` notes. Code-link conventions:
modules are referenced as `modules/<name>.py`.

---

## North star, and where the bar moved (2026-06-19)

**Original north star:** *a node you can leave at a location for several days that learns
the place's normal RF pattern of life, then reliably surfaces genuine anomalies — a new
device that shows up and stays, a known device behaving off-pattern, something physically
closing in — at a false-positive rate an operator can live with, while staying up and
bounded in memory and disk for the whole run.*

**That bar is cleared.** Soak #3 ran ~42 h post-freeze: learn → freeze → detect end-to-end,
memory bounded (no leak), a livable alert rate (240 paged / ~74.6k display-only / **0
dropped**), all five sensors up, the SDR time-share clean. The multi-day soak was the
validation gate, and the prototype passed it (see Part III).

**So the bar is now deployable trustworthiness + counter-surveillance depth:** a detector
you'd actually trust at an unknown location (it doesn't bake a present threat into "normal,"
and it doesn't fatigue you over many days), plus the identity/air-picture intelligence that
makes its alerts *mean* something. That reframe drives the forward sequencing in Part II.

---

# Part I — Design

## 1. Two threat models: fixed vs. mobile

Passive Vigilance scores observed devices to surface potential surveillance assets. What
counts as "suspicious" depends entirely on whether the sensor is **moving** or **stationary** —
two different questions, not two weightings of one model:

- **Mobile node** (wardrive / on-person / vehicle): *"Does this device follow me across
  locations?"* The sensor moves; a threat shares its movement, seen at many distinct
  locations. **Location diversity is the signal.**
- **Fixed node** (base station / leave-behind): *"What deviates from this location's
  established baseline?"* The sensor is static; the environment has a stable pattern of life.
  A threat departs from baseline — new, off-schedule, lingering, or approaching. **Deviation
  from normal is the signal.**

A threat in one model is invisible in the other. A fixed node run under mobile scoring never
alerts — a stationary sensor produces one location cluster, so every device forfeits the
location-diversity component and nothing crosses threshold (the root cause of issue #50: a
fixed node's GUI is structurally empty under mobile scoring). A mobile node run under fixed
scoring flags every new environment as anomalous. Mode is therefore a first-class deployment
decision that forks the scoring pipeline.

## 2. Mode selection

**Explicit, required, no silent default.** `NODE_MODE` (`.env`) is required: `fixed` or
`mobile`. A wrong default is dangerous both ways (fixed→mobile never alerts; mobile→fixed
flags everything). If unset, the node logs a prominent error and **refuses to enter scoring**
(it may still capture). Resolved in `main.resolve_node_mode()` — `.env` wins, then a `--mode`
CLI flag, else fail-loud.

**Deferred guards (low value for a stationary base station; §Deferred):**
- *GPS-movement sanity check* — cross-check declared mode against observed movement; warn if
  a `fixed` node is moving or a `mobile` node sits long. Auto-detection of mode is rejected
  (a fixed node carried indoors, or a mobile node at a long stop, would mis-infer).
- *Relocation re-baseline* — a `fixed` node that detects a GPS jump to a new stable position
  invalidates its baseline (it describes the old environment) and re-enters learning.

## 3. Architecture: shared pipeline, forked scoring

Mode forks the *scoring strategy*, not the whole system.

**Shared (mode-agnostic):** all capture (Kismet WiFi/BT, GPS, SDR ADS-B/DroneRF, Remote ID),
MAC parsing / OUI lookup / randomization detection, probe-SSID + BLE fingerprinting, alert
backends + rate limiting, the GUI shell / SSE / REST / map / tables, and output writers
(shapefile, KML, GeoJSON, WiGLE).

**Forked (mode-specific):** the scoring engine (`PersistenceEngine` location-diversity vs.
`FixedScoring` baseline-deviation), the baseline lifecycle (fixed only), and the GUI framing.

**Strategy pattern.** `modules/scoring_engine.py` defines the `ScoringEngine` ABC
(`update(devices, *, gps_fix=None)` + `status()`). `main.py` injects the chosen
implementation as `self.persistence`; the capture layer doesn't know which is attached.
`PersistenceEngine` (`modules/persistence.py`) is the existing mobile model, unchanged;
`FixedScoring` (`modules/fixed_scoring.py`) is the fixed model.

## 4. Mobile mode (existing, unchanged)

Documented for contrast; not being redesigned. Weights a device's threat score on **location
diversity** (dominant — seen at many of *my* clusters), temporal persistence, frequency, and
signal. Correct for a moving sensor; structurally unsatisfiable on a fixed one (one cluster).
Key knob: `KISMET_ACTIVE_WINDOW_SECONDS` (set **90–120 on a mobile node** — Kismet's device
list is permanent, so without it the node's own GPS movement stamps every ever-heard device
with new positions and fires spurious "following" clusters; road-test 2026-06-20 saw 4,833
devices flagged / 39k alerts). Leave at `0` on fixed nodes.

## 5. Fixed mode — pattern of life

Two phases: a **baseline learning period** that characterizes the environment's normal RF
pattern of life, then **ongoing deviation scoring** against that baseline, with slow
**adaptation** to keep it current without absorbing patient adversaries.

### 5.1 Baseline learning + durable persistence (implementation-critical)

For `FIXED_BASELINE_HOURS` (default 72; soaks use 48 to freeze inside the run), the node
builds per-device profiles: first/last-seen, observation count, dwell, time-of-day /
day-of-week histogram, RSSI mean+variance (stability, not absolute threshold — see 5.4),
device type / vendor / probe behaviour. At freeze, profiles become the **baseline** — the
description of "normal here." Devices fall into permanent fixtures (routers, neighbour APs),
regular transients (scheduled comings-and-goings), and — post-freeze — novel candidates.

**The baseline persists to SQLite and survives restarts** — `modules/baseline_store.py`
(`BaselineStore`, default `data/baseline.db`, override `BASELINE_DB_PATH`). The
learning-window **start time is persisted on first init and never recomputed on reopen**, so
a crash loop resumes the existing window instead of relearning forever (the single most
important correctness property). This is not hypothetical: during the 2026-06 crash-loop the
service auto-restarted ~60× in 70 min; an in-memory baseline would have re-learned on every
restart and never alerted. `BaselineStore` uses `check_same_thread=False` + an `RLock` so the
Flask GUI thread can read it (otherwise the scoring panel reported "scoring not active").

### 5.2 Egregious-during-baseline (the safety net — implemented, Phase 2.6)

Baseline learning does **not** fully suppress alerting. A sensor deployed into an
already-compromised environment must not bake an existing surveillance device into "normal."
Even during learning, the node still flags **egregious** conditions: a device very close /
very strong on first contact (in the operator's space, not street traffic), or one trending
stronger (reuses the approaching machinery, P1). Soak #1 showed a naive `-45 dBm` floor
floods a dense environment (~22/poll), so the Wi-Fi threshold is **environment-density-tuned**:
`NODE_DENSITY` → dense `-30` / suburban `-40` / rural `-50`, override `EGREGIOUS_SIGNAL_DBM`.
chase runs `dense`. The operator may also run a deliberate clean-environment baseline, but
the system must not *depend* on it.

**BLE has its own threshold** (`EGREGIOUS_BLE_SIGNAL_DBM`, default `-50`, *not* density-keyed):
BLE RSSI runs much lower than Wi-Fi for the same distance and is inherently a ~10 m proximity
signal, so the Wi-Fi presets silence it entirely. On chase the ambient BLE floor clusters
around `-55` while genuinely-close adverts reach `-32..-45`; `-50` separates them. A node whose
operator device advertises at low TX power (iPhones) may need this calibrated against that
device's measured close-range RSSI.

**Paging (the gap that was closed):** a single egregious signal scores `0.5` ("suspicious"),
which is **below** the `WIFI_ALERT_MIN_SCORE` (0.7) display-only bar — so for a window these
events showed in the GUI but never paged, defeating the safety net's purpose. Egregious events
now carry a `force_page` flag (`DetectionEvent.force_page`) that bypasses the suspicious-display
gate so they page anyway; the per-entity rate limiter still bounds them. Post-freeze deviation
events are unaffected (they page only on score, as before).

### 5.3 Deviation scoring (monitoring phase)

After baseline, devices are scored on **departure from baseline**, not absolute properties:

- **Novelty** — not in baseline at all (highest-value signal).
- **Off-schedule** — a known device appearing outside its normal hour/day histogram. Gated by
  `OFF_SCHEDULE_MIN_BASELINE_HOURS` (default 12 distinct hours) to avoid thin-baseline false
  alarms.
- **Abnormal dwell** — lingering materially longer than its baseline norm.
- **Signal trend, not magnitude** — RSSI is noisy (multipath, weather, body-blocking), so
  never threshold absolute dB. A stable mean = stationary; a mean *trending upward* =
  **approaching** (interesting). Use *relative* comparison ("stronger than most seen here").
  **Zero RSSI is a placeholder, not a measurement** (Kismet reports `last_signal == 0` for
  ~15–18% of readings); skip both `None` and `0` in stats and the approaching path.

`FixedScoring` emits the same `DetectionEvent` shape as the mobile path, graduated
`suspicious → likely → high`. WiFi pages **likely+ only** (`WIFI_ALERT_MIN_SCORE`); suspicious
is display-only (the soak-#2 fix). **No location gate** (a fixed sensor has one cluster).

### 5.4 Adaptation (P3, not yet built)

The baseline can't freeze forever — neighbours get new phones, the environment evolves. A
slow rolling update lets a consistently-present device *become* baseline. The core security
tradeoff: **fast adaptation** → fewer false alarms but a *patient adversary* gets absorbed;
**slow adaptation** → catches patient adversaries but more benign-change noise. Exposed as an
operator posture (`FIXED_ADAPTATION=twitchy|balanced|stable`, default `balanced`); a device
must show *consistent* presence over the adaptation window to promote, so an *intermittent*
returner is never silently absorbed.

### 5.5 Threat patterns fixed mode targets

Newcomer-that-stays; repeat visitor (returns across days, correlated with the operator's
presence — "are you being cased"); approaching device; coordinated appearance; and
(long-term, multi-node) a follower seen on the mobile node now appearing at the fixed node.

## 6. Randomization-resistant identity — fingerprinting (P4)

**The problem.** Modern phones/wearables rotate MAC roughly every ~15 min, so "new MAC" is
the *default*. A MAC-keyed baseline is stale within hours; soak #1 fired novelty on ~969
devices/poll (~10.7k alerts), ~60% randomized MACs — baselined devices that simply rotated.

**The insight: the payload outlives the address.** A device rotates its address aggressively,
but its *content* — WiFi management/probe-frame structure, BLE advertisement shape — is far
more stable, and is the documented basis for 802.11 probe-request and BLE/Apple-Continuity
tracking. **Key on the payload, not the MAC.**

**Unified fingerprint (`modules/device_identity.py`) — SHIPPED.** WiFi clients key by
`wifi-fp:` (probed SSIDs + Kismet IE-set hash, `modules/wifi_fingerprint.py`); BLE advertisers
by `ble-fp:` (vendor / services / name, `modules/ble_fingerprint.py`). Same `key/strong/label`
shape. **Over-merge guard:** a device with no distinctive content (bare vendor id / no named
SSID) is **not** grouped, so distinct devices never merge. Both `FixedScoring._device_key` and
`PersistenceEngine` key on it (stable MACs → `mac:<mac>`), so randomization resistance applies
to both modes. On chase this cut the post-freeze randomized-MAC flood from ~36 flags/cycle to
**3–5/cycle**. (Supersedes the earlier `mac_utils.group_by_fingerprint` union-find.)

**What's stable across rotation.** *WiFi:* vendor-specific IEs, supported/extended rate sets,
HT/VHT/HE capability, IE set + ordering, the probe-SSID set, Kismet's `probe_fingerprint`.
*BLE:* manufacturer-specific data (company id + payload), service UUIDs, appearance, name,
TX-power. Caveats: some stacks randomize IE ordering or send minimal broadcast-only probes;
infrastructure APs don't randomize (no special handling needed).

**Cross-modal fusion (future).** A person carries WiFi + BLE devices that beacon *together*;
a co-occurring WiFi-fp + BLE wearable-cluster is a "person" entity more stable than any single
radio, and its re-appearance is a **returning person** — the counter-surveillance payoff.

**Entity store landing.** `modules/entity_store.py` (durable SQLite: `probe_evidence`,
`device_fingerprint`, `entities`, `observations`, plus `contact_designator` and `pnl_evidence`)
records every device via `record_poll()` at the poll site, so it captures for **both** modes
(orthogonal to scoring). Per-device rows are real upserts; only `observations` grows, bounded
by a retention sweep (`ENTITY_OBSERVATION_RETENTION_DAYS` default 30, 0 = forever; swept at
most every `ENTITY_PRUNE_INTERVAL_SECONDS` default 3600). Guarded — a store failure never
affects capture or detection. **Shipped:** within-session identity. **Remaining for P4:** the
*cross-session* pass — link fingerprints into stable entities across days and emit "returning
entity."

**Enrichment round 1 (#146, capture+keying+GUI only — scoring key deliberately unchanged
until validated on real captures):**
- *WiFi accumulated PNL ("former networks").* A device emits a *slice* of its preferred-network
  list per scan; per-MAC `probe_evidence` fragments it across rotation. `pnl_evidence` now
  accumulates the PNL under the **rotation-stable IE hash**, so the full list ("Home", "Work",
  a cafe) accrues into one identity; `compute_pnl_fingerprint` anchors a parallel stable key.
- *BLE reconnect signals.* The parser now keeps what it discarded: directed adverts
  (`ADV_DIRECT_IND` = reconnect to a bonded peer), solicited service UUIDs, 128-bit custom
  UUIDs, and a volatile-masked manufacturer-data type prefix (e.g. Apple message type) —
  folded into `ble-fp`, turning many bare-vendor phones *strong*, the over-merge guard intact.
  Reconnect *intent* is an evidence/label flag (it flaps), not part of the identity hash;
  reconnect *targets* are resolvable-private-address-masked (we hold no bond keys). New GUI
  columns: *Known Networks* + *Reconnect*.

**Honest limits.** A sophisticated adversary can strip/spoof distinctive IEs or advert fields
— this raises the bar, it doesn't make a deliberately untraceable device visible. Minimal-probe
clients and privacy-hardened stacks fingerprint weakly. BLE's stable subset here skews to fixed
appliances, so WiFi carries most identity; BLE corroborates and adds proximity (~10 m range).

## 7. BLE advertisement capture — how (SHIPPED, 2026-06-14)

The fingerprint in §6 needs the raw BLE advertisement payload, which Kismet's `linuxbluetooth`
summary feed does not provide (empty service/vendor/TX fields, signal `0`).

**Capture primitive: raw, listen-only HCI** (`modules/ble_scanner.py`, behind
`BLE_SCANNER_ENABLED`). It reads LE Advertising Reports directly from the controller (passive
scan — the radio never transmits / no `SCAN_REQ`), recovering vendor data, service UUIDs,
service data, name, **and a real per-advert RSSI** (validated `-58 dBm` live; Kismet reported a
flat `0`). Passive capture cannot see scan-response-only fields — an accepted limit of the
no-transmit charter. **Key validation finding:** BlueZ's high-level offloaded
advertisement-monitor path returned *nothing* on this controller; **raw HCI is the production
primitive** (proof: `scripts/ble_capture_spike.py`).

**Path chosen:** repurpose the existing dongle (recommended option A — costs nothing, replaces
a near-empty feed). PV owns `hci0`, so Kismet's `linuxbluetooth` source is removed and
`bluetoothd` stays off. Deploy specifics that emerged: the service unit grants
`AmbientCapabilities=CAP_NET_RAW CAP_NET_ADMIN` (**no `CapabilityBoundingSet`** — it strips
the setuid caps `sudo` needs and broke the SDR coordinator); the HCI index is auto-detected
(`BLE_HCI_DEVICE`) so a USB re-enumeration to `hci1` still works. *Upgrade path if controller
capture proves too lossy:* a dedicated sniffer (Ubertooth / nRF) Kismet can ingest directly.
On chase, bare advertisers dominate (mostly `mac:`-keyed, few `ble-fp:`), as predicted — a
longer-range dongle was swapped in at soak #3's conclusion to improve capture.

## 8. Contact designators (SHIPPED, 2026-06-14)

The WiFi/BT panel listed devices by raw MAC/fingerprint hash — unreadable and impossible to
*refer to*. This tool is a contact tracker, so devices get naval/air-style **track
designators** (`modules/contact_designator.py`):

```
CLASS-IDENT-#
```

- **CLASS** — from the Kismet device type: AP / CLI (client) / BR (bridged) / BLE / DEV. This
  encodes the device type, so the redundant GUI **Device column was removed**.
- **IDENT** — most-identifying name in priority order: (1) network name (AP SSID or a client's
  probed-SSID `fingerprint_label`, e.g. `NETGEAR13_5G`), (2) vendor (Apple, Samsung …), (3) a
  short hex token of the stable fingerprint. Squeezed to one length-capped token.
- **#** — a **persisted, sequential** instance number disambiguating same-`CLASS-IDENT`
  devices.

Examples: `AP-NETGEAR13_5G-1`, `CLI-NETGEAR13_5G-1`, `BLE-Apple-3`, `CLI-7a3f-1`.

**Stability is the core property:** the designator binds to the device's rotation-stable
identity key (`wifi-fp:` / `ble-fp:` / `mac:`), and the number is persisted in the entity store
(`entity_store.assign_contact_number(identity_key, group_key)` → existing number, else
`max+1`), so it **survives MAC rotation, restart, and sessions**. Assigned only from the
asyncio poll thread (the GUI rides the finished label on the event), so no cross-thread lock;
if the store is absent it falls back to a short stable hash. Surfaced as the GUI **Contact**
column; the raw **SSID** column stays (complements, doesn't duplicate).

## 9. Aircraft of interest — persistence scoring for the air picture (P7)

**The problem.** Aircraft/drones were display-and-enrichment only, with one crude exception:
the alert path fired on *every* detection, so a transiting airliner paged like a threat — the
alert-fatigue trap. The sharper question: *is something watching from above, and has it watched
before?*

**The one distinction that matters: transit vs. orbit/return.** Almost everything overhead is
**transit** (approach/departure, inter-island hops, coastline tours) — benign, the common
case. The signal is an aircraft **orbiting/loitering in the immediate area** or **one that
keeps coming back**. Flag the *behaviour*; a known-benign daily orbiter is suppressed by the
**baseline / operator whitelist**, never by code guessing "probably training."

**Why a persistence score, not thresholds.** The mobile node already scores this shape — *is
this persistently with me?* — with a windowed weighted model. The air picture is the same
question with the node held still, so we **reuse the model and swap the features**. Continuous
and tunable: a plane just outside one threshold still contributes instead of vanishing off a
cliff. The thresholds become **normalizers**, not gates.

**Feature mapping (mobile → air):** temporal persistence → **dwell**; location diversity →
**proximity + orbit geometry** (*inverts* — node is fixed; do **not** copy mobile's 35%
location weight); frequency → **return frequency** (distinct visits/days, ICAO-keyed); signal
proximity → **closeness** (3-D slant range + altitude).

**Two timescales** (the one genuinely new piece — mobile is short-horizon only): *short*
(minutes, live per-ICAO track) → "loitering right now?"; *long* (hours/days, durable ICAO-keyed
store) → "has it come back, how often?"

**Features / first-cut params:**

| Feature | Normaliser (default) | Why |
|---|---|---|
| Closeness | slant range vs `AIR_RADIUS_NM=5`; altitude vs `AIR_CEILING_FT=5000` | immediate area; 3-D so a high overflight is far |
| Dwell | in-range span vs `AIR_DWELL_TARGET_S=480` (8 min) | a racetrack, not one pass |
| Orbit | cumulative heading change vs `AIR_HEADING_TARGET_DEG=270`; low groundspeed boosts | near-full circle ≠ fly-by |
| Return | distinct visits/days vs `AIR_RETURN_TARGET=3` | the "cased me before" signal |

Weights (location-diversity removed, reweighted): dwell .30 / orbit .30 / closeness .20 /
return .20, all `AIR_WEIGHT_*`-tunable. **Interest multiplier** (×, capped ~1.3) for
blocked/anonymous callsign (LADD/PIA), military flag, no callsign, rotorcraft, and (UAS)
low-and-slow. Tiers: suspicious ≥ 0.5, likely ≥ 0.7, high ≥ 0.9. A **min-observation guard**
(≥2 in-range sightings) is the "don't alert on a burn-by" rule, free. **Reference position:**
live GPS fix (median-smoothed — a fixed node's fix jitters), with a persisted GUI-pinned home
override that wins when set.

**Baseline pattern-of-life — the flood guard.** A bare score floods (tour helicopters, flight
schools, medevac). Same baseline-then-flag discipline as fixed WiFi: learn which ICAOs loiter/
recur and at what hours in a durable ICAO-keyed store; a **novel** high-scoring airframe is the
signal, a baselined daily orbiter is absorbed; an **egregious-during-learning** carve-out fires
for the unmistakable live case (very close, very low, sustained orbit) before the baseline
exists.

**Modality-agnostic.** A **loitering small UAS** (Remote ID) is the highest-value contact — a
drone holding station over a property is a more direct surveillance indicator than any crewed
aircraft. The scorer is fed by ADS-B, Remote ID, and (coarsely) drone-RF persistence against
the same reference. Remote ID carries operator/drone positions directly; RF has no position, so
it contributes only dwell/return, never closeness/orbit.

**Architecture.** *Not* on the `NODE_MODE` device-scoring fork — a **separate scorer instance**
fed at `_poll_adsb` / `_poll_remote_id` / `_poll_drone_rf`, driving the alert decision (through
the existing rate limiter) and GUI severity. Pure cores: `modules/air_geometry.py` (reference
resolution, 3-D slant range, bearing, gap-tolerant cumulative heading change, groundspeed) and
`modules/air_scoring.py` (features + weighted score + tiers + min-obs guard; no I/O,
unit-tested against synthetic tracks). Durable per-ICAO baseline + return history extends
`BaselineStore` with an ICAO-keyed air table.

**Honest limits.** Gappy reception (the receiver catches ~one target at a time with blind
spots) — dwell/heading must accumulate **across gaps within a window**, never require a
continuous track; loiters in a blind sector are a siting limit, not a software one. The node
reports geometry and history, not intent: "novel airframe orbited your location at 02:00, and
again the next night" is the strongest honest statement — and a useful one.

## 10. Deferred design — WiGLE enrichment & watchboxes

**WiGLE resident-vs-visitor enrichment.** The self-learned baseline decides "normal here" from
the node's own observations; WiGLE adds an external prior — *does this device live here or is it
visiting?* A **resident** (WiGLE shows the BSSID historically here) can be baselined
immediately; a **visitor** that lingers is precisely the fixed-node threat, and WiGLE
corroboration raises its novelty from first contact. Rules:
- **Augments, never gates.** WiGLE is incomplete (absent ≠ new; present ≠ still here). If
  unreachable/rate-limited/empty, the self-learned baseline and all core detection continue
  fully. Same rule as the alert backends: an external service is never a decision authority.
- **Connectivity-adaptive, runtime-detected** on the *internet path* (`wlan0`/ethernet — **not**
  `wlan1`, the monitor adapter connected to nothing). Reachable → live enrichment; not reachable
  → log raw + flag the session pending-enrichment, enriched later from a connected machine
  (WiGLE's native collect-then-process workflow; the *more thorough*, paced mode).
- **Rate-limited, cached, selective** — enrich novelty/persistent candidates, never every
  transient probe; never re-query a cached BSSID.
- **Shared WiGLE-operation queue.** Enrichment *queries* and detection *uploads* are both
  "do a WiGLE op when online, queue it when not" — one connectivity-gated queue. **Net-new
  work:** today's `modules/wigle.py` (`WiGLEUploader`) is a fire-once CSV upload with no queue
  and no query; this phase **refactors it onto the shared queue** and writes a **net-new query
  client**.
- **Opsec toggle.** Queries are outbound — they reveal what's being investigated and from where.
  A config toggle disables outbound enrichment entirely for sensitive deployments.

**Watchboxes / origin-geofencing.** Building on WiGLE per-device geography: flag a device on
*where it has historically lived*, not just its local behaviour. A device whose WiGLE history
clusters around a known-threat origin, or falls inside an operator-defined **watchbox**, raises
its score — catching a device that tailed the operator from a known source even if never seen
at this node. Strictly depends on WiGLE enrichment; inherits all of its rules.

## 11. SDR decode cycle — AIS / ADS-B / ACARS (replaces DroneRF)

DroneRF is **retired** (default off, low-value power-threshold scans, antenna-limited, the #63
libusb SIGSEGV). In its place, the single RTL-SDR runs an ordered **decode cycle**: AIS (marine
VHF ~162 MHz, optional), ADS-B (1090 MHz, the bulk), and ACARS (aviation VHF ~131 MHz). Generalizes
`SDRCoordinator` from a fixed readsb↔DroneRF alternation into an N-band cycle (`SDR_CYCLE_SLICES`)
of band *owners* (`acquire/release/is_available`), reusing the lock + settle barrier + sudoers-scoped
`systemctl` handshakes. AIS/ACARS run as external decoder systemd services (AIS-catcher / acarsdec),
not subprocesses — one handoff machine, one crash-isolation story.

**Hard realities (designed around):** ACARS is *decode*, not "decrypt" (it's plaintext); you can't
aim the radio at one aircraft (decode the shared channel, correlate by tail/flight-id); and AIS/ACARS
won't receive on the 1090 antenna — both are **best-effort, default-off** until VHF hardware exists.
On a single dongle, a VHF window blinds ADS-B; a 2nd VHF dongle (DEDICATED) removes the blackout and
the cycle is bypassed (each band continuous).

**Phase 1 (shipped):** retire DroneRF; generalize the coordinator to the N-band cycle; add the
optional AIS band (`modules/ais.py`, AIS-catcher JSON over UDP, deduped per MMSI) + GUI/deploy/sudoers.
**Phase 2 (implemented — on `feat/sdr-acars-correlation`, pending on-Pi validation):** ACARS decoder
(`modules/acars.py`, acarsdec/dumpvdl2 JSON) + the **>30s-held trigger** (a contact in view past
`ACARS_TRIGGER_SECONDS` requests a bounded `request_band_window("acars", …)` preemption) +
**connectivity-adaptive correlation** (`modules/aircraft_registry.py`) — adsb.lol enrichment when an
API key is set, else an **offline ICAO→registration SQLite built off-node** (`scripts/build_aircraft_registry.py`
from a public CSV; opsec-safe, zero node-side queries) — matching ACARS **tail↔registration /
flight-id↔callsign** back to the live ADS-B contact (`event["acars"]`). Augments-never-gates; offline
fully functional (correlation falls back to callsign with no registry at all).
*Note:* the offline DB is built from a public CSV, not tar1090's bundled `db-*` shards — those are
zlib-compressed binary (a fragile, version-specific trie), so a clean off-node SQLite is the robust source.

---

# Part II — Roadmap

## Where we are (2026-06)

Merged on `main`: the `NODE_MODE` fork + `ScoringEngine` strategy; `FixedScoring` (novelty +
off-schedule + graduated severity + per-device activation guard + egregious-during-baseline,
Phase 2.6 — now paging via `force_page`, §5.2); the crash-safe
`BaselineStore`; the GUI mode toggle; probe-SSID/fingerprint capture; the entity/observation
store (both modes); randomization-resistant fingerprint capture + keying for WiFi *and* BLE
(§6–7); contact designators (§8); the full operator GUI (durable history across all panels,
live-mirror); the air-picture GUI + Remote ID surface; and the P7-core air persistence scorer
with alerting reframed to *of-interest only*. Built but **not merged:** the approaching-signal
(rising-RSSI) trigger (Phase 2.5 / P1) — owes a positive walk-test.

## What drives the sequencing now

The endurance question that drove the *original* sequencing — an unproven post-freeze memory
leak — is **settled** (soak #3, ~42 h, memory bounded). The new driver is the gap between
*"works in a soak"* and *"trustworthy at an unknown location."* Two things gate cold
deployability and set the lead priority (**detection-quality completion**):

1. **Blind during learning (P2) — closed in code, owes field calibration.** A 48–72 h baseline
   that flags nothing while learning would bake an already-present surveillance device into
   "normal." The egregious-during-baseline net (§5.2) is now implemented *and paging*; what
   remains is the on-chase walk-test to calibrate the thresholds (Wi-Fi + BLE) so it pages a
   genuinely-close device without flooding.
2. **Fatigues over days (P3) — implemented, owes the multi-day read.** Post-freeze, every benign
   newcomer would otherwise read novel forever. Soak #1's novelty flood and soak #2's off-schedule
   flood are fixed, and the *durable* answer — a rolling baseline that promotes consistently-present
   devices without absorbing an intermittent/patient adversary — is now built and activated on
   chase (`conservative`). What remains is watching it across a multi-day post-freeze run.

Identity and air-picture depth follow detection-quality — they make alerts *mean more*, but a
detector you can't trust during learning isn't deployable regardless.

## Forward roadmap (priority order)

1. **Detection-quality completion (lead).** P2 egregious-during-baseline (implemented; owes the
   calibration walk-test) + P3 rolling adaptation (implemented & activated; owes the multi-day
   read) + the owed P1 approaching walk-test. Closes "blind while learning" and "fatigues over
   days" → run a **short confirmation soak** read for during-learning and multi-day FP
   behaviour.
2. **Fingerprinting program (round 2+).** Validate the shipped PNL/reconnect enrichment on real
   captures, then wire it into scoring; then cross-PHY WiFi↔BT linking (one device → one
   contact).
3. **Cross-session returning-entity (P4 remainder).** Link fingerprints into stable entities
   across days/sessions and emit "returning entity" — the original counter-surveillance payoff.
4. **AP evil-twin detection.** Fingerprint AP beacons; flag a known SSID appearing with a new
   IE set/BSSID, or an AP beaconing what nearby devices probe for (karma). Plus the **P7
   remainder** (durable cross-day air baseline + daily-orbiter novelty suppression, GUI severity
   badge, Remote ID loiter fusion) as follow-on.

The forward chain is detection-quality first because trustworthiness-during-learning gates
deployability; the depth that follows raises what alerts *mean*, not whether you can rely on
the detector.

## Phase status

①  = detection-quality lead; ②–④ = the identity/air-picture program after it.

| Phase | Goal | Status | Forward priority |
|---|---|---|---|
| **P0** | Endurance hardening (post-freeze memory + disk) | ✅ Merged #74; forced-freeze + soak #3 (~42 h) validated bounded memory | ✅ shipped |
| **P1** | Approaching trigger merged + walk-tested (Phase 2.5) | ◑ Merged & green; owes the positive walk-test | **① (owes walk-test)** |
| **P2** | Egregious-during-baseline safety net (§5.2) | ◑ Implemented (Phase 2.6) — density-tuned Wi-Fi + modality-specific BLE threshold, now paging via `force_page` (the 0.5-score events were display-only before the fix). *Owes:* the on-chase calibration walk-test (does a deliberately-close device page without flooding) | **① lead (owes walk-test)** |
| **P3** | Adaptation — rolling baseline (§5.4) | ◑ Implemented & wired — `promotion_policy.py` (swappable criterion, slow-in/fast-out invariant), `BaselineStore` promote/demote + post-freeze accumulator, `FixedScoring.run_adaptation_sweep`, the guarded `_adaptation_sweep_loop` task, 30 passing tests. Defaults `off`; **activated on chase** (`ADAPTATION_POSTURE=conservative`). *Owes:* multi-day post-freeze validation that FP decays without absorbing an intermittent returner | **① lead (owes validation)** |
| **P4** | Cross-session entity resolution | ◑ Within-session fingerprint capture+keying merged & live (flood ~36→3–5/cycle); round-1 PNL/reconnect enrichment merged (capture+GUI only). Cross-session *returning-entity* linkage + scoring integration remain | ②/③ |
| **P5** | Fixed-mode GUI framing + durable history | ✅ Contact designators, scoring-panel thread-safety, baseline-state header, sortable/filterable + CSV, durable history across ALL panels, live-mirror (re-seed + resync, #149). *Owed:* learning-vs-frozen framing + anomaly-by-severity list | ✅ shipped (slice owed) |
| **P6** | Air-picture GUI: aircraft panel fix + decay + Remote ID surface | ✅ Complete — current-sky panel, decay, chiclet accuracy, bounded tracks, ID-less split, Remote ID pruning + surface; 24 h retention; returning-ICAO as same identity | ✅ shipped |
| **P7** | Aircraft of interest: orbit/loiter detection (§9) | ◑ Mostly shipped — geometry + scorer (`air_geometry.py`/`air_scoring.py`), live scoring in `_poll_adsb`, alerting reframed to of-interest only (soak #3 confirmed). *Deferred:* durable cross-day per-ICAO baseline + daily-orbiter suppression; GUI severity badge; Remote ID loiter fusion | follow-on |

### Phase detail (the open work)

**P1 — Approaching trigger (Phase 2.5).** Coded & green, owes proof. Merge; perform the
operator **walk-test** (a device moved closer must trip it); confirm zero-RSSI placeholders are
skipped in the approaching path as in the baseline stats; tune margin only if the walk-test
mis-fires. *Exit:* a real approach fires; ambient stationary FP acceptable.

**P2 — Egregious-during-baseline (§5.2).** *Implemented (Phase 2.6).* During learning the node
emits alerts for egregious triggers (very close/strong on first contact, or trending stronger),
reusing P1's approaching machinery, a `NODE_DENSITY`-tuned Wi-Fi floor, and a separate BLE
threshold (`EGREGIOUS_BLE_SIGNAL_DBM`). The events now **page** via `force_page` (they score
0.5 and were silenced by the suspicious-display gate until the fix). Unit tests cover the
threshold routing and the paging bypass. *Owed:* the on-chase calibration walk-test — confirm
a deliberately-close phone (Wi-Fi *and* BLE) pages without flooding on ordinary traffic, and
tune `EGREGIOUS_BLE_SIGNAL_DBM` to the device if its BLE TX power is low. *Exit:* egregious
flags fire (and page) during learning, sparingly.

**P3 — Adaptation: rolling baseline (§5.4).** *Implemented & wired.* `promotion_policy.py` is a
swappable promotion criterion (`SustainedPresencePolicy` ships; `ConsistencyPatternPolicy` is
the designed-for stronger one behind the same seam) selecting **parameters** by posture
(`conservative` / `permissive`, default `off`), never mechanism. Demotion is a fixed mechanism,
not a policy — the **slow-in / fast-out** asymmetry (demote faster than you can promote) is a
design invariant, validated at construction so a misconfigured posture fails loud. The
post-freeze presence accumulator (distinct days + first→last span + distinct hours, kept
separate from the frozen baseline stats) feeds `FixedScoring.run_adaptation_sweep`, called
hourly by the guarded `_adaptation_sweep_loop`; demotions emit a `baseline_demotion` event to
`events.jsonl`. A promoted device is no longer novelty-eligible (§5.3) until demoted. Unit tests
cover promotion, the intermittent-returner-not-absorbed case, demotion, off-posture, the policy
seam, and the migration (30 passing). **Activated on chase** (`ADAPTATION_POSTURE=conservative`),
inert until the baseline freezes. *Owes:* the multi-day post-freeze read that novelty FP decays
without swallowing an intermittent returner. *Exit:* FP decays without absorbing intermittent
returners. *Deferred:* swapping in `ConsistencyPatternPolicy`; a "graveyard" GUI panel for
demotions (the producer ships; the consumer is later).

**P4 — Cross-session entity resolution.** Within-session identity is live (§6). Remaining: a
resolution pass over the entity store merging fingerprints/probe evidence into stable entities
across sessions, surfacing "returning entity." *Tests:* two MAC-rotated sightings sharing a
fingerprint resolve to one entity; distinct devices don't merge; a known device re-identifies
across a restart and a day boundary. *Exit:* cross-session re-identification works on known
devices.

**P5 owed slice.** The learning-vs-frozen baseline framing (time remaining) and the anomaly
list framed by signal/severity. (Durable history + live-mirror are done.)

**P7 remainder.** Durable per-ICAO air baseline (daily orbiters → normal, novel loiterer →
signal) with the egregious-during-learning carve-out and interest weighting; GUI severity
badge; Remote ID loiter fusion. *Build order:* geometry+scoring core (done) → reference + live
classifier (done, display-only) → baseline + scoring + alerting → Remote ID fusion.

## Deliberately deferred

GPS-movement sanity check (§2) and relocation re-baseline (§2) — low value for a stationary
base station; WiGLE resident-vs-visitor enrichment and watchboxes/origin-geofencing (§10);
multi-node correlation (the follower-resolves-to-fixed pattern, §5.5). All genuinely later.

## Standing risks

- **Post-freeze memory leak — RETIRED.** Soak #3 ran ~42 h post-freeze, RSS bounded
  (~116–200 MB, no drift); `all_events` dedups per device.
- **Alert fatigue — largely addressed.** Soak #1 novelty flood and soak #2 off-schedule flood
  both fixed (fingerprint keying; randomized-no-fp made off-schedule-ineligible; page likely+
  only). Soak #3 livable (240 paged / ~42 h / 0 dropped). **P3 remains the durable fix** for
  benign newcomers going stale-novel over many days.
- **Fingerprint over-merge** (with the #146 enrichment): content fingerprints aren't unique per
  device, so widening coverage risks fusing distinct devices. Enriched identities stay
  capture/display-only until validated; the over-merge guard (bare vendor / no-named-SSID stays
  ungroupable) is preserved.
- **BLE-as-identity is environment-limited** here (mostly bare advertisers); the longer-range
  dongle improves capture, but cross-PHY BLE↔WiFi linking is unbuilt.

---

# Part III — Validation: the multi-day soak

**The gate has been run and cleared** (soaks #1–#3). The method that mattered: set
`FIXED_BASELINE_HOURS` so the baseline **freezes inside the soak**, actually exercising
post-freeze scoring — the thing the original 4-hour run never reached. Soak #3 (~42 h, frozen)
demonstrated learn-then-detect end-to-end with bounded memory, disk in budget, a livable
post-freeze FP rate (after the #138 fix), and stability across days with the SDR time-shared.
Remaining soak use is a **shorter confirmation run after P2+P3** — a regression check on
during-learning and multi-day FP behaviour, not a gate.

## Soak history

**Soak #1 — chase, 2026-06-07→09 (P0+P1+P2, 48 h, DroneRF off).** *Machinery works; FP rate
did not.* Passed: clean freeze at +48 h; rich banking (5,026 profiles; 1,872 with ≥10 RSSI →
approaching-eligible; 4,227 with ≥12 baseline hours → off-schedule-eligible); 0 restarts; RSS
modest (99→155 MB). Failed — two floods: (1) **egregious flooded** at the `-45 dBm` default
(~22/poll in dense RF) → fix: `NODE_DENSITY`-tuned threshold (§5.2); (2) **novelty flooded**
(~969/poll, ~10.7k alerts), ~60% randomized MACs → fix: a randomized MAC with no fingerprint
must show sustained presence (`NOVELTY_RANDOM_MIN_OBSERVATIONS`) before novelty fires.

**Soak #2 — chase, 2026-06-17 (first post-freeze read).** Novelty flood stayed dead (~2% of
flags) ✓, but the noise **moved to `off_schedule`**: ~50 WiFi suspicious/poll, 97% on held-MAC
randomized (`mac:`) clients flagging on one new hour-of-day, all at 0.50; `alerts_dropped`
shedding. Root cause: a randomized-no-fp device was novelty-ineligible but still
off-schedule-eligible. Fixed (#138): off-schedule-ineligible for randomized-no-fp + page
likely+ only.

**Soak #3 — chase, 2026-06-17→19 (~42 h, post-fix validation).** The #138 fix held: **240
paged / ~74.6k display-only / 0 dropped**, SDR time-share clean (**0 wedges** with ADS-B +
DroneRF sharing the SDR), returning-aircraft live, **memory bounded** (~116–200 MB, no drift —
the post-freeze `all_events` dedups per device, retiring the P0 leak worry). BLE data-starved
(1 `ble-fp:`) — concluded early to swap in the longer-range dongle.

Full test evidence: [field-findings-2026-06.md](field-findings-2026-06.md).

## Method for a future confirmation soak

**Run the candidate stack on chase** (e.g. `feat/egregious-during-baseline` carrying P0+P1+P2)
— running unmerged validation code on the test node is acceptable for a prototype validation
run; PRs still merge only after the walk-test passes. Wipe the baseline so a fresh window
starts under the stack, then learn → freeze → observe.

| Knob | Soak value | Why |
|---|---|---|
| `NODE_MODE` | `fixed` | Required; absence crash-loops the service |
| `FIXED_BASELINE_HOURS` | `48` | Freeze lands *inside* the soak; clears the 12-distinct-hour off-schedule guard (real deployment uses 72) |
| `BASELINE_DB_PATH` | fresh / wiped | A clean window banking RSSI from minute one |
| `EGREGIOUS_SIGNAL_DBM` / `NODE_DENSITY` | density default | P2 knob; tune if the deliberately-close test floods or misses |
| `APPROACHING_*` | defaults | P1 knobs; tune only if the walk-test mis-fires |
| SDR / DroneRF | as deployed | A disabled SDR path is the cleanest first endurance read |

**Timeline (~4 days at 48 h baseline):** *0–48 h learning* — watch P2 (a deliberately-close
device should flag, ordinary traffic should not; calibrate `EGREGIOUS_SIGNAL_DBM`; confirm
RSSI + hour-of-day are banking). *~48 h freeze* — verify `is_learning` false, a healthy
fraction of profiles carry non-null `signal_mean`/`signal_var` and ≥12 distinct hours.
*48–96 h post-freeze* — run the P1 **walk-test** (carry a known device toward the node; the
approaching signal must trip while stationary devices stay quiet), then observe.

**Measure / pass bar:** RSS flat across the freeze boundary and over days; `observations` row
count / disk bounded by the retention sweep; approaching fires on a real walk-toward with low
ambient FP; egregious flags during learning without flooding; a sane post-freeze anomaly stream
with tolerable FP; uptime + counters advancing over days.

**Safety / isolation.** This soak **is** the live run on chase, so the usual "operate on copies"
rule is inverted — it is the production-shaped exercise. Guard rails: confirm `NODE_MODE=fixed`
before restart (or the service crash-loops); keep the retired baseline backup; **read counter
advancement, not the health-banner ✓ flags, to judge liveness** — the node has silently stalled
green before.
