# Detection Modes: Fixed vs. Mobile Nodes

**Status:** Partially implemented — see the phase status below.
**Tracks:** Issue #50 (stationary scoring), supersedes the mobile-only scoring assumption
**Audience:** Implementers (Claude Code), reviewers, operators

> **Implementation status (as of 2026-06).** This document is the full phased
> design; not all of it has shipped. Merged to `main`:
> - Mode selector + strategy fork (`NODE_MODE`, `ScoringEngine`, `MobileScoring`
>   vs `FixedScoring`) — Phase 1, #66.
> - GUI mode toggle — Phase 1.5, #67.
> - Fixed-mode baseline + novelty, durable crash-safe baseline — Phase 1, #66.
> - Off-schedule detection + graduated severity + baseline RSSI stats — Phase 2,
>   #68; with the per-device activation guard — #69.
> - Probe-SSID / fingerprint capture feeding the randomized-MAC key — #70.
> - Entity/observation store (recorded at the capture layer for both modes) — #71.
>
> Not yet shipped (genuinely future): the approaching-signal (rising-RSSI)
> trigger and zero-RSSI handling (Phase 2.5, in review), abnormal-dwell,
> egregious-during-baseline alerting (5.2), the GPS-movement sanity check (2.2),
> baseline adaptation (5.5), WiGLE enrichment (§9), watchboxes (§10), and
> multi-node correlation (§11.8).

---

## 1. Purpose and the two threat models

Passive Vigilance scores observed devices to surface potential surveillance assets. The
correct definition of "suspicious" depends entirely on whether the sensor itself is
**moving** or **stationary**. These are not two weightings of one model; they are two
different questions:

- **Mobile node** (wardriving / on-person / vehicle): *"Does this device follow me across
  locations?"* The sensor moves; a threat is a device that shares the sensor's movement —
  seen at multiple distinct locations as the operator travels. Location diversity is the
  signal.

- **Fixed node** (base station / leave-behind): *"What deviates from this location's
  established baseline?"* The sensor is static; the environment has a stable RF signature
  (pattern of life). A threat is a device that departs from that baseline — something new,
  off-schedule, lingering abnormally, or approaching. Deviation from normal is the signal.

A device that scores as a threat in one model is invisible in the other. A fixed node run
under mobile scoring never alerts, because a stationary sensor produces only one location
cluster — every device forfeits the location-diversity component and no device can cross the
threshold. (This is the root cause documented in issue #50: the production scoring is
mobile-shaped, so a fixed node's GUI is structurally empty.) Conversely, a mobile node run
under fixed scoring would flag every new location's environment as anomalous.

Therefore the node's mode is a first-class deployment decision that forks the scoring
pipeline. One capture pipeline feeds one of two scoring strategies, selected at startup.

---

## 2. Mode selection

### 2.1 Explicit, required, no silent default

`NODE_MODE` is a required configuration value (`.env`), one of `fixed` or `mobile`. There is
**no assumed default**. A wrong default is dangerous in both directions:

- A fixed node silently defaulting to mobile never alerts (the #50 failure).
- A mobile node silently defaulting to fixed flags every new environment as a threat.

If `NODE_MODE` is unset, the node logs a prominent error and refuses to enter scoring (it may
still capture, but it must not pretend to be doing threat detection under an assumed mode).
Fail loud, never guess.

### 2.2 GPS-movement sanity check

Mode is declared, not inferred — but the declared mode is **cross-checked against observed
GPS movement** as a misconfiguration guard:

- If `NODE_MODE=fixed` but GPS shows sustained movement beyond a threshold (e.g. the node
  travels more than a few hundred metres and keeps going), emit a loud, repeated warning:
  the node is declared fixed but is moving; scoring assumptions are invalid.
- If `NODE_MODE=mobile` but GPS shows the node stationary for a long period, emit an
  informational note (less severe — a mobile node legitimately sits still sometimes, e.g.
  parked). This is a hint, not an error.

Auto-detection of mode is explicitly **rejected** as the primary mechanism: a fixed node
temporarily carried indoors, or a mobile node at a long stop, would mis-infer. Declared mode
plus a movement sanity check is safer than inference.

### 2.3 Relocation of a fixed node

If a node declared `fixed` detects that it has moved to a materially different location (a
GPS jump beyond threshold that then settles at a new stable position), its baseline is now
invalid — it describes the old environment. Policy:

- Invalidate the existing baseline.
- Re-enter the baseline learning period (Section 5.1) for the new location.
- Log the re-baseline event prominently so the operator knows the node is relearning and
  (mostly) not yet alerting.

Without this, a relocated fixed node flags its entire new environment as anomalous.

---

## 3. Architecture: shared pipeline, forked scoring

Mode forks the *scoring strategy*, not the whole system. The split:

**Shared (mode-agnostic) — the entire input pipeline:**

- All capture: Kismet (WiFi/BT), GPS, SDR (ADS-B / DroneRF), Remote ID.
- MAC parsing, OUI/vendor lookup, MAC-randomization detection.
- Probe-SSID fingerprinting and clustering (load-bearing for fixed mode — see 5.3).
- Alert backends (ntfy / Telegram / Discord / console) and rate limiting.
- The GUI shell, SSE/REST transport, the map and tables.
- Output writers (shapefile, KML, GeoJSON, WiGLE).

**Forked (mode-specific):**

- The **scoring engine**: `MobileScoring` (location-diversity) vs. `FixedScoring`
  (baseline-deviation).
- The **baseline lifecycle** (fixed mode only — does not exist in mobile mode).
- The **GUI framing** of results (same data, different lens — Section 6).

### 3.1 Strategy pattern

Define a common `ScoringEngine` interface with a single selected implementation per run:

```
ScoringEngine (interface)
  ├── score(device_observation, context) -> Score
  ├── on_observation(device_observation)      # update internal state
  └── status() -> dict                        # for GUI / health

MobileScoring(ScoringEngine)    # location-diversity model (existing, works)
FixedScoring(ScoringEngine)     # baseline-deviation model (this design)
```

The orchestrator selects the implementation at startup from `NODE_MODE` and passes
observations to it. This keeps the shared/forked boundary explicit and avoids two parallel
codebases — the capture layer does not know or care which scoring engine is attached.

> **Codebase grounding.** The fork point is the existing `modules/persistence.py`
> (`PersistenceEngine`), which is today's scoring engine. `MobileScoring` is the current
> `PersistenceEngine` logic, essentially unchanged, behind the new interface; `FixedScoring`
> is the new implementation. The probe-fingerprint clustering required by fixed mode (5.3)
> already exists as `mac_utils.group_by_fingerprint` (union-find over shared probe SSIDs).
> `NODE_MODE`'s fail-loud check fits the existing `_validate_config()` pattern in `main.py`.

---

## 4. Mobile mode (existing model — documented for contrast)

Mobile mode is the current, working scoring model and is **not** being redesigned here. It is
documented so the fixed/mobile distinction is crisp and so implementers do not break it.

Mobile scoring weights a device's threat score on components including:

- **Location diversity** — seen at multiple distinct GPS clusters as the operator moves
  (the dominant signal: a device appearing in many of the places you go is following you).
- **Temporal persistence** — seen repeatedly over time.
- **Frequency** — observation count.
- **Signal** — presence/strength as a minor contributor.

This model is correct for a moving sensor. It is the wrong model for a fixed sensor only
because the location-diversity component is structurally unsatisfiable when the sensor does
not move. Mobile mode is retained unchanged; fixed mode is added alongside it.

---

## 5. Fixed mode — pattern of life

The fixed-node model has two phases: a **baseline learning period** during which the node
characterizes the environment's normal RF pattern of life, followed by **ongoing
deviation scoring** against that baseline. A slow **adaptation** process keeps the baseline
current without absorbing patient adversaries.

### 5.1 Baseline learning period

On first start (or after a re-baseline trigger, Section 2.3), the node enters a learning
period of configurable duration (`FIXED_BASELINE_HOURS`, see Section 8 for default
discussion). During this period it builds a per-device behavioral profile rather than
scoring threats normally.

**Per-device profile** (keyed on MAC *and* probe-fingerprint — see 5.3):

- First-seen, last-seen, total observation count.
- Total and typical dwell duration per appearance.
- Time-of-day / day-of-week histogram of appearances (the temporal texture).
- Signal statistics: mean and variance of RSSI (used as stability, not absolute threshold —
  see 5.4).
- Device type / vendor / probe behavior.

At the end of the learning period the profiles are frozen as the **baseline** — the
description of "normal here." The environment's devices fall into rough classes:

- **Permanent fixtures** — continuously present, stable signal, never absent (your router,
  neighbors' APs). The environment itself.
- **Regular transients** — present on a *schedule* (a neighbor's commute, recurring
  visitors). Predictable comings-and-goings are part of normal.
- (Anything not in baseline becomes, in the monitoring phase, a candidate anomaly.)

#### Baseline persistence — required (implementation-critical)

The baseline **must be stored durably and survive service restarts and reboots.** It must
not live only in process memory. This is the single most important phase-2 detail:

- The current scoring engine (`modules/persistence.py`) holds observations **in memory** with
  a 60-minute rolling purge — adequate for mobile mode, fatal for fixed mode. A 48–72h
  baseline held in memory is destroyed by any restart.
- This is not hypothetical: during the 2026-06 crash-loop incident the service auto-restarted
  ~60 times in 70 minutes. A fixed node with an in-memory baseline would have re-entered the
  learning period on every restart and **never alerted** — silently broken in exactly the
  way #50 describes, for a different reason.
- Therefore fixed-mode per-device profiles and the frozen baseline persist to **SQLite** (the
  project's stated event store) and are **reloaded on startup**. On restart the node resumes
  its existing baseline / learning progress rather than starting over. The baseline's learning
  start-time, freeze-time, and per-device profiles are all durable state.

### 5.2 Egregious-threat flagging DURING baseline (critical)

Baseline learning does **not** fully suppress alerting. A sensor deployed into an
*already-compromised* environment must not silently bake an existing surveillance device
into "normal." Therefore, even during the learning period, the node still flags **egregious**
conditions, for example:

- A device that is physically very close / very strong signal on first contact (suggesting
  it is in the operator's immediate space, not passing traffic on the street).
- A device whose signal is *trending stronger* (approaching) during the learning window.
- (Other operator-defined egregious triggers.)

This means baseline mode is "learn the ordinary, but still shout about the obviously
alarming." The operator may also perform a deliberate **clean-environment baseline** (run the
learning period in a known-safe location/time) before deployment, but the system must not
*depend* on that — the egregious-during-baseline path is the safety net for when it cannot be
guaranteed.

### 5.3 MAC randomization — fingerprint-keyed, not MAC-keyed

Modern devices rotate MAC addresses constantly, so "new MAC" is the *default*, not the
exception — most probe requests come from randomized MACs never seen again. Pattern-of-life
keyed naively on MAC would see endless "new devices" and be useless.

Therefore fixed-mode profiling keys on the **probe-SSID fingerprint** (the existing
clustering that groups MACs sharing probe behavior — the feature that merged multiple
randomized MACs into one logical device during live testing), not on the raw MAC. A device's
identity for pattern-of-life is its fingerprint; its rotating MACs are the same logical
entity. This makes the existing `mac_utils` fingerprinting (`group_by_fingerprint`)
load-bearing for fixed-mode anomaly detection rather than a side feature.

Devices with stable (non-randomized) MACs — most APs and many IoT devices — are profiled by
MAC directly. The two keying strategies coexist.

### 5.4 Deviation scoring (monitoring phase)

After baseline, every observed device is scored on **departure from baseline**, not on
absolute properties:

- **Novelty** — not present in baseline at all (highest-value signal: a device that wasn't
  part of the established environment and now is).
- **Off-schedule appearance** — a known device appearing outside its normal time-of-day /
  day-of-week histogram (the regular-transient that shows up at an abnormal time).
- **Abnormal dwell** — a device lingering materially longer than its baseline norm.
- **Signal trend, not magnitude** — RSSI is noisy (multipath, weather, body-blocking), so
  do **not** threshold on absolute dB. Instead:
  - A device whose RSSI wobbles around a *stable mean* is stationary (normal).
  - A device whose mean is *trending upward over time* is **approaching** — interesting.
  - Use *relative* comparison ("stronger than most devices normally seen here") rather than
    absolute thresholds, since absolute values drift.

A device's score combines these deviation signals. The score answers "how much does this
depart from what's normal here," not "how persistent / how strong" in absolute terms.

### 5.5 Adaptation rate (and its security tradeoff)

The baseline must not be frozen forever — neighbors get new phones, devices change, the
environment legitimately evolves. A slow rolling update lets a device seen consistently over
time *become* baseline, preventing the sensor from crying wolf at every benign newcomer.

But this is the core security tradeoff of the whole model:

- **Fast adaptation** → fewer false alarms, but a *patient adversary* (a surveillance device
  that sits quietly for days) gets absorbed into "normal" and stops being flagged.
- **Slow adaptation** → catches patient adversaries, but more noise from benign change.

There is likely no single correct rate. The design exposes adaptation as an
operator-selectable posture (e.g. `FIXED_ADAPTATION=twitchy|balanced|stable`), and the doc
flags the default as needing field calibration (Section 8). The key property: a device that
becomes baseline should require *consistent* presence over the adaptation window, so that an
*intermittent* repeat-visitor (Section 6) does not silently get absorbed.

---

## 6. Threat patterns fixed mode should detect

Concrete patterns the fixed-mode model is designed to surface, framed for counter-surveillance:

- **Newcomer that stays.** A device never seen during baseline that appears and *persists*.
  Matches a surveillance device placed nearby, or a person conducting static observation:
  new identity, shows up, does not leave.
- **Repeat visitor.** A device that appears, leaves, and *returns* across days — especially
  at times correlated with the operator's own presence. The "are you being cased" pattern.
  Requires correlating across sessions/days, which is why persistence-over-time (not
  persistence-in-one-window) matters, and why adaptation must not absorb intermittent
  returners.
- **Approaching device.** A device whose signal mean trends upward over minutes — moving
  toward the operator's space rather than passing by.
- **Coordinated appearance.** Multiple novel devices appearing together, or a device whose
  signal implies it is *inside* the operator's space rather than street traffic.
- **(Future, multi-node) Follower that resolves to fixed.** A device seen on the operator's
  *mobile* node elsewhere now appearing at the *fixed* node — bridging the base/spoke
  architecture to catch a device that tailed the operator home. Requires multi-node
  correlation (out of scope for first implementation; noted as the powerful long-term case).

---

## 7. GUI framing by mode

Same capture pipeline, different lens. The GUI reads `NODE_MODE` and frames accordingly:

- **Fixed mode:** "Baseline: established (N devices) / learning (M min remaining)."
  "Monitoring for deviations." "3 anomalies flagged: 1 newcomer-persistent, 1 off-schedule,
  1 approaching." The map highlights *deviations from baseline*, not raw device lists.
- **Mobile mode:** "Tracking N devices across M locations." Highlights devices seen at
  multiple clusters. The existing framing.

The GUI must also clearly indicate **baseline-learning state** in fixed mode, so the operator
knows the node is still learning and (mostly) not yet alerting — with the exception that
egregious flags (5.2) still surface during learning.

---

## 8. Open questions / tuning defaults (need field calibration)

These are deliberately left as tunables with flagged defaults rather than hardcoded:

- **`FIXED_BASELINE_HOURS` default.** Too short bakes transients into "normal" (the daily
  mail carrier becomes environment); too long leaves the sensor learning-blind. A few days
  captures daily + weekday/weekend rhythm; under a day misses the weekly pattern. Default
  needs field calibration — a starting candidate is ~48–72h, but shorter may be acceptable
  for faster deployment if egregious-during-baseline (5.2) is trusted.
- **`FIXED_ADAPTATION` posture and rate.** The patient-adversary vs. false-alarm tradeoff
  (5.5). Default posture and the consistency-window that promotes a device to baseline both
  need calibration.
- **Signal-trend thresholds** (5.4) — what slope counts as "approaching," over what window,
  given RSSI noise. Needs empirical tuning against real captures.
- **Egregious triggers** (5.2) — the precise definition of "physically very close on first
  contact" (relative-signal percentile? absolute floor?) needs calibration.
- **Relocation jump threshold** (2.3) — how large a GPS move invalidates the baseline vs.
  normal GPS drift.

---

## 9. WiGLE enrichment (resident vs. visitor)

The self-learned baseline (Section 5) decides "normal here" purely from the node's own
observations over the learning period. **WiGLE** adds an external corroborating prior: a
global historical database of where WiFi/BT devices have been observed. Cross-referencing a
detected device against WiGLE answers a question the node cannot answer alone — *does this
device live here, or is it visiting?*

- **Resident** — WiGLE shows the device (BSSID) historically observed at or near this
  location → it genuinely belongs to the environment. Can be classified as baseline
  **immediately**, shortcutting the learning period for known-local devices.
- **Visitor** — WiGLE shows the device only at other locations, or shows no local history →
  it is not native here. A visitor that *lingers* or *persists* is precisely the fixed-node
  threat profile, and WiGLE corroboration raises its novelty signal from first contact.

### 9.1 Architectural rule: enrichment augments, never gates

WiGLE is a **prior, not an oracle**, and the design must treat it as strictly additive:

- WiGLE is historical and **incomplete**. A device absent from WiGLE may simply never have
  been wardriven — not necessarily new. A device present in WiGLE may have since moved away.
- Therefore WiGLE **enriches the score; it never gates detection.** Same rule as the alert
  backends: an external service is never a decision authority for the core function.
- If WiGLE is unreachable, rate-limited, or has no data on a device, the **self-learned
  baseline and all core detection continue to function fully.** The resilient core is the
  on-node pattern-of-life; WiGLE is the connected-mode enhancement layered on top.

### 9.2 Connectivity-adaptive: live vs. deferred enrichment

Enrichment availability is **runtime-detected, not pre-declared.** The node tests real
reachability to the WiGLE API and adapts:

- **Important hardware note:** the node's internet path is `wlan0` (onboard WiFi, on the
  operator's network) or ethernet — *not* `wlan1`, which is the monitor-mode capture adapter
  connected to nothing. The connectivity check must test actual reachability to WiGLE over
  the internet path, **not** "is a WiFi interface up." A field node can have `wlan1` happily
  monitoring while `wlan0` is associated with no network → no internet.

- **Reachable → live enrichment mode.** As priority devices are detected, the node queries
  WiGLE in real time, classifies resident/visitor immediately, and the GUI shows enriched
  data live. (Subject to the opsec toggle, 9.4, and rate limits, 9.3.)

- **Not reachable → deferred enrichment mode.** The node logs all raw detections with
  everything needed to enrich later (BSSIDs, timestamps, GPS, signal) and flags the session
  **pending-enrichment**. The operator enriches the session later from a connected machine
  (the natural post-wardrive / post-session review workflow). Resident/visitor
  classification is produced then.

This maps onto both deployment patterns:

- **Fixed sessions** — a wall-powered base station near the operator's network usually has
  internet → typically live enrichment. Deployed somewhere without internet → degrades to
  deferred, enriched on data collection.
- **Wardrive (mobile) sessions** — almost always offline in the field → logs raw, enriched
  post-wardrive at a connected machine. This is WiGLE's native workflow (collect in field,
  process/upload after).

The operator never pre-declares the mode; the node always tries, succeeds or fails
gracefully, and follows the result.

### 9.3 Rate limits, caching, and selectivity

WiGLE imposes API rate limits. Live-querying every detected device on a node seeing hundreds
of devices would exhaust quota immediately. Therefore even in live mode:

- **Selective**, not exhaustive — enrich novelty candidates and persistent devices, not every
  transient randomized probe.
- **Cached** — never re-query a BSSID already looked up this session (or across sessions, if
  a local enrichment cache is kept). Caching also serves the ICAO-style lookup-cache pattern
  already noted as high-value.
- Consequently, **deferred/batch enrichment is gentler on the rate limit** (it can be paced)
  and may be the *more thorough* mode. Live mode is best understood as "real-time flagging of
  the highest-priority candidates as quota allows"; deferred mode as "thorough enrichment of
  the full session, paced." Deferred is not merely the offline fallback — it is the better
  mode for completeness.

### 9.4 Shared WiGLE-operation queue

Both enrichment **queries** (this section) and detection-data **uploads** to WiGLE (existing
deferred high-value item) are "perform a WiGLE network operation when online, queue it when
not." They share infrastructure: a single connectivity-gated WiGLE-operation queue that
accumulates pending operations offline and flushes (paced, rate-limited) when connectivity
returns. Design the enrichment-query path and the upload retry queue together.

> **Codebase grounding — query path is net-new.** WiGLE today is **upload-only**:
> `modules/wigle.py` (`WiGLEUploader`) is a fire-once-at-session-end CSV upload, with no
> retry queue and no query capability. Implementing this section means **refactoring that
> fire-once uploader into the shared connectivity-gated queue** (it is not simply extended in
> place), and writing a **net-new WiGLE query/enrichment client** for the resident/visitor
> lookups. Scope phase 6 accordingly: queue + query client are new construction, the existing
> uploader is migrated onto the queue.

### 9.5 Opsec consideration

WiGLE queries are **outbound** — the node sends BSSIDs and location context to a third party
to ask about them. For a counter-surveillance tool this is a real consideration: the queries
themselves reveal what is being investigated and from where. This is acceptable for many
uses, but must be a **conscious, disable-able choice**: a config toggle to disable outbound
enrichment entirely for sensitive deployments (same family as the GUI-auth and
ntfy-topic-privacy gaps). When disabled, the node relies solely on the self-learned baseline.
Deferred enrichment from a *different* machine also distances the queries from the node's
location, which may be preferable for sensitive fixed deployments.

---

## 10. Watchboxes / origin-geofencing

Building on WiGLE's per-device geography (Section 9), a further capability: flag a device not
just on its behavior at the node, but on **where it has historically lived** relative to
known threat origins.

- **Device-origin geofencing.** If a detected device's WiGLE history clusters around a
  known-threat area (e.g. a location surveillance is known to originate from), then that
  device appearing near a node is high-signal — a device whose "home turf" is a threat origin
  now showing up near the operator. This can catch a device that tailed the operator from a
  known source *even if it has never been seen at this node before*, because the signal is the
  device's *origin*, not its local history.

- **Watchboxes** are operator-defined geographic zones associated with known threats. A device
  whose WiGLE-observed history falls within a watchbox raises its score when detected at any
  node. Watchboxes are configuration (operator-supplied threat geography), evaluated against
  the device-geography WiGLE provides.

This moves scoring from "is this device suspicious in isolation" to "is this device suspicious
*given where it has been*." It is genuinely advanced and strictly depends on WiGLE enrichment
(you need the device's historical geography to know its home), so it sits as a phase **beyond**
basic enrichment. It also inherits all of Section 9's rules: augments-never-gates,
connectivity-adaptive, opsec-conscious.

---

## 11. Implementation path (phased)

Built incrementally, not as one monolithic PR. Each phase ships something useful and is
independently validatable on real hardware.

1. **Mode selector + strategy split.** Introduce `NODE_MODE` (required, fail-loud-if-unset),
   the `ScoringEngine` interface, and wire the existing mobile model in as `MobileScoring`.
   No behavior change for mobile; fixed mode not yet implemented. Establishes the fork
   cleanly. Validate: mobile node behaves exactly as before; fixed mode declared but stubbed.
2. **Fixed mode: baseline + novelty (minimal useful cut).** Baseline learning period;
   per-device profiling (MAC + fingerprint keyed); the single highest-value deviation signal —
   *novelty* (device not in baseline). Plus the egregious-during-baseline safety net (5.2).
   This alone makes a fixed node useful (it flags new persistent devices) and directly
   resolves the #50 empty-GUI problem. Validate on `chase` against the real environment.
   **Hard requirement (5.1): the baseline persists to SQLite and survives restarts/reboots.**
   An in-memory baseline is disqualified — a restart (or a crash loop like 2026-06) would wipe
   it and the node would re-learn forever and never alert. The persistence engine's current
   in-memory 60-minute buffer is insufficient for this phase; profiles must be durable and
   reloaded on startup.
3. **Fixed mode: temporal + signal sophistication.** Off-schedule detection (time-of-day
   histograms), abnormal-dwell, signal-trend/approaching. Validate against real captures.
4. **Adaptation.** Rolling baseline update with the operator-selectable posture (default
   `balanced`); the consistency-window that prevents absorbing intermittent returners.
5. **GUI framing by mode** (Section 7) — can land alongside phase 2 (basic) and enrich
   through later phases.
6. **WiGLE enrichment (Section 9).** Connectivity-adaptive live/deferred resident-vs-visitor
   classification; the shared WiGLE-operation queue (with the upload path); rate-limit
   caching/selectivity; the opsec toggle. Augments the self-learned baseline; node remains
   fully functional offline. Depends on a working baseline (phase 2+).
   **Note (9.4): the existing fire-once `WiGLEUploader` is refactored onto the shared queue,
   and the query/enrichment client is net-new — scope this phase as new construction, not an
   extension of the current uploader.**
7. **Watchboxes / origin-geofencing (Section 10).** Operator-defined threat geography
   evaluated against WiGLE device-history. Depends on phase 6 enrichment.
8. **(Long-term) Multi-node correlation** — the follower-resolves-to-fixed pattern (Section
   6), bridging mobile and fixed nodes. Out of scope for initial implementation.

Each phase is a separate branch/PR through the single-tier model, CI-green, with real-hardware
validation on `chase` before merge (per the project's validation gate).

---

## 12. Summary

- Mode (`fixed` | `mobile`) is a required, explicit deployment choice that forks the scoring
  strategy. No silent default; declared mode is sanity-checked against GPS movement.
- One shared capture pipeline feeds one of two scoring engines (strategy pattern), forking at
  the existing `PersistenceEngine`.
- **Mobile** = location-diversity (does this follow me) — existing model, unchanged.
- **Fixed** = pattern-of-life (what deviates from baseline) — baseline learning period, then
  deviation scoring (novelty, off-schedule, abnormal dwell, approaching-signal), keyed on
  probe-fingerprint to survive MAC randomization, with slow operator-tunable adaptation
  (default `balanced`). **The baseline persists to SQLite and survives restarts.**
- Baseline learning still flags *egregious* threats, so a sensor deployed into an
  already-compromised environment does not bake the threat into "normal."
- Relocating a fixed node invalidates and rebuilds its baseline.
- **WiGLE enrichment** adds external resident-vs-visitor classification: connectivity-adaptive
  (live when online, deferred/batch when offline — runtime-detected on the internet path, not
  the monitor adapter), rate-limited and cached, opsec-toggle-able, sharing a queue with the
  upload path. It **augments, never gates** — the node is fully functional offline on the
  self-learned baseline alone. The current upload-only `WiGLEUploader` is refactored onto that
  queue; the query client is net-new.
- **Watchboxes / origin-geofencing** build on WiGLE device-geography to flag devices whose
  historical "home" ties them to known threat origins — a phase beyond basic enrichment.
- Built in phases; phase 2 (baseline + novelty) is the minimal cut that resolves #50 and makes
  a fixed node genuinely useful. Enrichment (phase 6) and watchboxes (phase 7) layer on top.
