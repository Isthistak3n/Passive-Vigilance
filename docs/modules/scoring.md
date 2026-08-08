# Scoring Engines

Two concrete engines share a common abstract interface. The orchestrator constructs exactly one of them at startup according to `NODE_MODE`.

## Overview

| Engine | Mode | Core idea |
|---|---|---|
| **PersistenceEngine** | mobile | Location-diversity / persistence scoring — devices that keep appearing as the node moves |
| **FixedScoring** | fixed | Baseline-deviation scoring — devices that deviate from the learned pattern of life |

Both implement `ScoringEngine` (`modules/scoring_engine.py`) and emit `DetectionEvent` objects.

## Design rationale

Mobile and fixed surveillance are different problems. A device that follows a moving observer is interesting; a device that is simply present in a static RF environment is not. The engines therefore share only the interface and the identity helpers; their internal models are deliberately distinct.

## ScoringEngine ABC

- `update(devices, *, gps_fix=None)` — ingest a poll; returns a list of `DetectionEvent`s.
- `status()` — current engine state (learning/frozen, counts) for the health banner and GUI.
- Never talks to the network, filesystem, or GPS directly; pure logic on the records handed to it.

## PersistenceEngine (mobile)

- Maintains per-identity observation clusters tagged with GPS positions.
- Scores devices that reappear across spatially separated locations (location diversity) or that persist for an unusually long time relative to the node’s own motion.
- Randomized MACs are keyed by strong content fingerprints when available; otherwise they remain MAC-keyed and can only score via proximity signals.

## FixedScoring (fixed)

- During the learning window (`FIXED_BASELINE_HOURS`) every device is upserted into BaselineStore. Novelty and off-schedule are not raised yet — but the *egregious-during-baseline* safety net still pages (via `force_page`) for a device in the operator's immediate space, so an already-present threat is not silently learned as normal.
- After freeze, a device is interesting if it is novel, off-schedule, or (Phase 2.5) approaching (RSSI rising relative to the frozen baseline mean).
- Rolling adaptation (P3) can later promote persistent post-freeze fingerprints into the baseline so the node does not keep flagging permanent new residents.
- Uses `BaselineStore.batch()` so thousands of upserts become a single commit and stay under the watchdog.

## DetectionEvent

Canonical alert / GUI / GIS payload (`modules/persistence.py`); both engines emit the same dataclass:

- `mac`, `fingerprint` (rotation-stable identity key), `fingerprint_label`, `mac_type`, `ssid`
- `score` and `score_breakdown` — the per-signal contributions (mobile: temporal / location / frequency / signal; fixed: novelty / off_schedule / approaching)
- `alert_level` — `suspicious` (0.5–0.7) / `likely` (0.7–0.9) / `high` (0.9+)
- `first_seen`, `last_seen`, `observation_count`, `manufacturer`, `device_type`
- `locations` — GPS cluster centroids at detection time (empty in fixed mode; no location gate)
- `force_page` — bypasses the paging bar for the egregious-during-baseline safety net

The *kind* of detection is read from `score_breakdown` / `alert_level`, not a separate "type" field.

## Pitfalls

- **Learning-window reset.** Wiping `data/baseline.db` is required when a fixed node is relocated; otherwise it carries the old site’s pattern of life.
- **Weak fingerprints.** A randomized device with only public SSIDs or a bare vendor ID stays MAC-keyed; proximity is the only remaining signal.
- **Batch discipline.** FixedScoring must call `upsert` inside `BaselineStore.batch()`; per-device commits at ambient density will trip the watchdog.

## Related modules

- [Durable Stores](stores.md) — BaselineStore is written only by FixedScoring; EntityStore is written by the orchestrator for both modes.
- [Identity](identity.md) — strong fingerprint supplies the scoring key.
- [SensorOrchestrator](orchestrator.md) — constructs and drives the engine.

## Hardware notes

| Node | Engine |
|---|---|
| **chase** | FixedScoring + BaselineStore |
| **survkis** | PersistenceEngine only |
