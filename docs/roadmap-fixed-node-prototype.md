# Roadmap: Fixed-Node Counter-Surveillance Prototype

**Status:** Active roadmap (2026-06). Living document — update the phase status as
work lands.
**Audience:** Implementers, reviewers, operators.
**Companion:** [field-findings-2026-06.md](field-findings-2026-06.md) (the test
evidence behind these decisions) and
[design-detection-modes.md](design-detection-modes.md) (the full design).

---

## North star — and where the bar moved (2026-06-19)

The original north star: *a node you can leave at a location for several days that
learns the place's normal RF pattern of life, then reliably surfaces genuine
anomalies — a new device that shows up and stays, a known device behaving
off-pattern, something physically closing in — at a false-positive rate an operator
can live with, while staying up and bounded in memory and disk for the whole run.*

**That bar has been cleared.** Soak #3 ran ~42 h post-freeze on the node: learn →
freeze → detect end-to-end, memory bounded (no leak), a livable alert rate (240 paged
/ ~74.6k display-only / **0 dropped**), all five sensors up, the SDR time-share clean.
The multi-day soak was the validation gate, and the prototype passed it.

**So this document pivots.** It is no longer a road *to* a working prototype; the
prototype works. The bar is now **deployable trustworthiness + counter-surveillance
depth**: a detector you'd actually trust at an unknown location (it doesn't bake a
present threat into "normal," and it doesn't fatigue you over many days), and the
identity/air-picture intelligence that makes its alerts *mean* something. The
remaining work, re-sequenced for that bar, is in **Forward roadmap** below; the
shipped phases are summarised under **Validated & shipped**.

## Where we are (2026-06)

Merged on `main`: the `NODE_MODE` fork and `ScoringEngine` strategy, `FixedScoring`
(novelty + off-schedule + graduated severity + activation guard), the crash-safe
`BaselineStore`, the GUI mode toggle, probe-SSID/fingerprint capture, and the
entity/observation store recorded at the poll site for both modes. Bluetooth is
capturing via the USB dongle, and its boot/hot-plug durability is now wired in
(the controller is raised before Kismet reads its sources). Built but **not
merged**: the approaching-signal trigger (Phase 2.5) — it still owes a positive
walk-test.

**Update (2026-06-07).** P0 is **done and merged** (#74), with its forced-freeze
validation run on chase against copies of the live databases. The P1
approaching-signal branch is rebased onto the post-P0 `main` and green, awaiting
only the walk-test. The node was found mis-configured (no `NODE_MODE`, which
crash-looped the service) and silently stalled; it has been restarted clean on
`main` with a wiped baseline, so a fresh 72h learning window is banking RSSI and
hour-of-day data and **freezes 2026-06-10 21:42 UTC** — the earliest the P1
walk-test can run.

**Update (2026-06-14) — randomization-resistant fingerprinting deployed (P4 core).**
The big one: a device is now identified by *what it broadcasts*, not its rotating
address, for **both** radios. Merged and live on chase:
- **Passive BLE advertisement capture** (`modules/ble_scanner.py`) — owns the BT
  dongle via a raw HCI socket and listens (no transmitting), recovering vendor
  data, service UUIDs, and a **real RSSI** that Kismet's BT feed never provided.
  Validated finding: this controller doesn't support BlueZ's offloaded advert
  monitoring, so raw HCI is the production primitive (see
  [design-ble-advertisement-capture.md](design-ble-advertisement-capture.md)).
- **Unified fingerprint signatures** — `modules/ble_fingerprint.py` (vendor /
  services / name) and `modules/wifi_fingerprint.py` (probed SSIDs + Kismet IE
  hash), same `key/strong/label` shape, with the over-merge safeguard that a bare
  vendor id / no-named-SSID stays ungroupable.
- **Fingerprint-keyed scoring** — `FixedScoring._device_key` keys randomized
  devices by `wifi-fp:` / `ble-fp:` so novelty/off-schedule track across rotation;
  BLE was previously un-fingerprintable and novelty-suppressed entirely.
- **GUI identity collapse** — rotating addresses fold into one labeled row (a slice
  of P5).
- **Live result:** on chase the new keying cut the post-freeze randomized-MAC flood
  from ~36 devices/cycle (the old probe-SSID scheme) to **3–5/cycle** — the durable
  fix the soak called for. The cutover also surfaced and fixed two real deploy bugs
  (a `CapabilityBoundingSet` that broke `sudo`, and a hardcoded `hci0`).
- **Remaining for P4:** the cross-session *returning-entity* linkage (same device
  across days) — the within-session fingerprint identity is now in place to build
  it on. BLE-as-identity is still environment-limited here (most advertisers are
  bare), as the design predicted.

**Update (2026-06-14) — operator-facing GUI work (P5/P6 slices).** A batch of
GUI/air-picture work merged and is live on chase:
- **Contact designators (P5)** — WiFi/BT devices now carry naval/air-style track
  labels, `CLASS-IDENT-#` (e.g. a phone's probed SSID → `PHONE-LINKSYS-3`), with the
  sequential number persisted in the entity store against the rotation-stable
  fingerprint so it survives MAC rotation and restart (`modules/contact_designator.py`,
  `entity_store.assign_contact_number`). This **replaces the redundant Device column**
  in the GUI — the designator already encodes the class.
- **Scoring-panel thread-safety** — the GUI read `BaselineStore` from the Flask
  thread while it was created in the asyncio thread, so the scoring panel reported
  "scoring not active." `BaselineStore` is now `check_same_thread=False` + an `RLock`
  on every connection-touching method; the panel populates correctly.
- **Aircraft panel — current sky (P6 core)** — `/api/aircraft` now serves the live
  per-ICAO index (`current_aircraft()`), so a refresh rebuilds the present sky once
  per airframe instead of a churn-evicted slice of the 200-event push-log. The map
  decays markers by recency (~120 s) while the **table retains a longer detection log**
  (`AIRCRAFT_RETENTION_SECONDS`, default 3600) — the same two-lens "what's active now
  vs. what has been seen" split the persistent-scoring panel uses. Aircraft and drones
  now persist across a refresh, and the WiFi/aircraft "Identity" column was renamed
  **Contact**.
- **Sensor-chiclet accuracy** — the GUI status chiclets now reflect true runtime
  state: ADS-B shows green whenever the decoder is working in SHARED mode (it was
  greying while live), and DroneRF greys only on the real auto-disable signal
  (`drone_rf.auto_disabled`), not merely when `can_scan` is false under the SDR
  coordinator.

**Update (2026-06-15) — durable GUI history (P5 slice) + P6 remainder shipped.**
Two more merges, both deployed on chase:
- **Durable detection/alert history (P5)** — the WiFi/BT, Drone, and Alerts panels
  now rebuild from the on-disk session logs (`events.jsonl` / `drone.jsonl` /
  `alerts.jsonl`), walking session directories newest-first, so a page refresh **and
  a service restart** show the real history instead of the in-memory 200-cap (empty
  after a restart, truncated otherwise). Crucially, **alerts are now persisted and
  shown at all** — the orchestrator never pushed `alert` events to the GUI and wrote
  no `alerts.jsonl`, so the Alerts tab had always been empty despite alerts going out
  via the backend. `current-sky` panels (aircraft, Remote ID) intentionally stay on
  the live index, not the disk-history lens.
- **P6 remainder** — flight tracks are now **bounded** (`AIRCRAFT_TRACK_MAX_POINTS`,
  default 500; a long loiter no longer grows a track without limit); **ID-less
  aircraft** are skipped from the per-ICAO log instead of collapsing into one bogus
  "unknown" airframe; the **Remote ID index is pruned** on the retention window; and
  the **Remote ID GUI surface** shipped — `/api/remote_id` + a Remote ID tab fed by
  live `push_event("remote_id", …)`, the prerequisite for P7's loitering-UAS case.
  (The drone index is band-keyed — ~5 entries max — so it needs no pruning. The
  returning-ICAO track-gap is the one P6 item deferred.)

**Update (2026-06-19) — soaks #2/#3 closed the FP-rate gate; deploy hardened;
fingerprinting enriched.** A dense run of validation + fixes, all merged and live on
chase (full test evidence in
[field-findings-2026-06.md](field-findings-2026-06.md), soaks #2 and #3):
- **Off-schedule flood found and fixed (soak #2 → #138).** Post-freeze, the novelty
  flood stayed dead but the noise *moved* to `off_schedule` on held-MAC randomized
  clients (~50 suspicious/poll). Fix: a randomized device with no fingerprint is now
  **off-schedule-ineligible** (mirroring novelty), and WiFi pages **likely+ only**
  (`WIFI_ALERT_MIN_SCORE`), suspicious is display-only. A fingerprinted randomized
  device still off-schedules — that's correct, it's trackable.
- **Soak #3 validated the fix over ~42 h** (reused frozen baseline): 240 paged /
  ~74.6k display-only / **0 dropped**, SDR time-share held with **0 wedges**,
  returning-aircraft detection live, and **memory bounded** (~116–200 MB, no drift —
  the post-freeze `all_events` is deduped per device, retiring the P0 leak worry for
  real). Concluded early to swap in a longer-range BT dongle.
- **Power-cycle deploy hardening (#131–#141).** Single-SDR coordinator settle barrier
  (readsb↔DroneRF handoff no longer wedges), BLE controller raised LE-on at boot,
  refreshed deploy assets (`set-bt-up.sh`, service units, `setup.sh`), single-source
  RTL IDs, bounded ProbeAnalyzer history.
- **Durable GUI history completed across ALL panels (P5).** WiFi/BT scoring now
  persists across a refresh (`#142` — re-flag appends on alert-level change so the
  disk-seed reflects current state) and the **aircraft table survives a refresh AND a
  restart** (`#144` — `/api/aircraft` merges the durable on-disk log with the live
  current-sky index). Every panel now rebuilds from disk.
- **ADS-B reconnect fix (#145).** `poll_aircraft()` returned `[]` when the session
  was down, so the orchestrator's reconnect never fired and a startup connect that
  lost the race with readsb's cold-start stranded ADS-B disconnected forever. Now it
  raises → reconnect recovers. (Node-local `READSB_URL` also corrected to this box's
  tar1090 path `…/tar1090/data/aircraft.json`.)
- **Fingerprinting enriched — round 1 (#146).** WiFi: the preferred-network list
  ("former networks") now accumulates per **rotation-stable IE hash** (`pnl_evidence`,
  not per MAC) so it survives rotation; a parallel `compute_pnl_fingerprint` anchors a
  stable identity on it. BLE: the parser now keeps the **reconnect signals** it used
  to discard — directed adverts (`ADV_DIRECT_IND`), solicited service UUIDs, 128-bit
  UUIDs, and a masked manufacturer-data type prefix — folded into `ble-fp`. **Capture
  + keying + GUI only; the scoring/baseline key is deliberately unchanged** (validated
  on real data first; flood-safe). New GUI columns: *Known Networks* + *Reconnect*.

## What drives the sequencing now

The endurance question that drove the *original* sequencing — the unproven
post-freeze memory leak — is **settled**: soak #3 ran ~42 h past the freeze with
memory bounded. The new driver is the gap between *"works in a soak"* and
*"trustworthy at an unknown location."* Two things stand between the validated
prototype and a tool you'd deploy cold, and they set the lead priority
(**detection-quality completion**):

1. **It's blind during learning (P2).** A 48–72 h baseline that flags nothing while
   it learns will quietly bake an already-present surveillance device into "normal."
   A real deployment can't assume a clean environment.
2. **It fatigues over days (P3).** Post-freeze, every benign newcomer (a neighbour's
   new phone, a visitor) reads novel forever. Soak #1's novelty flood and soak #2's
   off-schedule flood are fixed, but the *durable* answer to slow novelty accrual is
   a rolling baseline that promotes consistently-present devices without absorbing an
   intermittent or patient adversary.

Identity and air-picture depth (the fingerprinting program, cross-session linkage,
AP evil-twin) follow detection-quality — they make alerts *mean more*, but a
detector you can't trust during learning isn't deployable regardless.

## Forward roadmap (post-validation)

Priority order, re-sequenced for the deployable-trustworthiness bar:

1. **Detection-quality completion (lead).** P2 egregious-during-baseline + P3 rolling
   adaptation + the owed P1 approaching walk-test. Closes the "blind while learning"
   and "fatigues over days" gaps — the two things between a soak-validated detector
   and a cold-deployable one. Detail: P1/P2/P3 sections below.
2. **Fingerprinting program (round 2+).** Validate the shipped PNL/reconnect
   enrichment on real captures, then wire it into scoring; then cross-PHY WiFi↔BT
   linking (one device → one contact). Deepens randomization-resistant identity.
   Design: [design-entity-fingerprinting.md](design-entity-fingerprinting.md).
3. **Cross-session returning-entity (P4 remainder).** Link fingerprints into stable
   entities across days/sessions and emit "returning entity" — "was this device here
   yesterday / casing me last week," the original counter-surveillance payoff.
4. **AP evil-twin detection.** Fingerprint AP beacons; flag a known SSID appearing
   with a new IE set/BSSID, or an AP beaconing what nearby devices probe for (karma).

Iterate a shorter confirmation soak after P2+P3 land, read for the during-learning
and multi-day FP behaviour specifically.

---

## Phases

Status reference (detail in the per-phase sections below). The last column is the
**forward priority** from the post-validation re-frame, not the old soak-blocking
flag. ① = detection-quality lead; ②–④ = the identity/air-picture program after it.

| Phase | Goal | Status | Forward priority |
|---|---|---|---|
| **P0** | Endurance hardening (post-freeze memory + disk) | ✅ Done — merged #74; forced-freeze + soak #3 (~42 h) validated bounded memory | ✅ shipped |
| **P1** | Approaching trigger merged + walk-tested (Phase 2.5) | ◑ Merged & green; owes the positive walk-test | ① detection-quality (owes walk-test) |
| **P2** | Egregious-during-baseline safety net (§5.2) | ☐ Not started | **① detection-quality (lead)** |
| **P3** | Adaptation — rolling baseline (§5.5) | ☐ Not started (design drafted) | **① detection-quality (lead)** |
| **P4** | Cross-session entity resolution (Phase F) | ◑ In progress — randomization-resistant fingerprint capture + keying merged & live; cut the flood ~36→3–5/cycle. **Enriched (round 1, #146):** WiFi PNL accumulated per IE hash; BLE reconnect signals (directed/solicited/128-bit/mfg-structure) — capture+keying+GUI, scoring untouched. Cross-session *returning-entity* linkage + scoring integration remain | ②/③ identity program |
| **P5** | Fixed-mode GUI framing + durable history | ✅ Complete — contact designators, scoring-panel thread-safety, baseline-state header strip (learning/frozen + countdown), sortable/filterable columns + CSV, and **durable history across ALL panels**: WiFi/BT (incl. scoring, #142), Aircraft (refresh+restart, #144), Drone, Remote ID, Alerts all rebuild from disk; **GUI is now a live mirror** (re-seed on reconnect + periodic resync, #149) | ✅ shipped |
| **P6** | Air-picture GUI: aircraft panel fix + decay + Remote ID surface | ✅ Complete — current-sky panel, decay, chiclet accuracy, **bounded tracks**, **ID-less split**, Remote ID index pruning + **Remote ID GUI surface** merged; the final pass widened the map current-sky window (a too-tight 120 s cutoff blanked the map under sparse reception), set **24 h aircraft retention**, and shipped **returning-ICAO as same identity** with a marked track gap + an of-interest flag (bridges to P7) | ✅ shipped |
| **P7** | Aircraft of interest: orbit/loiter detection | ◑ Mostly shipped — **persistence-score** model (mirrors the mobile engine; transit≈0, loiter/return climbs): pure geometry + scorer (`air_geometry.py` / `air_scoring.py`), live scoring wired into `_poll_adsb` against a GPS/home reference, and **alerting reframed** — aircraft alert only when *of-interest* (not every airframe), drone-RF alerts only after sustained sweeps. **Soak #3 confirmed the reframe held** (0 paged aircraft flood; returning-aircraft live). **Still deferred:** durable cross-day per-ICAO baseline + daily-orbiter novelty suppression; GUI severity badge; Remote ID loiter fusion | follow-on (remainder) |

### P0 — Endurance hardening (blocking)

**Why.** The post-freeze event-stream growth above, plus the observation history
that grew ~14 MB/hr (~1 GB/day) with no pruning.

**Scope.** Collapse repeated flags of the same entity into one ongoing detection
so `all_events` / the GUI feed / the JSONL don't grow per-poll — the in-flight
`RateLimiter` already does this for ntfy; do the equivalent for the event stream
(or cap `all_events` with periodic flush). Add age- or size-based pruning /
rotation to the observation history in `entity_store`, with an explicit disk
budget (chase has ~38 GB free, so a 72h run fits, but make it a decision).

**Tests.**
- Off-hardware: a simulated post-freeze run feeding the same flagged device over
  many polls yields a bounded event list (not N×devices); pruning holds
  `observations` under the cap.
- On chase: a **forced-freeze** run (a tiny baseline window) so the node actually
  crosses into post-freeze scoring with real devices, sampling RSS to confirm it
  stays flat across that boundary — the test the 4h soak could not do.

**Exit gate.** RSS flat across a multi-hour post-freeze run; observations bounded.

**Status (2026-06-07): DONE, merged #74.** All four per-poll streams (WiFi,
aircraft, drone, Remote ID) now collapse repeated flags of one entity into a
single ongoing detection, and the observation history is bounded by a retention
sweep. The forced-freeze test was run on chase against copies of the live
baseline and observation databases: the freeze engaged, the frozen RSSI stats
were immutable to post-freeze traffic, and a 472k-row history pruned by age in
seconds. The remaining whole-system proof is the multi-day soak below.

### P1 — Approaching trigger merged + walk-tested (Phase 2.5)

**Why.** Coded but unmerged, and owes its positive proof.

**Scope.** Merge the approaching trigger; perform the **operator walk-test** still
owed (a device deliberately moved closer must trip it); confirm the 15–18%
zero-RSSI placeholders are skipped in the approaching path as they already are in
the baseline stats; tune the margin only if the walk-test shows mis-fires.

**Tests.** Existing unit suite; on chase, a controlled walk-toward trips
approaching while the ambient false-positive rate among stationary devices stays
low.

**Exit gate.** A real approach fires; ambient FP acceptable.

### P2 — Egregious-during-baseline safety net (§5.2)

**Why.** A 48–72h baseline that flags nothing during learning will quietly bake an
already-present surveillance device into "normal." A real deployment can't assume
a clean environment, so the node must still shout about egregious conditions
while it learns.

**Scope.** During the learning window, keep emitting alerts for egregious triggers
— a device very close / very strong on first contact, or trending stronger —
reusing the RSSI stats and the approaching machinery from P1.

**Tests.** Off-hardware: a strong/close device during learning flags while normal
traffic stays silent. On chase: the operator's own deliberately-close device
flags during baseline without flooding.

**Exit gate.** Egregious flags fire during learning, sparingly.

### P3 — Adaptation: rolling baseline (§5.5)

**Why.** Over a multi-day post-freeze run, every benign newcomer (a neighbor's new
phone, a visitor) is "novel" forever — alert fatigue. A slow rolling baseline
update lets a consistently-present device become normal, without absorbing an
intermittent or patient adversary.

**Scope.** An operator-selectable adaptation posture and a consistency window that
promotes a device to baseline only after sustained presence.

**Tests.** Off-hardware: a device present consistently across the adaptation
window stops flagging, while an intermittent returner does not get absorbed. On
chase: the post-freeze novelty FP rate decays over days.

**Exit gate.** FP decays without swallowing intermittent returners.

### P4 — Cross-session entity resolution (Phase F)

**Why.** The entity store holds the raw probe and fingerprint evidence, but
nothing yet links a device's rotating MACs into one logical entity across
sessions and days. That linkage — "is this the same device that was here
yesterday, or cased me last week" — is the core counter-surveillance value the
store was built for.

**Scope.** A resolution pass over the entity store that merges fingerprints /
probe evidence into stable entities across sessions and surfaces "returning
entity" as a signal. The durable answer to the soak's randomized-MAC novelty
flood: key on a **payload fingerprint that survives MAC rotation**, not the MAC.
BLE-as-identity stays weak (the stable subset is appliances), but BLE adds
**proximity** and a **person-level wearable cluster** — and capturing BLE/WiFi
beacon payloads (incl. BLE RSSI) is the prerequisite. Full design:
[design-entity-fingerprinting.md](design-entity-fingerprinting.md).

**Built so far (2026-06-14).** The within-session half is done and live: passive
BLE advertisement capture (raw HCI), unified BLE/WiFi fingerprint signatures, and
`FixedScoring` keying randomized devices by `wifi-fp:` / `ble-fp:` so a device's
rotating addresses collapse to one identity for novelty/off-schedule. This is the
direct fix for the randomized-MAC flood (cut ~36→3–5 flags/cycle on chase). What
remains is the **cross-session pass**: linking those fingerprints into stable
entities across days and emitting "returning entity."

**Tests.** Off-hardware: two MAC-rotated sightings sharing a probe fingerprint
resolve to one entity; distinct devices don't merge. On chase: a known device
re-identifies across a restart and a day boundary.

**Exit gate.** Cross-session re-identification works on known devices.

### P5 — Fixed-mode GUI framing + durable history

**Why.** The GUI got the mode toggle but still shows a raw device list, not the
fixed-node lens; there is a known bug where the dashboard's aircraft panel does
not show ADS-B that readsb has; and **the detection/alert view does not survive a
page refresh.** The GUI is backed only by in-memory state — the server's bounded
`_recent_*` caches (200 events each) plus whatever the browser has accumulated
live — so a refresh, a reconnect, or a service restart re-seeds from those caps
and silently drops everything older. The raw `events.jsonl` and the SQLite stores
keep the full history, but the operator, who lives in the GUI, loses sight of it.
For a counter-surveillance tool whose entire value is "what showed up, and when,"
a forgetful operator surface is a correctness gap, not a polish item — and it bites
hardest exactly when volume is high (the soak's floods would blow past the 200-cap
in seconds).

**Scope.** Show baseline state (learning vs. frozen, time remaining), the anomaly
list framed by signal / severity, and returning entities; and **make the detection
and alert history durable across a refresh** — back the panels with the on-disk
session store (read history on load, paginate rather than truncate) instead of only
the in-memory caches, so a reload or restart rebuilds the operator's view rather
than forgetting it. Alerts especially must persist: an alert the operator missed
while away from the screen must still be there when they return. (The aircraft
panel's own version of this gap is split out as **P6** — a self-contained bug with
a live reproduction, pulled ahead of the broader framing work.)

**Tests.** Panels populate; after a forced page reload — and after a service
restart — the detection and alert lists rebuild from disk to the same history, not
an empty or truncated view.

**Exit gate.** An operator can read node state and anomalies at a glance, and the
detection/alert history they rely on survives a refresh and a restart.

**Status (2026-06-15): mostly shipped.** Live: the identity-collapse row and **contact
designators** (a device shows as a stable `CLASS-IDENT-#` track label whose number
persists across MAC rotation and restart, entity store keyed by fingerprint; the
redundant Device column was dropped); the scoring-panel cross-thread SQLite fix so
baseline state renders; and the **durable detection/alert history** — the WiFi/BT,
Drone, and Alerts panels rebuild from the on-disk session logs across a refresh and a
restart (walking session dirs newest-first), and **alerts are persisted and shown for
the first time** (`alerts.jsonl` + a live `push_event("alert", …)`; the tab had been
unfed). **Still owed:** the learning-vs-frozen baseline framing (time remaining) and
the anomaly list framed by signal/severity.

### P6 — Aircraft panel: serve the live current sky, not a push-log (near-term bug)

**Why.** Operator-reproduced on chase, 2026-06-10: an aircraft doing tight circles
in tar1090 — so readsb has it — drops off the dashboard's aircraft panel after a
page refresh. Verified live against the running node, the mechanism is **cache
eviction in the GUI's re-seed path**, not a decode miss:

1. readsb keeps each aircraft for its own staleness timeout, so tar1090 keeps
   showing the circling target. PV pushes an aircraft to the GUI only when it
   freshly polls a position update for it.
2. `/api/aircraft` — what a refresh re-seeds from — serves the flat `_recent_aircraft`
   **push-log**, capped at 200 events. With ~18 aircraft each re-pushing on most
   5 s polls, those 200 slots hold only about the last minute of pushes (and the
   orchestrator pushes the *same* dict object each time, so the slots are even more
   redundant). Any aircraft PV hasn't pushed within that ~minute is already evicted
   from the cap — and under sparse RTL-SDR reception, where the receiver catches one
   target at a time, a circling aircraft routinely goes that long between PV pushes.

So the live SSE session shows the plane (it caught each push as it arrived) and a
refresh loses it (its last push aged out of the 200-cap). PV's own per-ICAO
current-aircraft map (`_aircraft_index`, never pruned) still holds it — the panel
just re-seeds from the wrong, churn-starved structure. (A live check found PV
holding 18 aircraft while readsb's instantaneous view held 1 — the same eviction
story from the other side: the cap is dominated by re-push churn from the most
active aircraft.) Null-position aircraft — the earlier hypothesis — are a separate,
already-handled case: shown as "no position", omitted from the map (3 of the 18 in
the live check).

**Scope.** Serve `/api/aircraft` from the current-aircraft index (`_aircraft_index`)
so a refresh rebuilds the actual present sky rather than a bounded slice of push
history, and key the client's aircraft state by ICAO so the seed path can never
list one airframe twice — **one event per aircraft, locations updated in place**.
Add **recency decay** to the map: a marker shrinks and greys by time since last
seen, then expires, so the operator sees what is *active now*, not a frozen pile of
stale dots. Supporting data-model fixes (also prerequisites for P7): **bound each
aircraft's track** (an orbiter grows it without limit, and the whole track ships to
the GUI on every push), **expire aircraft from the index** on a staleness timeout so
it does not grow across a multi-day run, stop merging ID-less contacts into one
"unknown" airframe, and treat a returning ICAO as the **same identity with a marked
track gap**. This is the aircraft-specific instance of the P5 durability gap, but it
is self-contained with a live reproduction, so it is pulled ahead as a near-term bug.

**Surface Remote ID.** The same air-picture GUI is missing a Remote ID view: the
node detects UAS via Remote ID, but the dashboard has no way to show them
(`gui/server.py` carries a standing `TODO(remote-id)` for a `/api/remote_id`
endpoint and a Remote ID tab). Add that surface here — it is near-term GUI plumbing
in the same domain, and it is where **P7's loitering-UAS case must appear**, so it
is a prerequisite for the highest-value aircraft-of-interest signal.

**Tests.** A circling aircraft present in readsb stays in the panel — as a single
row — across a page refresh and a reconnect; a departed aircraft decays then ages
out; position-less aircraft still render as "no position"; a track stays bounded
under a long orbit; a Remote ID detection appears in its tab.

**Exit gate.** What readsb holds is in the panel, once per airframe, and stays there
across a refresh for as long as readsb holds it — fading as it goes stale.

**Status (2026-06-15): core + remainder shipped.** Core: `/api/aircraft` serves the
current sky from the per-ICAO index (once per airframe), the map decays markers by
recency while the table retains a longer detection log (`AIRCRAFT_RETENTION_SECONDS`),
aircraft and drones survive a refresh (the live-reproduced eviction bug is fixed), and
the sensor-chiclet accuracy fix rode along (ADS-B green when working in SHARED mode;
DroneRF greys only on real auto-disable). Remainder: **bounded per-aircraft/Remote-ID
tracks** (`AIRCRAFT_TRACK_MAX_POINTS`, default 500), **ID-less aircraft no longer merge**
into one "unknown" airframe (skipped from the per-ICAO log, counted in
`stats.aircraft_idless_skipped`), the **Remote ID index is pruned** on the retention
window (the aircraft index already was; the drone index is band-keyed and inherently
bounded), and the **Remote ID surface** shipped — `/api/remote_id` + a Remote ID tab
fed by live `push_event("remote_id", …)`, the prerequisite for P7's loitering-UAS case.
**Status (2026-06-16): P6 complete.** Final pass:
- **Map plotting fix** — the `addAircraftMarker` 120 s hard cutoff (added in #123)
  dropped a still-present plane off the map under sparse single-target RTL-SDR
  reception (it lingered in the table but vanished from the map). The current-sky
  window widened to 10 min (`AIRCRAFT_MAP_DECAY_MS`), keeping the recency fade.
- **24 h retention** — `AIRCRAFT_RETENTION_SECONDS` 1 h → 24 h so a returning
  airframe is recognised as the SAME identity rather than a fresh contact.
- **Returning-ICAO (the deferred item) + of-interest signal** — a re-sighting after
  a gap > `AIRCRAFT_RETURN_GAP_SECONDS` (10 min) marks a track gap (KML flight path
  breaks into legs instead of drawing straight across the absence), flags the
  contact `returning` with a count, records it to the alerts feed (no backend send —
  avoids fatigue), and the GUI shows it amber with a ↩ RETURN badge. This is the
  near-term bridge into P7's returning-aircraft-of-interest scoring.

### P7 — Aircraft of interest: orbit/loiter detection (ADS-B + Remote ID)

**Why.** Aircraft are currently display-and-enrichment only — nothing scores them.
But the air picture carries a real counter-surveillance question: *is something
watching from above, and has it watched before?* The operator asked for a
returning-aircraft signal analogous to the WiFi/BT work. Unlike WiFi, identity is
the *easy* part here — the ICAO address is a stable, non-rotating airframe key, so
the returning-entity problem (P4) nearly vanishes; the work is **geometry and
baseline discipline**.

**Scope.** Score aircraft against the node's own position. The one distinction that
matters is **transit vs. orbit**: almost everything is fly-by traffic (approach /
departure, coastline tour helicopters) and benign; the signal is an aircraft that
**orbits or loiters in the immediate area** — circle patterns overhead, a slow
racetrack within visual range. Flag the *behavior*, never assuming a circling
aircraft is benign (a known-benign orbiter is suppressed by the baseline / operator
whitelist, not by the code guessing "training"). Trigger = inside a horizontal
radius, under an altitude ceiling (3-D slant range, so a high overflight does not
count), for a sustained dwell, with an orbit signature (cumulative heading change)
rather than a straight pass — tolerant of the gappy tracks sparse reception
produces. First-cut defaults: 5 nm / 5,000 ft / 8 min / >270°, all configurable.
Reference position defaults to **GPS (smoothed), with a GUI override** for
degradation. A **durable per-ICAO baseline** makes daily orbiters (tours, training,
medevacy) normal and a *novel* loiterer the signal — the same baseline-then-flag 
discipline that tamed the WiFi flood — with an
egregious-during-learning carve-out and an interest weight for blocked/anonymous,
military, no-callsign, and rotorcraft. The orbit logic is **modality-agnostic**: a
loitering small UAS via Remote ID is the highest-value case and rides the same path
(its dashboard surface is the Remote ID tab added in P6).
Full design: [design-aircraft-of-interest.md](design-aircraft-of-interest.md).

**Tests.** Off-hardware: a synthetic orbit track near the node flags while a
straight transit and a high overflight do not; a baselined daily orbiter stops
flagging while a novel one fires; dwell/heading accumulate across track gaps. On
chase: a real circling aircraft is surfaced as
"orbiting," and the ambient false-positive rate among transit traffic stays low.

**Exit gate.** A circling aircraft in the immediate area is distinguished from
transit and surfaced; a novel returning loiterer alerts; routine traffic does not.

---

## The multi-day soak — the validation gate (PASSED)

**This gate has been run and cleared** (soaks #1–#3 below). The method that mattered:
set `FIXED_BASELINE_HOURS` so the baseline **freezes inside the soak** so it actually
exercises post-freeze scoring — the thing the original 4-hour run never reached.
Soak #3 (~42 h, baseline frozen) demonstrated learn-then-detect end-to-end with
bounded memory, disk in budget, a livable post-freeze FP rate (after the #138 fix),
and stability across days with the SDR time-shared. The remaining soak use is a
**shorter confirmation run after P2+P3** to read the during-learning and multi-day FP
behaviour specifically — not a gate, a regression check.

Measure (retained for that confirmation run): RSS flat across the freeze boundary,
disk within budget, the post-freeze anomaly and false-positive rates,
egregious-during-baseline behavior, and stability across days. A run that survives
multi-day, stays bounded, and produces a sane anomaly stream with tolerable FP —
demonstrating learn-then-detect end to end —
is the working prototype.

### Soak #1 — chase, 2026-06-07 → 09 (P0+P1+P2, 48h, DroneRF off)

**The machinery works; the false-positive rate did not.** What passed: clean
freeze at +48h; rich banking (5,026 profiles, 1,872 with ≥10 RSSI samples →
approaching-eligible, 4,227 with ≥12 baseline hours → off-schedule-eligible);
0 restarts; both sensors advancing post-freeze; RSS modest (99 → 155 MB across the
freeze). What failed — two false-positive **floods**:

1. **Egregious-during-baseline flooded** — ~22 devices/poll flagged at the −45 dBm
   default for the whole 48h. chase is a dense RF environment; −45 is not "in the
   operator's space." Fix: the threshold is now **environment-density-tuned**
   (`NODE_DENSITY` → dense −30 / suburban −40 / rural −50; `EGREGIOUS_SIGNAL_DBM`
   overrides). chase runs `dense`.
2. **Post-freeze novelty flooded** — ~969 devices/poll, ~10.7k ntfy alerts. ~60%
   were randomized MACs: a baselined device, post-MAC-rotation, reads as
   brand-new. Fingerprint keying only rescues the ~quarter that broadcast named
   probes. Fix: **a randomized MAC with no fingerprint must show sustained
   presence (`NOVELTY_RANDOM_MIN_OBSERVATIONS`) before novelty fires.** A possible
   off-schedule-at-hour-rollover contribution is unconfirmed — `score_breakdown`
   is now written to `events.jsonl` so soak #2 can decompose the flag mix.

Soak #2 runs the fix-stack (above changes) to confirm the FP rate is livable; the
walk-test is held until then. This is the alert-fatigue risk below, realized — it
elevates **P3 (rolling baseline)** from "later" toward "needed for usability."

### Soak #2 — chase, 2026-06-17 (first post-freeze read)

The novelty flood stayed dead (~2% of flags) ✓ — but the noise **moved to
`off_schedule`**: ~50 WiFi suspicious/poll, 97% on held-MAC randomized (`mac:`)
clients flagging on one new hour-of-day, all at 0.50; `alerts_dropped` shedding under
the load. Root cause: a randomized-no-fp device was novelty-ineligible but still
*off-schedule-eligible*. Fixed (#138): off-schedule-ineligible for randomized-no-fp +
page likely+ only. Full record: [field-findings-2026-06.md](field-findings-2026-06.md).

### Soak #3 — chase, 2026-06-17 → 19 (~42 h, post-fix validation)

The #138 fix held over a multi-hour run: **240 paged / ~74.6k display-only / 0
dropped**, SDR time-share clean (**0 wedges** with ADS-B + DroneRF sharing the SDR),
returning-aircraft live, and **memory bounded** (~116–200 MB, no drift). The
deferred-from-soak-#2 memory worry (post-freeze `all_events` growth, the old P0
concern) is retired: the list dedups per device. BLE remains data-starved (1 `ble-fp`)
— addressed at conclusion by the longer-range dongle. Concluded early for that swap.
Full record + next steps: [field-findings-2026-06.md](field-findings-2026-06.md).

## Sequencing

**Executed so far:** P0 → soaks #1/#2 (found + fixed the novelty then off-schedule
floods) → P4-core fingerprinting + P5 + P6 + P7-core → soak #3 (validation gate
passed). That chain is history; the document above records it.

**Forward (from 2026-06-19):**
1. **Detection-quality completion (lead).** P2 (egregious-during-baseline) and P3
   (rolling adaptation) together, with the owed P1 walk-test alongside — they close
   the "blind while learning" and "fatigues over days" gaps. → a **short confirmation
   soak** read for during-learning and multi-day FP behaviour.
2. **Fingerprinting program.** Validate the shipped PNL/reconnect enrichment, then
   wire it into scoring; then cross-PHY WiFi↔BT linking.
3. **Cross-session returning-entity (P4 remainder).**
4. **AP evil-twin detection**, and the **P7 remainder** (cross-day air baseline,
   severity badge, Remote ID loiter fusion) as follow-on.

The forward chain is detection-quality first because trustworthiness-during-learning
gates deployability; the identity/air-picture depth that follows raises what the
alerts *mean*, not whether you can rely on the detector.

## Deliberately deferred (per the design doc)

GPS-movement sanity check (§2.2) and relocation re-baseline (§2.3) — low value for
a stationary base station; WiGLE resident-vs-visitor enrichment (§9); watchboxes /
origin-geofencing (§10); multi-node correlation (§11.8). All genuinely later.

## Standing risks

- Post-freeze memory leak — **RETIRED**. Soak #3 ran ~42 h post-freeze with RSS
  bounded (~116–200 MB, no drift); the `all_events` stream dedups per device.
- Alert fatigue — **largely addressed**. Soak #1's novelty flood and soak #2's
  off-schedule flood are both fixed (fingerprint keying; randomized-no-fp made
  off-schedule-ineligible; page likely+ only). Soak #3 was livable (240 paged over
  ~42 h, 0 dropped). **P3 (rolling baseline) remains the durable fix** for benign
  newcomers going stale-novel over many days — still worth doing, no longer the only
  thing between us and a usable stream.
- **Fingerprint over-merge** (new, with the #146 enrichment): content fingerprints
  aren't unique per device, so widening coverage risks fusing distinct devices. The
  enriched identities stay capture/display-only until validated; the over-merge guard
  (bare vendor / no-named-SSID stays ungroupable) is preserved.
- BLE-as-identity is environment-limited here (mostly bare advertisers); the new
  longer-range dongle improves capture but linking BLE↔WiFi (cross-PHY) is unbuilt.
