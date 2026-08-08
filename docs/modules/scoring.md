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

- `process_devices` / `update` — ingest a poll.
- Emits zero or more `DetectionEvent`s (type, score, reason, device snapshot, identity key).
- Never talks to the network, filesystem, or GPS directly; pure logic on the records handed to it.

## PersistenceEngine (mobile)

- Maintains per-identity observation clusters tagged with GPS positions.
- Scores devices that reappear across spatially separated locations (location diversity) or that persist for an unusually long time relative to the node’s own motion.
- Randomized MACs are keyed by strong content fingerprints when available; otherwise they remain MAC-keyed and can only score via proximity signals.

## FixedScoring (fixed)

- During the learning window (`FIXED_BASELINE_HOURS`) every device is upserted into BaselineStore; no alerts are raised for novelty.
- After freeze, a device is interesting if it is novel, off-schedule, or (Phase 2.5) approaching (RSSI rising relative to the frozen baseline mean).
- Rolling adaptation (P3) can later promote persistent post-freeze fingerprints into the baseline so the node does not keep flagging permanent new residents.
- Uses `BaselineStore.batch()` so thousands of upserts become a single commit and stay under the watchdog.

## DetectionEvent

Canonical alert / GUI / GIS payload:

- event type (novel, persistent, approaching, aircraft-of-interest, …)
- numeric score / confidence
- human reason string
- device snapshot + identity key
- GPS stamp at detection time

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
