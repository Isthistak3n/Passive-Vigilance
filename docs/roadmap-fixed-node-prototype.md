# Roadmap: Fixed-Node Counter-Surveillance Prototype

**Status:** Active roadmap (2026-06). Living document — update the phase status as
work lands.
**Audience:** Implementers, reviewers, operators.
**Companion:** [field-findings-2026-06.md](field-findings-2026-06.md) (the test
evidence behind these decisions) and
[design-detection-modes.md](design-detection-modes.md) (the full design).

---

## North star — what "working prototype" means

A node you can leave at a location for several days that learns the place's
normal RF pattern of life, then reliably surfaces genuine anomalies — a new
device that shows up and stays, a known device behaving off-pattern, something
physically closing in — at a false-positive rate an operator can live with,
while staying up and bounded in memory and disk for the whole run. The multi-day
soak is the **validation gate** for that, not a build step.

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

## The one finding that drives the sequencing

The clean 4-hour memory soak was a **learning-phase** result. With a 72h baseline
window the soak never froze, so `FixedScoring` returned no detections (WiFi:0
events) and the orchestrator's unbounded `all_events` list stayed empty. The
moment a real run crosses the baseline freeze, novelty and off-schedule re-fire
on every poll for every qualifying device, and the orchestrator appends each to
`all_events` and pushes it to the GUI — the mobile leak re-incarnated, just
deferred past the freeze. **The prototype's memory profile is currently unproven
for the half of its life that matters.** Hence: endurance hardening first, not
new detection features.

---

## Phases

| Phase | Goal | Status | Blocking for first multi-day soak? |
|---|---|---|---|
| **P0** | Endurance hardening (post-freeze memory + disk) | ✅ Done — merged #74, forced-freeze validated on chase | **Yes** |
| **P1** | Approaching trigger merged + walk-tested (Phase 2.5) | ◑ Rebased on `main`, green; walk-test gated on 2026-06-10 freeze | No (recommended) |
| **P2** | Egregious-during-baseline safety net (§5.2) | ☐ Not started | Strongly recommended |
| **P3** | Adaptation — rolling baseline (§5.5) | ☐ Not started | No |
| **P4** | Cross-session entity resolution (Phase F) | ☐ Not started | No |
| **P5** | Fixed-mode GUI framing + durable history | ☐ Not started | No |
| **P6** | Aircraft panel: live current-sky view (bug) | ☐ Not started — near-term, independent of phasing | No |

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

### P6 — Aircraft panel: serve the live current sky, not a push-log (near-term bug)

**Why.** Operator-reproduced on chase, 2026-06-10: an aircraft loitering in
tar1090 — so readsb has it continuously — drops off the dashboard's aircraft panel
after a page refresh. The decode is fine; the loss is in the GUI's re-seed path,
and the root cause is two interacting things:

1. The orchestrator pushes an aircraft to the GUI **once, on first detection**, and
   thereafter only re-pushes it when its track actually *moves* (`_poll_adsb`
   pushes on `moved`). A loitering or holding aircraft barely moves, so after the
   first push it goes quiet on the live stream.
2. `/api/aircraft` — what a refresh re-seeds from — serves the flat `_recent_aircraft`
   **push-log**, capped at the last 200 events. The loiterer's single push scrolls
   off the back as other traffic accumulates, so the refreshed page seeds an
   aircraft list that no longer contains it — even though the orchestrator's own
   `_aircraft_index` (the live, per-ICAO current-aircraft map) still holds it and
   readsb still reports it.

So the live SSE session shows the plane (it caught the one push) and the refresh
loses it (that push aged out of the cap). The data was on the server the whole
time; the panel just reads from the wrong structure. (Null-position aircraft — the
earlier hypothesis — are a real but separate, already-handled case: they render as
"no position" in the table and are omitted from the map.)

**Scope.** Serve `/api/aircraft` from the current-aircraft index (`_aircraft_index`)
so a refresh rebuilds the actual present sky rather than a bounded slice of push
history; optionally re-push loitering aircraft on a heartbeat so the live stream
keeps them current too. This is the aircraft-specific instance of the P5 durability
gap, but it is self-contained with a clear fix and a live reproduction, so it is
pulled ahead as a near-term bug rather than waiting on the broader P5 work.

**Tests.** A loitering aircraft present in readsb stays in the panel across a page
refresh and a reconnect; a departed aircraft ages out; position-less aircraft still
render as "no position."

**Exit gate.** What readsb holds is in the panel and stays there across a refresh,
for as long as readsb holds it.

---

## The multi-day soak — the validation gate

Run it **after P0** at minimum, ideally P0 + P2. The critical config change from
the 4-hour run: set `FIXED_BASELINE_HOURS` so the baseline **freezes inside the
soak** (e.g. a 48h baseline then 24–48h of post-freeze observation), so it
actually exercises post-freeze scoring — the thing the 4h soak never reached.

Measure: RSS flat across the freeze boundary (the P0 proof), disk within budget
(P0 pruning), the real post-freeze anomaly and false-positive rates,
egregious-during-baseline behavior, and stability across days with the SDR path
disabled. A run that survives multi-day, stays bounded, and produces a sane
anomaly stream with tolerable FP — demonstrating learn-then-detect end to end —
is the working prototype.

## Sequencing

P0 → (P1, P2 in parallel) → **first multi-day soak** → P3 → P4 → P5, iterating the
soak after P3 once adaptation is in. The first long soak should be read as an
endurance-and-correctness test, not a usability one, until P3 lands. **P6 sits
outside this chain** — it is a self-contained GUI bug with a live reproduction and
can be fixed at any time, independent of the detection-quality sequencing.

## Deliberately deferred (per the design doc)

GPS-movement sanity check (§2.2) and relocation re-baseline (§2.3) — low value for
a stationary base station; WiGLE resident-vs-visitor enrichment (§9); watchboxes /
origin-geofencing (§10); multi-node correlation (§11.8). All genuinely later.

## Standing risks

- Treating the 4h memory pass as sufficient and finding the post-freeze leak only
  during the multi-day run — P0 plus the forced-freeze test exists to retire this.
- Alert fatigue without P3 — a multi-day run past freeze accumulates "novel
  forever" devices.
- Every recent PR is blocked on **commit signing** (verified-signatures ruleset) —
  resolve so these merges don't stall at the gate.
