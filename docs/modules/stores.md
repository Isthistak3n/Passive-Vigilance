# Durable Stores (EntityStore & BaselineStore)

Passive Vigilance keeps two independent SQLite stores that survive restarts, crash loops and multi-day soaks. They serve different purposes and are deliberately kept orthogonal to scoring.

## Overview

| Store | File | Purpose | Written by | Affects scoring? |
|---|---|---|---|---|
| **EntityStore** | `data/entities.db` | Probe evidence, fingerprints, observation history, contact registry, person links, beacon affinity | Orchestrator on every poll (both modes) | No |
| **BaselineStore** | `data/baseline.db` | Fixed-mode pattern-of-life baseline (hour masks, RSSI stats, promotion state) | FixedScoring only | Yes (the baseline itself) |

Both use WAL mode + `synchronous=NORMAL` and careful batching so they never stall the asyncio poll loop on SD-card storage.

## Design rationale

Two independent failure modes drove the design:

1. **Crash-loop safety for fixed mode.** An in-memory baseline is destroyed by any restart. During the 2026-06 incident the service restarted ~60 times in 70 minutes; a resetting learning window would have left the node in permanent “learning” and never alerting. The BaselineStore therefore persists the learning-start timestamp on first init and never recomputes it.
2. **Unbounded growth on SD cards.** Early observation history grew until a WAL-backed read stalled the poll loop past the systemd watchdog. EntityStore therefore enforces three independent bounds: age retention, hard row cap, and periodic WAL TRUNCATE checkpoints. An optional off-loop writer can further isolate slow fsyncs from the event loop.

Recording into EntityStore is orthogonal to scoring: the same poll data is written for both `fixed` and `mobile` nodes. A store failure is always guarded and never affects capture or detection.

## EntityStore (`modules/entity_store.py`)

### Tables

| Table | Keying | Growth | Purpose |
|---|---|---|---|
| `probe_evidence` | (mac, ssid) | Upsert | Per-MAC named probe SSIDs |
| `pnl_evidence` | (probe_fingerprint, ssid) | Upsert | Preferred-network list under the rotation-stable IE hash |
| `device_fingerprint` | mac | Upsert | Latest probe fingerprint + count |
| `entities` | (type, identifier) | Upsert | Logical entity row + obs_count |
| `observations` | auto | Insert (bounded) | Full history with GPS stamp |
| `beacon_evidence` | (bssid, ssid) | Upsert | Local AP beacons + running RSSI stats |
| `network_affinity` | (probe_fingerprint, ssid) | Upsert | Probed SSIDs that are also beaconed locally |
| `contact_registry` | identity_key | Upsert | Cross-session returning-entity memory |
| `contact_links` | (key_a, key_b) | Upsert | Durable person links from co-presence |
| `contact_designator` | identity_key | Upsert | Stable CLASS-IDENT-# instance numbers |

### Critical correctness properties

- Every per-device write except the observation history is a real `INSERT … ON CONFLICT DO UPDATE`. Row counts for a stable device set level off.
- Observation history is the only growing table; it is bounded by age *and* a hard row cap (`ENTITY_OBSERVATION_MAX_ROWS`, default 4 M).
- WAL is periodically TRUNCATE-checkpointed so it cannot grow unbounded on a busy writer.
- An optional async writer (`ENTITY_ASYNC_WRITES`) moves the commit off the asyncio thread; a full queue drops the poll rather than blocking capture.

### Key public methods

- `record_poll(devices, gps_fix=…)` — one call per Kismet poll.
- `distinctive_anchors(max_df=…)` — map IE-hash → rarest distinctive SSID (used by the orchestrator for scoring/display anchors).
- `accumulated_pnl(probe_fingerprint)` / `network_affinity_profile(…)` — rotation-surviving network lists.
- `record_contact_sighting` / `record_contact_link` — cross-session identity.
- `assign_contact_number` — stable designator instance numbers.
- `prune_observations` / `checkpoint_wal` / `storage_stats` — maintenance and health-banner data.
- `close()` — drains the writer (if any) and truncates the WAL.

## BaselineStore (`modules/baseline_store.py`)

### Core invariant

> The learning-window **start** timestamp is written once on first init and is **never** recomputed from “now” on a later open. A restart resumes the existing window.

`baseline_hours` itself may be refreshed so an operator can retune `FIXED_BASELINE_HOURS`, but the start time is immutable.

### DeviceProfile

Each profile carries:

- Identity / recency (`key`, `first_seen`, `last_seen`, `observation_count`, manufacturer, type, mac_type).
- Baseline behavioural stats (hour-of-day mask, RSSI mean/variance) — accumulated **only** while learning.
- Post-freeze recent-signal EMA (for the approaching trigger).
- Rolling-adaptation provenance (`promoted`, `promotion_ts`).

### Key methods

- `upsert(key, now, …, accumulate_baseline=…)` — the only write path; called once per device per poll by FixedScoring.
- `batch()` context manager — coalesces thousands of upserts into a single commit (required at high ambient density to stay under the watchdog).
- `is_learning(now)` / `freeze_time` / `learning_start`.
- `promotion_candidates` / `promote` / `demote` / `promoted_profiles` — rolling adaptation surface.
- `baseline_count` / `profile_count` / `promoted_count` — status.

Zero RSSI readings are treated exactly like missing readings and never folded into any statistic.

## Configuration

| Variable | Store | Default | Notes |
|---|---|---|---|
| `ENTITY_OBSERVATION_RETENTION_DAYS` | Entity | 30 | 0 = keep forever |
| `ENTITY_OBSERVATION_MAX_ROWS` | Entity | 4 000 000 | Hard cap |
| `ENTITY_PRUNE_INTERVAL_SECONDS` | Entity | 3600 | |
| `ENTITY_WAL_CHECKPOINT_SECONDS` | Entity | 300 | |
| `ENTITY_ASYNC_WRITES` | Entity | false | Experimental |
| `ENTITY_AUDIBLE_WINDOW_SECONDS` | Entity | 0 | Filter silent devices before write |
| `BASELINE_DB_PATH` | Baseline | `data/baseline.db` | |
| `FIXED_BASELINE_HOURS` | Baseline | 72 | |
| `APPROACHING_RECENT_EMA_ALPHA` | Baseline | 0.3 | Post-freeze EMA weight |

High-density fixed nodes should use the tighter profile documented in `.env.example`.

## Pitfalls and edge cases

- **Relocating a fixed node.** Wipe `data/baseline.db` so the node learns the new environment from scratch; otherwise it carries the old site’s pattern of life.
- **SD-card fsync latency.** Even with WAL the absolute ceiling is the underlying storage. A USB SSD is the durable fix for the largest soaks.
- **Import-time env leakage.** Both stores read their defaults inside `__init__`, not at module level, so pytest collection order cannot bake in stale values.
- **Audible window.** On a fixed node Kismet’s device list is cumulative for the session. Without `ENTITY_AUDIBLE_WINDOW_SECONDS` a device heard once keeps generating observation rows forever.
- **Batch depth.** `BaselineStore.batch()` must be entered on the writer (poll) thread only; the depth counter is deliberately unlocked so GUI reads can interleave.

## Related modules

- `modules/fixed_scoring.py` — sole writer of BaselineStore.
- `modules/orchestrator.py` — sole writer of EntityStore (via `record_poll`).
- `modules/device_identity.py` / fingerprint helpers — supply the keys that both stores use.
- `modules/sighting_rollup.py` — optional nightly fold of aged observations.

## Hardware notes

| Node | Store behaviour of interest |
|---|---|
| **Fixed node** | Both stores active. Baseline learns for 72 h then freezes; EntityStore records every poll and feeds distinctive anchors + contact registry. High ambient density requires the tighter row-cap / prune profile. |
| **Mobile node** | EntityStore still records (orthogonal). BaselineStore is not opened because FixedScoring is not constructed. |

## See also

- [Scoring Engines](scoring.md)
- [SensorOrchestrator](orchestrator.md)
- [docs/design-and-roadmap.md](../design-and-roadmap.md) — original durability and growth requirements.
