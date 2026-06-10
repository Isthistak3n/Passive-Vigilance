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
capturing via the USB dongle. Built but **not merged**: the approaching-signal
trigger (Phase 2.5) — it still owes a positive walk-test.

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
| **P0** | Endurance hardening (post-freeze memory + disk) | ☐ Not started | **Yes** |
| **P1** | Approaching trigger merged + walk-tested (Phase 2.5) | ☐ Built, unmerged | No (recommended) |
| **P2** | Egregious-during-baseline safety net (§5.2) | ☐ Not started | Strongly recommended |
| **P3** | Adaptation — rolling baseline (§5.5) | ☐ Not started | No |
| **P4** | Cross-session entity resolution (Phase F) | ☐ Not started | No |
| **P5** | Fixed-mode GUI framing + ADS-B panel fix | ☐ Not started | No |

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
entity" as a signal. (Recon showed Bluetooth will NOT strengthen this here — it's
sparse and the stable subset is appliances — so this leans on WiFi probe
fingerprints, not BT.)

**Tests.** Off-hardware: two MAC-rotated sightings sharing a probe fingerprint
resolve to one entity; distinct devices don't merge. On chase: a known device
re-identifies across a restart and a day boundary.

**Exit gate.** Cross-session re-identification works on known devices.

### P5 — Fixed-mode GUI framing + ADS-B panel fix

**Why.** The GUI got the mode toggle but still shows a raw device list, not the
fixed-node lens; and there is a known bug where the dashboard's aircraft panel
does not show ADS-B that readsb has.

**Scope.** Show baseline state (learning vs. frozen, time remaining), the anomaly
list framed by signal / severity, and returning entities; fix the aircraft panel
(null-position aircraft are a starting hypothesis for the missing-aircraft bug).

**Tests.** Panels populate; the aircraft panel matches what the ADS-B module
logged.

**Exit gate.** An operator can read node state and anomalies at a glance.

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

## Sequencing

P0 → (P1, P2 in parallel) → **first multi-day soak** → P3 → P4 → P5, iterating the
soak after P3 once adaptation is in. The first long soak should be read as an
endurance-and-correctness test, not a usability one, until P3 lands.

## Deliberately deferred (per the design doc)

GPS-movement sanity check (§2.2) and relocation re-baseline (§2.3) — low value for
a stationary base station; WiGLE resident-vs-visitor enrichment (§9); watchboxes /
origin-geofencing (§10); multi-node correlation (§11.8). All genuinely later.

## Standing risks

- Treating the 4h memory pass as sufficient and finding the post-freeze leak only
  during the multi-day run — P0 plus the forced-freeze test exists to retire this.
- Alert fatigue without P3 — **CONFIRMED in soak #1**: post-freeze novelty floods
  (~969/poll, ~60% randomized MACs). Mitigated by the sustained-presence guard for
  fingerprint-less randomized MACs; P3 (rolling baseline) is the durable fix.
- Every recent PR is blocked on **commit signing** (verified-signatures ruleset) —
  resolve so these merges don't stall at the gate.
