# Design: Aircraft of Interest — persistence scoring for the air picture

**Status:** Design note (rev. 2026-06-16). Companion to
[roadmap-fixed-node-prototype.md](roadmap-fixed-node-prototype.md) (phase P7),
[design-detection-modes.md](design-detection-modes.md) (the mobile persistence
engine this mirrors), and
[design-entity-fingerprinting.md](design-entity-fingerprinting.md) (the WiFi/BT
returning-entity work).
**Audience:** Implementers, reviewers, operators.

> **Rev. 2026-06-16.** Replaces the original hard-threshold model (inside R *and*
> below C *and* dwell >T *and* heading >270° → flag) with a **persistence score**,
> after the operator observation that the air question is the same one the *mobile*
> node already answers — "is this thing staying with me, or just passing through?"
> The thresholds are not discarded; they become the **normalizers** that turn
> geometry into continuous score features instead of cliff-edge gates.

> **Prereqs (done, P6, 2026-06-16):** per-ICAO current-sky index with recency decay
> and a 24 h detection log; bounded tracks; ID-less split; returning-ICAO recognised
> as the same identity with a marked track gap and a GUI of-interest flag; the Remote
> ID surface (`/api/remote_id` + tab). The returning-flag is **GUI-only** today — it
> becomes an *alert* only behind the score gate below, or it floods on every regular
> returning airliner.

---

## The problem

Aircraft and drones are **display-and-enrichment only** today, with one crude
exception: the alert path fires on *every* detection (per-ICAO / per-band / per-UAS,
rate-limited), so a transiting airliner pages the operator exactly like a threat.
That is the alert-fatigue trap the first WiFi soak taught us to fear. The
counter-surveillance question for the air picture is sharper than "is a plane
there": *is something watching from above, and has it watched before?*

## The one distinction that matters: transit vs. orbit/return

Almost everything overhead is **transit** — airliners on approach/departure,
inter-island hops, coastline tour helicopters tracing the same path. It passes
through and leaves. Benign, and the overwhelming common case.

The behaviour of interest is the opposite: an aircraft **orbiting/loitering in the
immediate area**, or **one that keeps coming back**. We flag the *behaviour* — a
known-benign daily orbiter is suppressed by the **baseline** and the operator
whitelist, **never** by the code guessing "probably training." Assuming benignity is
how a counter-surveillance sensor goes blind.

## Why a persistence score, not thresholds

The mobile node already scores exactly this shape of question for WiFi/BT: *is this
device persistently with me as I move?* It does so with a windowed, weighted
persistence model (`modules/persistence.py`): temporal persistence, location
diversity, frequency, and signal proximity → a 0–1 score → `suspicious / likely /
high` tiers, with a minimum-observation guard so a single fleeting sighting never
scores.

The air picture is the same question with the node held still. So we **reuse the
model and swap the features.** A burn-by accumulates almost nothing; an orbiter
racks up dwell + turning; a frequent returner climbs across days. Continuous and
tunable — a plane just outside one threshold still contributes instead of vanishing
off a cliff.

### Feature mapping (mobile → air)

| Mobile (device, node moves) | Air (aircraft/UAS, node fixed) | Note |
|---|---|---|
| Temporal persistence across 5/10/15/20-min windows | **Dwell** — sustained presence in range | direct |
| **Location diversity** — seen at many of *my* locations | **Proximity + orbit geometry** — close, circling *my one* location | **inverts** (node is fixed) |
| Frequency — how often seen | **Return frequency** — distinct visits/days, keyed by ICAO | long-horizon |
| Signal proximity (RSSI −85→−40) | **Closeness** — 3-D slant range + altitude | direct, different units |

The feature that does **not** transfer is mobile's strongest one — location
diversity — because it needs the *node* to move. The fixed air picture drops it and
reweights onto dwell + orbit-geometry + return-frequency. (So: do **not** copy
mobile's 35 % location weight.)

## The model

Reuse the mobile engine's math and shape; feed air features on **two timescales**
(the one genuinely new piece — mobile is short-horizon only):

- **Short horizon (minutes):** dwell + orbit signature + closeness, from the live
  per-ICAO track → *"is it loitering right now?"* Catches the live orbiter.
- **Long horizon (hours/days):** return count + distinct-day spread, from a durable
  ICAO-keyed store → *"has it come back, and how often?"* Catches the patient
  watcher. This is where "returning often raises alarm" lives.

Each feature normalises to 0–1 against a configurable parameter (the old thresholds
become these normalizers), a weighted sum yields the score, and the score crosses
the same `suspicious / likely / high` tiers. A **minimum-observation guard** (≥2
in-range sightings before any non-zero score) *is* the "don't alert on a burn-by"
rule, for free.

### Features and first-cut parameters

| Feature | Normaliser (param, default) | Why |
|---|---|---|
| Closeness | slant range vs **AIR_RADIUS_NM = 5**; altitude vs **AIR_CEILING_FT = 5000** | "immediate area," not whole receiver range; 3-D so a high overflight is far |
| Dwell | in-range time span vs **AIR_DWELL_TARGET_S = 480** (8 min) | a racetrack/orbit, not a single pass |
| Orbit | cumulative heading change vs **AIR_HEADING_TARGET_DEG = 270**; low groundspeed boosts | a near-full circle separates orbit from fly-by |
| Return | distinct visits/days vs **AIR_RETURN_TARGET = 3** | the "cased me before" signal |

First-cut **weights** (location-diversity removed, reweighted): dwell 0.30, orbit
0.30, closeness 0.20, return 0.20 — all `AIR_WEIGHT_*` env-tunable. **Interest
multiplier** (×, capped ~1.3) for: blocked/anonymous callsign (LADD/PIA), the
military flag, no callsign at all, rotorcraft type, and (UAS) low-and-slow. Tiers:
**suspicious ≥ 0.5, likely ≥ 0.7, high ≥ 0.9** (mirrors mobile).

All defaults are deliberately conservative and **tuned against real traffic** — the
first long run is an endurance-and-correctness test, not a usability one.

### Reference position

All range/bearing/heading math keys off one reference: the **live GPS fix**
(median-smoothed, since a fixed node's fix jitters) by default, with a **GUI-pinned
home override** (persisted) that wins when set — for GPS degradation, indoor siting,
or a survey point.

## Baseline pattern-of-life — the flood guard

A bare score would still flood: tour helicopters orbit the same landmarks daily,
flight schools fly patterns, medevac returns constantly. So the score gets the
**same baseline-then-flag discipline** as fixed WiFi scoring:

- Learn the node's normal air pattern-of-life — which ICAOs/callsigns loiter or
  recur in range, at what hours — in a **durable ICAO-keyed store**.
- A **novel** airframe that scores high is the signal; a baselined daily orbiter is
  absorbed as normal-for-here (its score is expected, so suppressed).
- An **egregious-during-learning** carve-out still fires for the unmistakable live
  case (very close, very low, sustained orbit) before the baseline exists — so the
  node is not blind on day one, mirroring the WiFi egregious net.

## Modality-agnostic: Remote ID and RF ride the same path

A **loitering small UAS** is the highest-value contact — a drone holding station
over a property is a more direct surveillance indicator than any crewed aircraft.
The scorer is fed by **ADS-B, Remote ID, and (coarsely) drone-RF persistence**
against the same reference and parameters. Remote ID carries operator and drone
positions directly; RF has no position, so it contributes only the dwell/return
axes (a band heard persistently / recurring), never closeness/orbit.

## Architecture

Aircraft/drones are **not** on the `NODE_MODE` device-scoring fork (that scores the
WiFi/BT stream). This is a **separate scorer instance** in the orchestrator, fed at
the `_poll_adsb` / `_poll_remote_id` / `_poll_drone_rf` sites, producing a score
that drives (a) the alert decision through the existing rate limiter and (b) the GUI
severity. Orthogonal to the device `ScoringEngine`; modality-agnostic across the
three feeds.

- `modules/air_geometry.py` (pure): reference resolution, 3-D slant range, bearing,
  cumulative heading change (gap-tolerant), groundspeed from a track.
- `modules/air_scoring.py` (pure): the feature extraction + weighted score + tiers +
  min-obs guard; takes a track, reference, return history, and interest flags →
  score/severity/breakdown. No I/O — unit-tested against synthetic tracks.
- Durable per-ICAO air baseline + return history — extends `BaselineStore` or a
  parallel store (decision below).
- Orchestrator wiring + GUI severity + alert gating come after the pure core is
  validated against real-traffic replay.

## Honest limits (unchanged)

- **Gappy reception.** The receiver catches ~one target at a time with directional
  blind spots, so a real orbit produces a *gappy* track — dwell and heading change
  must accumulate **across gaps within a window**, never require a continuous track.
  Loiters in a blind sector are unobservable: a siting limit, not a software one.
- **Corridors / landmark tours.** Repeated transits and near-but-not-over orbits are
  suppressed by the orbit-geometry feature and absorbed by the baseline; the signal
  is the airframe that shifts its orbit onto *the operator's* location.
- **No intent.** The node reports geometry and history, not purpose. "Novel airframe
  orbited your location at 02:00, and again the next night" is the strongest honest
  statement — and a genuinely useful one.

## Open decisions (first-cut answers in brackets)

1. **Weights** with location-diversity gone. [dwell .30 / orbit .30 / closeness .20
   / return .20, env-tunable]
2. **Two-horizon interaction** — does a live loiter alert alone? [egregious live
   orbit alerts immediately; everything else waits for baseline]
3. **"Return" granularity** [distinct visits separated by the P6 gap threshold AND
   distinct UTC days]
4. **Baseline storage** [extend `BaselineStore` with an ICAO-keyed air table]

## Phasing (build order)

1. **Geometry + scoring core (pure, this PR):** `air_geometry.py` + `air_scoring.py`
   + off-hardware synthetic-track tests. No orchestrator/alert wiring; the long-
   horizon return feature is an input parameter so the core stays pure.
2. **Reference position + live classifier:** GPS-smoothed/GUI-override home; wire the
   short-horizon score into `_poll_adsb`; surface "currently orbiting / score" on the
   GUI — **no alerting yet**, validate against real traffic.
3. **Baseline + scoring + alerting:** durable ICAO-keyed history, novelty/returning,
   egregious-during-learning carve-out, interest weighting, alert through the rate
   limiter (replacing today's blanket per-detection alerts). Drone-RF persistence
   gate rides here.
4. **Remote ID fusion:** feed UAS loiter through the same path; the dashboard surface
   is the P6 Remote ID tab.
