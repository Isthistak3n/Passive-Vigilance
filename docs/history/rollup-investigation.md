# Sighting Rollup — Investigation & Proposed Design

Branch: `experiment/sighting-rollup` · Written 2026-07-18 · Status: **PROPOSAL — no code changes**

Motivation: the SD card on the chase node hit 100% full on 2026-07-18 (27 GB
entities.db WAL; ~18 h of silent persistence failures). The per-sighting event-log
model grows without bound at fixed-node device density. This doc records exactly how
sightings are persisted today (Phase 1) and proposes a bounded state-table +
pruned-sightings model (Phase 2).

---

## Phase 1 — How PV persists the Kismet feed today

### 1. Storage layer

PV does **not** read Kismet's `.kismet` SQLite logs. Kismet is polled over its REST
API (`modules/kismet.py`), and PV persists what it needs into **its own SQLite
databases** (Kismet's native logging is disabled on the node —
`enable_logging=false` in `kismet_site.conf` — precisely because its unbounded
`.kismet` files caused an earlier failure).

Four distinct persistence surfaces:

| Store | File | Owner | Growth |
|---|---|---|---|
| Entity/observation store | `data/entities.db` | `modules/entity_store.py` (`EntityStore`) | **UNBOUNDED in practice** (see §2) |
| Fixed-mode baseline | `data/baseline.db` | `modules/baseline_store.py` (`BaselineStore`) | Bounded (one row per device) |
| Session event logs | `data/sessions/<session_id>/*.jsonl` (+ summary.json, shp/kml at shutdown) | orchestrator / GUI history | Bounded per session; sessions accumulate (785 session dirs currently) |
| Scoring engine state | in-memory only (`PersistenceEngine` windows / `FixedScoring` profiles backed by baseline.db) | `modules/persistence.py`, `modules/fixed_scoring.py` | Bounded |

`entities.db` schema (`entity_store.py:240` `_create_schema`):

- `entities` — **one row per device** (`entity_type`, `identifier` = MAC,
  `first_seen`, `last_seen`, `obs_count`). Upserted; flat for a stable device set.
- `observations` — **one row per device per poll**: `entity_id`, `timestamp`,
  `lat`, `lon`, `pos_source`, `pos_confidence`, `signal`. **This is the unbounded
  event log** and the only table that grows per poll (the code says so explicitly
  at `entity_store.py:532`).
- Eight more tables, all bounded upserts keyed per device / per (device, SSID)
  pair: `probe_evidence`, `pnl_evidence`, `device_fingerprint`, `beacon_evidence`
  (with running Welford RSSI stats), `network_affinity`, `contact_designator`,
  `contact_registry`, `contact_links`.

`baseline.db` schema (`baseline_store.py:192` `device_profiles`): one row per
device key — `first_seen`, `last_seen`, `observation_count`, `manufacturer`,
`device_type`, `mac_type`, `hour_mask` (24-bit hours-of-day mask), `signal_mean`,
`signal_var`, plus promotion provenance. Already a state table in miniature.

### 2. Where the per-sighting write happens, and why it is unbounded

Write site: `SensorOrchestrator._poll_kismet` → `entity_store.record_poll(devices,
gps_fix=…)` (`orchestrator.py:1216`), every Kismet poll
(`KISMET_POLL_INTERVAL_SECONDS=30` on chase). `record_poll` →
`_write_poll` (`entity_store.py:429`) inserts **one `observations` row per device
in the poll list**, all in one commit (via a writer thread when
`ENTITY_ASYNC_WRITES=true`, as on chase).

The multiplier that makes it explode: **Kismet's device list is cumulative for the
session**, and on fixed nodes `KISMET_ACTIVE_WINDOW_SECONDS` is deliberately 0
(unset on chase) so the full historical list feeds baseline learning. So a device
heard once at 09:00 keeps generating a fresh "sighting" row every 30 seconds for
the rest of the session, even though it is long gone. Row creation is therefore
O(cumulative devices × polls), not O(actual sightings):

- chase health banner 2026-07-18: **15.0 M cumulative device records in 13.8 h**
  ≈ 12.5 k devices per poll × 2 polls/min ≈ **36 M observation rows/day attempted**.
- The prune (age retention + 250 k hard row cap, #206/#212) deletes at most one
  25 k batch per sweep under a 3 s SD-fsync budget (~700 k rows/h ceiling), so at
  daytime density ingest permanently outruns it. That losing race is what produced
  3.4 M rows and the 27 GB WAL on 2026-07-18.

**Key finding — `observations` is write-only at runtime.** The only code that reads
it is the pruner itself and `storage_stats()` (health banner). No scoring path, no
GUI path, no analysis queries it. The node pays its entire write/WAL/prune cost for
a table nothing consumes. (The per-entity index `idx_obs_entity` has no live query
using it.) This makes the rollup proposal low-risk: the raw rows have no runtime
reader to break.

The GUI's inflated "seen" counter (hundreds of thousands per contact) is the same
cumulative-list artifact, but via a different path: it is `observation_count` from
the scoring engine's in-memory profile (incremented once per poll the device
appears in the list — i.e. once per poll for the whole session on a fixed node),
attached to the event dict. It is not read from `observations`.

### 3. Per-sighting context available for FIXED vs MOBILE classification

Captured today, per observation row:

- **RSSI** — `signal` (caveat: Kismet reports `0` as a placeholder for ~15–18 % of
  readings; must be treated as missing, as `baseline_store` already does).
- **Position** — `lat`/`lon` are the **node's own GPS fix** (`pos_source =
  'gps_node'`), stamped identically onto every device that poll. On a fixed node
  this is a constant and carries zero per-device information; on a mobile node it
  gives real location diversity. There is no per-device geolocation.

Captured today, per device (in the poll record or aggregate tables, not per
sighting):

- **AP vs client** — `is_ap` / `beaconed_ssid` / `type` / `phyname` from Kismet
  (`kismet.py:256-286`); beaconing APs additionally accrue running RSSI
  mean/variance in `beacon_evidence`.
- **Randomized-MAC indicators** — `mac_type` / `is_randomized`
  (`mac_utils`), plus the rotation-stable IE-hash `probe_fingerprint` that already
  keys `pnl_evidence` / `contact_registry` across MAC rotations.
- **Session/day recurrence** — `contact_registry` already tracks, per
  rotation-stable identity: `visits`, `distinct_days`, `last_session`,
  `first_seen`/`last_seen`. This is exactly the "distinct sessions" signal the
  state table wants, and it already exists.
- **Hour-of-day schedule + RSSI stats** — `baseline.db device_profiles` has
  `hour_mask`, `signal_mean`, `signal_var` per device key (fixed mode).

Conclusion: everything needed to classify FIXED vs MOBILE is already captured, and
most of it is **already aggregated**; the raw `observations` rows add essentially
only full-resolution timestamps (and per-poll RSSI samples beyond the running
stats).

### 4. Does the GUI read sighting rows?

**No.** `gui/server.py` has zero references to `EntityStore` and opens no SQLite
connection to `entities.db`. Its data sources are (a) in-memory `_recent_*` caches
pushed by the orchestrator, and (b) the per-session JSONL logs
(`_history("events.jsonl", …)` etc., rebuilt for `/api/wifi`, `/api/aircraft`,
`/api/ais`, `/api/acars`, `/api/alerts`, `/api/drone`). The per-contact "seen"
figure in `app.js` (`e.observation_count`, `app.js:414,547`) rides the event dict
from the scoring engine. **A schema change to `observations` requires no GUI
changes.** (If we later want the GUI's "seen" to be honest, it should read the new
state row's total — a small, separate improvement.)

---

## Phase 2 — Proposed design: bounded state table + pruned sightings

### Design principles

- The durable record of "what do we know about this device" becomes **one state
  row per device**, updated by upsert (flat storage for a flat population — the
  pattern every bounded table in entities.db already follows).
- Full-resolution sighting rows are kept only for a **rolling N-day window** as a
  working set (for future features, debugging, and rollup input), then folded into
  the state row and deleted.
- Nothing runs on the poll path that isn't already there; the rollup job runs
  off-loop, batched, under a time budget (per the standing rule: no blocking work
  on the live loop).

### A. `device_state` table (new, in entities.db)

One row per rotation-stable identity (the contact-identity key where one exists,
`mac:<mac>` otherwise — same keying rule FixedScoring already uses):

| Column | Meaning |
|---|---|
| `identity_key` (PK) | Rotation-stable key (`wifi-fp:`/`ble-fp:`/`mac:` form) |
| `entity_type` | wifi / bt / ble |
| `first_seen`, `last_seen` | ISO timestamps |
| `total_sightings` | Lifetime count, incremented by rollup (plus live tail) |
| `learning_member` | 1 if first seen inside the baseline learning window (a "known fixture" candidate) |
| `node_type` | `fixed` \| `mobile` \| `unknown` (classifier output, §D) |
| `distinct_sessions`, `distinct_days` | Recurrence counters (seed: `contact_registry`) |
| `distinct_locations` | Count of ≥100 m-separated node-position clusters this device was sighted from (meaningful on mobile nodes / recon-pair; stays 1 on a fixed node) |
| `hour_counts` | 24 small integers (JSON or 24 cols) — per-hour-of-day presence totals; supersedes the lossy 24-bit `hour_mask` |
| `day_counts` | Coarse per-day presence for the last ~30 days (JSON ring) — "seen 22 of last 30 days" |
| `rssi_n`, `rssi_mean`, `rssi_m2` | Welford running RSSI stats (zero/None skipped) — same scheme `beacon_evidence` uses today |
| `is_ap`, `mac_type`, `manufacturer` | Classification inputs, denormalized |
| `last_rollup_ts` | High-water mark of folded-in sightings (idempotency cursor) |

Size estimate: ~40 k entities × ~200 bytes ≈ **8 MB**, flat — versus 27 GB of WAL
for data nothing read.

### B. `sightings` table (successor to `observations`)

Same shape as today's `observations` (full timestamp resolution, RSSI, node
position) with `identity_key` added, retained for
`SIGHTING_RETENTION_DAYS` (default **7**, configurable; 0 = forever for
research nodes on real disks). Two changes to the *write* side:

1. **Only write a sighting for devices actually heard this poll** — i.e. devices
   whose Kismet `last_time` falls within the poll interval. The full cumulative
   list still flows to scoring/baseline untouched (that filter is applied at the
   entity-store write site, not in `KismetModule`), but absent devices stop
   generating phantom rows. This cuts steady-state ingest from ~12.5 k rows/poll
   to roughly the currently-audible population (hundreds), an ~10–50× write
   reduction, and makes `total_sightings` mean "times actually heard."
2. Batch-dedup guard stays as-is (cap + age prune machinery already exists and
   becomes easily sufficient at the reduced rate).

### C. Nightly prune+rollup job

Runs once per day in the quiet hours (and opportunistically at startup if overdue),
on the existing writer thread / an executor, in batches under a wall-clock budget —
never on the poll loop:

1. Select sightings with `timestamp < now − N days` **and**
   `timestamp > state.last_rollup_ts`, in batches (reusing the batched-DELETE
   pattern from `prune_observations`).
2. Per identity: increment `total_sightings`; fold timestamps into `hour_counts`
   and `day_counts`; fold RSSI into the Welford triple; update
   `distinct_locations` by clustering node positions with the same 100 m
   haversine rule `PersistenceEngine` uses; bump `distinct_sessions`/
   `distinct_days` from session boundaries; advance `last_rollup_ts`.
3. Delete the folded batch; WAL-checkpoint between batches (machinery exists).
4. Crash-safety: rollup and delete for a batch commit together; `last_rollup_ts`
   makes re-runs idempotent (a re-run folds nothing twice, a crash between
   batches loses nothing).

### D. FIXED vs MOBILE classification (from signals PV actually has)

Recomputed cheaply at rollup time per state row:

- **FIXED** — any of: `is_ap` with a beaconed SSID (infrastructure); or presence
  in ≥ ~20 h of the 24 `hour_counts` buckets across ≥ ~5 distinct days with low
  RSSI variance (a static emitter parked near the node: printer, TV, neighbor's
  IoT). On a fixed node this is the "always here" population.
- **MOBILE** — intermittent presence (few hour buckets per day, gaps between
  days), higher RSSI variance, typically a randomized-MAC client; on a mobile
  node (or via recon-pair surveys), `distinct_locations ≥ 2` is the direct
  signal — the same location-diversity rule mobile scoring already uses.
- **UNKNOWN** — until `total_sightings` and day coverage cross minimum-evidence
  thresholds (mirrors the existing thin-baseline gate:
  `OFF_SCHEDULE_MIN_BASELINE_HOURS`).

Known limitation, stated honestly: on a *fixed* node all sightings share the
node's position, so "mobile across locations" is only observable via multi-day /
multi-session recurrence (`contact_registry`), cross-node survey findings
(`survey_store`), or a future mobile node sharing state — not from the fixed
node's own GPS column.

**Baseline / learning-period suppression.** `learning_member=1` (first seen inside
the FixedScoring learning window, which `baseline.db` already anchors durably)
marks known fixtures; classified-FIXED learning members are the suppressed
furniture. Post-baseline, the two alerting populations the user cares about fall
out of the state row directly:

- **(a) New persistent fixed node** — `learning_member=0`, classified FIXED (or
  trending: rising day-coverage + hour-coverage since `first_seen`). "A camera
  went up across the street."
- **(b) Recurring mobile node** — classified MOBILE with `distinct_sessions`/
  `distinct_days` above threshold (fixed node), or `distinct_locations ≥ 2`
  (mobile node) — the returning-entity signal `contact_registry` +
  `_note_cross_session_return` already surface, now with a durable home.

Scoring engines are untouched: this is persistence-layer state. FixedScoring /
PersistenceEngine keep emitting events exactly as today; a later (separate) change
can let alerts consult `node_type`/`learning_member` for suppression.

### E. Migration / backfill sketch

One-time, offline (node stopped or on a copy — same discipline as the 07-11
archive-and-recreate):

1. Build `device_state` rows by aggregating existing tables — `entities`
   (first/last/obs_count), `contact_registry` (sessions/days), `beacon_evidence`
   (AP RSSI), `baseline.db device_profiles` (hour_mask seeds `hour_counts` as
   0/1, RSSI stats seed the Welford triple), and whatever `observations` rows
   still exist (`GROUP BY entity_id`: counts, min/max, strftime-hour histogram).
   The current live table is only ~250 k rows post-recovery, so this is seconds,
   not hours.
2. Trim `observations`/`sightings` to the last N days.
3. `VACUUM` into a fresh file (also finally adopts `auto_vacuum=INCREMENTAL`,
   closing the #191 dead-no-op noted earlier).
4. Keep the pre-migration file aside as `entities.db.pre-rollup-<date>` until the
   new shape has soaked (the established snapshot convention).

Idempotent and abortable: the state table is derived entirely from surviving
sources; a failed run can be rerun from scratch.

### Interaction with existing safety nets

The 250 k row cap, batched pruner, WAL checkpoint, `pv-wal-watch` cron, and
`pv-async-watch` all stay. With sightings written only for audible devices and a
nightly fold, steady-state ingest drops below the pruner's ~700 k rows/h ceiling
by an order of magnitude, so the cap becomes a true backstop instead of a losing
race. The SD-fsync ceiling (#211) remains real; a USB SSD is still the durable
substrate fix and this design reduces, not removes, that dependency.

### Open questions for review

1. Retention default: 7 days proposed (the 30-day age window never engaged before
   the cap anyway). Shorter (3 d) fine on SD; longer once on SSD.
2. Should the sighting-write filter (audible-only) ship first as its own small PR?
   It is the single biggest lever (~10–50× write cut), independent of the schema
   work, and immediately relieves #211 pressure.
3. Key by rotation-stable identity (proposed) vs raw MAC: identity keys make
   randomized-MAC mobile devices one row, but mean the state table keys differ
   from `entities.identifier` (MAC). Proposal keeps both: state rows keyed by
   identity, `entities` untouched.
4. GUI "seen" honesty fix (read `total_sightings` from state) — in-scope here or
   separate?
