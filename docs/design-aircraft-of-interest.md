# Design: aircraft of interest — and hooking ACARS into it

Status: **proposed** (design only — no code in this doc). Supersedes the
aspirational reference in `modules/air_scoring.py`.

## Why this exists

Passive Vigilance watches the air the same way it watches WiFi and Bluetooth:
most contacts are transits that "burn by" and mean nothing, and the job is to
surface the few that behave like they're paying attention to the node — an
aircraft that loiters, orbits, or keeps coming back. That engine already exists
as a pure scorer (`modules/air_scoring.py`) and is wired into the orchestrator
(`_score_aircraft`). This document records where that engine stands today and
lays out a phased plan to make **ACARS** — the aviation datalink PV now decodes —
feed it, unlocking two detections that ADS-B alone cannot produce.

## What already works

- **Geometry scorer** (`air_scoring.score_air_contact`): scores a track by
  closeness / dwell / orbit / return against the node's reference position,
  times an "interest multiplier," and crosses the same suspicious / likely /
  high tiers as the WiFi engine.
- **Orchestrator wiring** (`_score_aircraft`): runs the scorer on each aircraft
  event and stamps `air_score` / `air_severity` / `air_of_interest` /
  `air_breakdown`; the GUI already reflects `of_interest`.
- **Within-session returns** (`_note_aircraft_return`): a re-appearing airframe
  after a gap is tagged `returning` and bumps a `return_count`.
- **ACARS decode + correlation** (`_correlate_acars`): a decoded message is
  matched back to a live ADS-B contact by tail↔registration, then
  flight-id↔callsign, then nearest reported position. Matched messages enrich
  the contact's GUI row with route / flight-id / position.

## The gaps this plan closes

1. The interest multiplier is fed only two of its five flags (`military`,
   `no_callsign`). **`anonymous_callsign` — the blocked/privacy-address case,
   the most surveillance-relevant of all — is never set.**
2. Returns are counted **per ICAO and only within a session.** ICAO is the weak
   key for exactly the aircraft we care about (privacy addresses rotate; some
   spoof). There is no durable "this airframe keeps coming back over days."
3. ACARS correlation only ever *attaches* to a contact. When it finds **no**
   contact, that result is discarded — even though "heard on datalink but
   invisible on ADS-B" is itself a signal.

## The four enhancements (most valuable first)

### 1. De-anonymize privacy-ICAO aircraft
An aircraft can hide behind a blocked / LADD / PIA ICAO on ADS-B — which is
precisely what should raise interest. ACARS frequently carries the **real tail
and operator** in the message header even when ADS-B is anonymized. So an
aircraft that is obscured on ADS-B but whose identity we recover from ACARS is
doubly notable: it is deliberately hiding **and** we now know who it is. This is
the single highest-signal thing ACARS unlocks, and no other sensor provides a
second identity channel.

- Hook: set `InterestFlags.anonymous_callsign` in `_score_aircraft` from the
  ADS-B address class (blocked/LADD/PIA) or the enrichment military/anon flags.
- Hook: when a decoded ACARS message resolves a tail for an
  `anonymous_callsign` contact, tag the event (`identity_recovered`) and surface
  the recovered tail/operator on the row and in the alert reason.

### 2. Dark-aircraft detection (ACARS with no ADS-B track)
Some aircraft transmit VDL2/ACARS while their ADS-B is off or intermittent. An
ACARS message that carries an identity or position but matches **no** live
ADS-B contact is a contact PV would otherwise miss entirely.

- Hook: the existing `None` return from `_correlate_acars`. When a message that
  *has* a tail or a position fails to correlate, emit a distinct **ACARS-only
  contact** detection rather than dropping it.
- Guard against noise: require an identity or a position (not a bare uplink),
  and rate-limit per tail — VHF reception is opportunistic, so treat this as
  low-severity awareness, not a hard alert, until it proves out in the field.

### 3. Durable, tail-keyed returns
Give the "keeps coming back" signal a stable identity and a memory that outlives
a session.

- Hook: a durable per-airframe store mirroring `contact_registry` (an
  `air_registry` table in `entity_store`), keyed by **resolved tail when ACARS
  gives one, ICAO otherwise.** `_score_aircraft` reads `return_count` from it
  instead of the in-session counter. Keying on tail makes the signal much harder
  to evade for the privacy-address aircraft that matter most.

### 4. Refine the interest flags from both sources
Fill the remaining multiplier inputs and cross-confirm identity.

- `rotorcraft` from the ADS-B type / emitter category; `low_slow` from
  groundspeed (UAS / survey behavior).
- Use ACARS operator/type to cross-confirm ADS-B enrichment (e.g. confirm a
  government / law-enforcement operator by tail block) rather than trusting one
  source alone.

## Phased plan

Each phase is independently shippable, off by default where it changes
behavior, and lands with unit tests against synthetic tracks/messages (the
scorer and correlation are already pure, which keeps this cheap).

- **P1 — interest flags (no ACARS yet).** Add `anonymous_callsign`,
  `rotorcraft`, `low_slow` to `_score_aircraft` from ADS-B/enrichment fields.
  Pure change to flag population; immediately sharpens scoring. *Prereq for #1.*
- **P2 — de-anonymization.** On an ACARS tail resolution for an
  `anonymous_callsign` contact, set `identity_recovered`, surface tail/operator,
  add it to the alert reason. Builds on P1 + existing correlation.
- **P3 — durable tail-keyed returns.** Add the `air_registry` store; migrate
  `return_count` to read from it, keyed by tail-then-ICAO. Reuses the
  `contact_registry` pattern and its retention discipline.
- **P4 — dark-aircraft contacts.** Turn the `_correlate_acars` miss into an
  ACARS-only detection with the identity/position guard and per-tail
  rate-limit. Lowest-confidence, so last; gated by an env flag.

Ordering rationale: P1 is a prerequisite and valuable on its own; P2 delivers
the headline capability; P3 hardens the existing return signal; P4 is the most
speculative and rides on the reception proof-out.

## Guardrails

- **Metadata, not content.** Use ACARS for identity and presence — tail,
  operator, type, "was heard" — **never** the message body. Free-text / CPDLC
  can carry crew, dispatch, and personal data; the repo rules ("never upload raw
  log data; only summaries"; "never commit captured data") apply, and there are
  legal sensitivities to logging content. The raw `acars.jsonl` stays local and
  uncommitted like every other capture artifact.
- **Enrichment nudges, never dominates.** The interest multiplier stays capped
  (`AIR_INTEREST_CAP`) so identity flags can only amplify a real geometric
  presence — a hiding aircraft that never comes near the node still scores low.
- **No alert fatigue.** Returns and dark contacts surface on the dashboard;
  hard alert sends stay reserved for genuine loiter/orbit, per the existing
  `_note_aircraft_return` posture.
- **This is not a flight tracker.** The value is anomaly and surveillance
  detection; tar1090 / FR24 already display traffic.

## Testing

- P1: synthetic events with each flag combination assert the multiplier and
  tier crossings (extends `tests/test_air_scoring.py`).
- P2/P4: synthetic ACARS messages against a synthetic aircraft index assert
  identity-recovered tagging and the correlate-miss → dark-contact path
  (extends the ACARS correlation tests).
- P3: the `air_registry` store gets its own durability tests mirroring
  `contact_registry` (insert / update / cross-session return count).

## Open questions

- Which ADS-B address ranges/flags should map to `anonymous_callsign` — LADD
  and PIA and operator-blocked, or a subset? Needs a field survey of what
  actually appears overhead.
- Dark-aircraft severity and rate-limit defaults — pending a look at how many
  ACARS-only contacts a normal day produces from this site.
