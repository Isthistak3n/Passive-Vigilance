# Design: Aircraft of Interest — orbit/loiter detection from ADS-B + Remote ID

**Status:** Design note (2026-06). Companion to
[roadmap-fixed-node-prototype.md](roadmap-fixed-node-prototype.md) (phase P7) and
[design-entity-fingerprinting.md](design-entity-fingerprinting.md) (the WiFi/BT
returning-entity work this mirrors).
**Audience:** Implementers, reviewers, operators.

> **Prereq progress (2026-06-14).** P6's core shipped: the aircraft panel now serves
> the live current sky from the per-ICAO index with recency decay on the map and a
> persistent detection log in the table (`current_aircraft()` / `AIRCRAFT_RETENTION_SECONDS`),
> and the sensor chiclets read true runtime state. Still owed before P7: the **Remote
> ID surface** (`/api/remote_id` + tab) — where the loitering-UAS signal must appear —
> plus bounded per-aircraft tracks and returning-ICAO track-gap identity.

---

## The problem

Aircraft are currently **display-and-enrichment only** — the node decodes ADS-B,
enriches it, draws it on the map, and stops. Nothing scores it. But the
counter-surveillance question for the air picture is real: *is something watching
from above, and has it watched before?* The operator asked for a returning-aircraft
signal analogous to the WiFi/BT work. This note defines what that signal actually
is and how to build it without rebuilding the false-positive flood the WiFi soak
taught us to fear.

## The one distinction that matters: transit vs. orbit

Almost everything in the sky here is **transit** — aircraft flying *by*. Airliners
on approach and departure, inter-island hops, and tourist helicopters tracing the
same coastline all pass through and leave. This is the overwhelming common case and
it is benign.

The behavior of interest is the opposite: an aircraft **orbiting or loitering in
the immediate area** — a helicopter flying circle patterns overhead, an aircraft
holding a slow racetrack within visual range of the node. As of this writing a
contact is doing exactly that nearby. We happen to know it is a training
event — **but the code must never assume that.** The detector flags the *behavior*;
a known-benign orbiter is suppressed by the baseline and the operator whitelist,
**not** by the code guessing "probably training." Assuming benignity is how a
counter-surveillance sensor goes blind.

So the detector's entire job is to separate *transit* from *orbit-near-node*. Every
threshold below serves that one discrimination.

## Why this is easier than the WiFi version (on identity)

MAC randomization made WiFi identity the hard part — hence the fingerprinting work
in the entity-resolution note. Aircraft have no such problem: the **ICAO 24-bit
address is a globally unique, non-rotating airframe identity**, usually accompanied
by a callsign and (via enrichment) a registration. The returning-entity problem
that dominates the WiFi design essentially vanishes here — the ICAO *is* the durable
key. The hard parts are **geometry** and **baseline discipline**, not identity.

## What we already have to build on

- A **fixed node with a GPS position** — so we can compute range and bearing from
  the operator's location to every aircraft, and detect orbits centered on *us*.
- **Per-aircraft tracks** — the orchestrator already keeps one record per ICAO and
  extends a thinned position track in place.
- **Enrichment** — registration, operator, and a military flag via adsb.lol.
- **Remote ID** — the node already parses ASTM F3411 drone broadcasts.
- A **rate limiter** and per-session **aircraft log** already in place.

## The signal

An **aircraft of interest** is one that, against the node's reference position:

1. stays within a **horizontal radius R**, and
2. below an **altitude ceiling C** (3-D slant range, so a high airliner directly
   overhead does not count as "close"), and
3. for a sustained **dwell T**, and
4. with an **orbit signature** — cumulative heading change beyond a threshold, or a
   low-groundspeed hold — rather than a straight transit.

A **returning aircraft of interest** is the same ICAO satisfying that across
separate sessions or days — the genuine "has cased me before" signal.

### First-cut thresholds (deliberately conservative, tuned against real data)

| Parameter | Default | Why |
|---|---|---|
| Radius **R** | 5 nm | "Immediate area," not the whole receiver range |
| Ceiling **C** | 5,000 ft | Excludes overflight cruise traffic |
| Dwell **T** | 8 min | A racetrack/orbit, not a single pass |
| Orbit heading change | > 270° | A near-full circle separates orbit from fly-by |

All configurable. The orbit-geometry test is what actually rejects the common case:
a landing/departing aircraft or a coastline tour heli accumulates little heading
change as it passes; a circling aircraft wraps past 270° and keeps going.

### Reference position — GPS by default, GUI override

Default to the **live GPS position** (smoothed/median, since a fixed node's fix
jitters). Provide a **GUI control to pin a manual home position**, which takes over
when set — important for GPS degradation, indoor siting, or a known-good survey
point. Persist the choice. All range/bearing math keys off this single reference.

## Baseline pattern-of-life — the flood guard

Bare "I have seen this airframe orbit before" would flood: tour helicopters orbit
the same landmarks daily, flight schools fly patterns, medevac and  return constantly. 
This is the exact alert-fatigue trap the first WiFi soak sprang.

So aircraft of interest get the **same baseline-then-flag discipline** as the fixed
WiFi scoring:

- Learn the node's normal aircraft pattern-of-life — which ICAOs/callsigns loiter
  in range, at what hours.
- A **durable cross-session store keyed by ICAO** records that history.
- A **novel** airframe that orbits the immediate area is the signal; a baselined
  daily orbiter is absorbed as normal.
- An **egregious-during-learning** carve-out still fires for the unmistakable case
  (very close, very low, sustained orbit) even before the baseline is established —
  mirroring the WiFi egregious safety net, so the node is not blind on day one.

### Interest weighting

Beyond the geometry, raise confidence for: a **blocked/anonymous** callsign (LADD /
PIA), the **military** flag, **no callsign at all**, and **rotorcraft** type. These
are enrichment fields we already fetch.

## Remote ID belongs here too

A **loitering small UAS** is arguably the highest-value aircraft of interest — a
drone holding station over a property is a far more direct surveillance indicator
than any crewed aircraft. The node already parses Remote ID, which carries operator
and drone positions directly. The orbit/loiter logic should be **modality-agnostic**
— fed by ADS-B *and* Remote ID against the same reference position and thresholds.

## Data-model and capture requirements (do these before scoring)

These are prerequisites surfaced while diagnosing the aircraft panel (roadmap P6),
and several are endurance lessons from P0 applied to the air picture:

- **One event per ICAO, locations updated in place** — never a new detection per
  sighting. This is already the server's intent; the GUI must honor it on the
  refresh/seed path too (P6).
- **Bound the track** — an orbiting aircraft grows its position list without limit
  (and the whole list ships to the GUI on every push). Cap by age or count.
- **Expire from the index** — drop an aircraft after a staleness timeout so the live
  index does not grow across a multi-day run; the GUI recency-decay (shrink → grey →
  remove) reads the same last-seen age.
- **Do not merge ID-less contacts** — collapsing every aircraft without an ICAO into
  one "unknown" bucket fuses distinct airframes; skip or key them separately.
- **Reappearance is the same identity with a marked gap** — a returning ICAO
  continues its airframe history; do not spawn a fresh event or draw a track line
  across the absence.

## Honest limits

- **Intermittent reception.** The receiver catches roughly one target at a time and
  has directional blind spots (poor to the south here). A real orbit will produce a
  *gappy* track, so dwell and heading-change must accumulate across gaps within a
  window, not require a continuous track. Loiters in a blind sector are simply
  unobservable — a siting limitation, not a software one.
- **Corridors.** Standard approach/departure paths produce repeated transits through
  the same airspace; the orbit-geometry requirement is what suppresses them, and the
  baseline absorbs the rest.
- **Distant landmark tours.** Helicopters that orbit a landmark *near but not over*
  the node may clip the radius; the baseline absorbs the regulars, and the signal is
  the airframe that shifts its orbit onto *the operator's* location.
- **No intent.** The node reports geometry and history, not purpose. "Novel airframe
  orbited your location at 02:00, and again the next night" is the strongest thing it
  can honestly say — and that is a genuinely useful thing to be told.

## Phasing

1. **P6 (near-term):** the GUI/data-model fixes — one-event-per-ICAO on refresh,
   bounded tracks, expiry, recency-decay, the "unknown" bucket fix. Self-contained,
   independent of scoring.
2. **Reference position + geometry:** node-relative range/bearing, the GPS-default /
   GUI-override home, and the live orbit-vs-transit classifier (no alerting yet —
   surface "currently orbiting" on the GUI first, validate against real traffic).
3. **Baseline + scoring:** the durable per-ICAO history, novelty/returning/off-hours
   scoring, egregious-during-learning carve-out, interest weighting, and alerting
   through the existing rate limiter.
4. **Remote ID fusion:** feed UAS loiter through the same logic.

Like the WiFi path, the first long run is an endurance-and-correctness test, not a
usability one — the baseline is what eventually makes the alerts livable.
