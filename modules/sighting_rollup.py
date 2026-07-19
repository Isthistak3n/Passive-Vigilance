"""Nightly sighting rollup — fold aged observation rows into a bounded per-device
state table (design: docs/rollup-investigation.md, Phase 2).

The entity store's ``observations`` table is a full-resolution sighting log that
nothing reads at runtime; left alone it grows until it outruns the pruner and
fills the disk (2026-07-18, issue #211). This module gives every device ONE
durable ``device_state`` row — lifetime counters, hour-of-day and per-day
presence, running RSSI stats, distinct node-position clusters, and a
FIXED/MOBILE/UNKNOWN classification — and keeps ``observations`` as a rolling
N-day working set: before a row ages out of the window it is folded into its
device's state row, then deleted, in the same transaction.

Isolation contract: this NEVER runs on the poll path. ``run()`` is synchronous
and opens its OWN SQLite connection (WAL lets it coexist with the live writer;
short batched transactions + a busy timeout bound any lock contention), so the
orchestrator drives it from an executor thread on a nightly schedule. A rollup
failure must never affect capture — callers wrap it accordingly.

Retention interplay: the fold boundary (``ENTITY_SIGHTING_RETENTION_DAYS``,
default 7) must sit INSIDE the entity store's age window
(``ENTITY_OBSERVATION_RETENTION_DAYS``, default 30) — the nightly fold then
deletes everything past 7 days, and the store's own age sweep never finds
anything to delete unfolded. The hard row cap remains the emergency pressure
valve; rows it drops under storage stress are lost to the counters (lower
bounds), which is the store's documented lossy-under-stress behavior.
"""

import json
import logging
import math
import os
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

from modules.mac_utils import is_randomized_mac

logger = logging.getLogger(__name__)

# ── Classification thresholds (design §D) ──────────────────────────────────
# A beaconing AP is infrastructure: FIXED regardless of the numbers below.
FIXED_MIN_ACTIVE_HOURS = 20     # present in ≥ this many of the 24 hour buckets…
FIXED_MIN_DISTINCT_DAYS = 5     # …across ≥ this many distinct days ⇒ FIXED
FIXED_MAX_RSSI_STD = 8.0        # a parked emitter's RSSI is steady (dB); the
FIXED_RSSI_MIN_SAMPLES = 10     # check is skipped below this many samples
MOBILE_MIN_LOCATIONS = 2        # sighted from ≥2 distinct node positions ⇒ MOBILE
MOBILE_MIN_DISTINCT_DAYS = 2    # recurring but intermittent (few hour buckets)
MOBILE_MAX_ACTIVE_HOURS = 8     # ⇒ MOBILE on a fixed node
# Below this, stay UNKNOWN — the thin-evidence gate, mirroring the intent of
# OFF_SCHEDULE_MIN_BASELINE_HOURS.
MIN_EVIDENCE_SIGHTINGS = 10

# Per-day counter ring kept on the row. The distinct_days lifetime counter is
# NOT trimmed with the ring, so a >45-day returner may re-count a day — the
# counter is a close upper bound, documented.
DAY_COUNTS_KEEP = 45
LOCATION_CLUSTER_METERS = 100.0  # same 100 m rule mobile scoring uses
MAX_LOCATION_CLUSTERS = 50


def classify_node_type(*, is_ap: bool, total_sightings: int, active_hours: int,
                       distinct_days: int, distinct_locations: int,
                       rssi_n: int, rssi_std: float) -> str:
    """Pure FIXED/MOBILE/UNKNOWN call from a state row's aggregates."""
    if is_ap:
        return "fixed"
    if total_sightings < MIN_EVIDENCE_SIGHTINGS:
        return "unknown"
    if distinct_locations >= MOBILE_MIN_LOCATIONS:
        return "mobile"
    if active_hours >= FIXED_MIN_ACTIVE_HOURS and distinct_days >= FIXED_MIN_DISTINCT_DAYS:
        if rssi_n < FIXED_RSSI_MIN_SAMPLES or rssi_std <= FIXED_MAX_RSSI_STD:
            return "fixed"
    if distinct_days >= MOBILE_MIN_DISTINCT_DAYS and active_hours <= MOBILE_MAX_ACTIVE_HOURS:
        return "mobile"
    return "unknown"


def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def _parse_iso(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(s)
    except (TypeError, ValueError):
        return None


class SightingRollup:
    """Fold-and-prune job over an entity store DB file. Construct once; call
    ``run()`` from an executor thread (or directly in tests/migration)."""

    def __init__(self, db_path: str,
                 retention_days: Optional[int] = None,
                 batch_rows: Optional[int] = None,
                 time_budget_s: Optional[float] = None) -> None:
        self._db_path = db_path
        self._retention_days = max(0, int(
            os.getenv("ENTITY_SIGHTING_RETENTION_DAYS", "7")
            if retention_days is None else retention_days))
        self._batch_rows = max(1, int(
            os.getenv("ENTITY_ROLLUP_BATCH_ROWS", "5000")
            if batch_rows is None else batch_rows))
        self._budget_s = float(
            os.getenv("ENTITY_ROLLUP_TIME_BUDGET_S", "300")
            if time_budget_s is None else time_budget_s)

    # ------------------------------------------------------------------
    # Connection / schema
    # ------------------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        # Wait out the live writer's brief batched commits instead of erroring.
        conn.execute("PRAGMA busy_timeout=10000")
        self._ensure_schema(conn)
        return conn

    @staticmethod
    def _ensure_schema(conn: sqlite3.Connection) -> None:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS device_state (
                identity_key       TEXT PRIMARY KEY,
                entity_type        TEXT NOT NULL DEFAULT 'wifi',
                first_seen         TEXT NOT NULL,
                last_seen          TEXT NOT NULL,
                total_sightings    INTEGER NOT NULL DEFAULT 0,
                learning_member    INTEGER NOT NULL DEFAULT 0,
                node_type          TEXT NOT NULL DEFAULT 'unknown',
                distinct_sessions  INTEGER NOT NULL DEFAULT 0,
                distinct_days      INTEGER NOT NULL DEFAULT 0,
                distinct_locations INTEGER NOT NULL DEFAULT 0,
                hour_counts        TEXT NOT NULL DEFAULT '[]',
                day_counts         TEXT NOT NULL DEFAULT '{}',
                location_clusters  TEXT NOT NULL DEFAULT '[]',
                rssi_n             INTEGER NOT NULL DEFAULT 0,
                rssi_mean          REAL NOT NULL DEFAULT 0.0,
                rssi_m2            REAL NOT NULL DEFAULT 0.0,
                is_ap              INTEGER NOT NULL DEFAULT 0,
                mac_type           TEXT NOT NULL DEFAULT 'static',
                last_rollup_ts     TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS rollup_meta (
                id       INTEGER PRIMARY KEY CHECK (id = 1),
                last_run TEXT
            )
            """
        )
        conn.commit()

    def last_run(self) -> Optional[datetime]:
        """When the last rollup ran (None if never). Own short connection, safe
        from any thread."""
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT last_run FROM rollup_meta WHERE id = 1").fetchone()
            return _parse_iso(row["last_run"]) if row else None
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # The fold
    # ------------------------------------------------------------------

    def run(self, now: Optional[datetime] = None,
            learning_end: Optional[datetime] = None,
            fold_all: bool = False) -> dict:
        """Fold observations older than the retention window into device_state,
        deleting each folded batch in the same transaction (crash-safe: a kill
        between batches loses nothing and double-counts nothing). ``fold_all``
        folds everything up to ``now`` regardless of the window (migration).
        Returns a summary dict; never raises for per-row data problems."""
        now = now or datetime.now(timezone.utc)
        cutoff = _iso(now if fold_all else now - timedelta(days=self._retention_days))
        deadline = time.monotonic() + self._budget_s
        conn = self._connect()
        folded = 0
        identities: set = set()
        exhausted = False
        try:
            while True:
                batch = conn.execute(
                    """
                    SELECT o.rowid AS rid, o.timestamp, o.lat, o.lon, o.signal,
                           e.entity_type, e.identifier,
                           e.first_seen AS entity_first_seen,
                           df.probe_fingerprint
                    FROM observations o
                    JOIN entities e ON e.entity_id = o.entity_id
                    LEFT JOIN device_fingerprint df ON df.mac = e.identifier
                    WHERE o.timestamp < ?
                    ORDER BY o.rowid
                    LIMIT ?
                    """,
                    (cutoff, self._batch_rows),
                ).fetchall()
                if not batch:
                    exhausted = True
                    break
                self._fold_batch(conn, batch, learning_end)
                conn.execute(
                    "DELETE FROM observations WHERE rowid IN (%s)"
                    % ",".join("?" * len(batch)),
                    [r["rid"] for r in batch],
                )
                conn.commit()
                folded += len(batch)
                identities.update(self._identity_key(r) for r in batch)
                if time.monotonic() >= deadline:
                    logger.info(
                        "Sighting rollup: time budget (%.0fs) hit after %d row(s) — "
                        "remaining backlog resumes next run", self._budget_s, folded)
                    break
            conn.execute(
                "INSERT INTO rollup_meta (id, last_run) VALUES (1, ?) "
                "ON CONFLICT(id) DO UPDATE SET last_run = excluded.last_run",
                (_iso(now),),
            )
            conn.commit()
        finally:
            conn.close()
        return {"folded_rows": folded, "identities": len(identities),
                "exhausted": exhausted}

    @staticmethod
    def _identity_key(row) -> str:
        """Rotation-stable where possible: devices with a stable IE fingerprint
        share one state row across MAC rotations (same keying idea FixedScoring
        uses); everything else keys by MAC."""
        fp = row["probe_fingerprint"]
        return f"fp:{fp}" if fp else f"mac:{row['identifier']}"

    def _fold_batch(self, conn: sqlite3.Connection, batch: list,
                    learning_end: Optional[datetime]) -> None:
        # Aggregate the batch per identity first; one read-merge-write per
        # identity per batch, not per row.
        agg: dict = {}
        for r in batch:
            key = self._identity_key(r)
            a = agg.setdefault(key, {
                "entity_type": r["entity_type"], "macs": set(),
                "first": r["timestamp"], "last": r["timestamp"], "n": 0,
                "hours": [0] * 24, "days": {}, "rssi": [],
                "positions": [], "entity_first": r["entity_first_seen"],
            })
            a["macs"].add(r["identifier"])
            a["n"] += 1
            ts = r["timestamp"]
            a["first"] = min(a["first"], ts)
            a["last"] = max(a["last"], ts)
            a["entity_first"] = min(a["entity_first"], r["entity_first_seen"])
            dt = _parse_iso(ts)
            if dt is not None:
                a["hours"][dt.hour] += 1
                day = dt.date().isoformat()
                a["days"][day] = a["days"].get(day, 0) + 1
            sig = r["signal"]
            if sig is not None and sig != 0:   # 0 is Kismet's placeholder
                a["rssi"].append(float(sig))
            if r["lat"] is not None and r["lon"] is not None:
                a["positions"].append((r["lat"], r["lon"]))

        for key, a in agg.items():
            self._merge_state_row(conn, key, a, learning_end)

    def _merge_state_row(self, conn: sqlite3.Connection, key: str, a: dict,
                         learning_end: Optional[datetime]) -> None:
        row = conn.execute(
            "SELECT * FROM device_state WHERE identity_key = ?", (key,)).fetchone()

        if row:
            first_seen = min(row["first_seen"], a["first"], a["entity_first"])
            last_seen = max(row["last_seen"], a["last"])
            total = row["total_sightings"] + a["n"]
            hours = json.loads(row["hour_counts"]) or [0] * 24
            days = json.loads(row["day_counts"])
            clusters = [tuple(c) for c in json.loads(row["location_clusters"])]
            distinct_days = row["distinct_days"]
            distinct_sessions = row["distinct_sessions"]
            n0, mean0, m2_0 = row["rssi_n"], row["rssi_mean"], row["rssi_m2"]
            learning = bool(row["learning_member"])       # sticky once set
        else:
            first_seen = min(a["first"], a["entity_first"])
            last_seen = a["last"]
            total = a["n"]
            hours, days, clusters = [0] * 24, {}, []
            distinct_days = distinct_sessions = 0
            n0, mean0, m2_0 = 0, 0.0, 0.0
            learning = False

        for h in range(24):
            hours[h] += a["hours"][h]
        for day, n in a["days"].items():
            if day not in days:
                distinct_days += 1
            days[day] = days.get(day, 0) + n
        if len(days) > DAY_COUNTS_KEEP:                   # ring-trim, keep newest
            for day in sorted(days)[:-DAY_COUNTS_KEEP]:
                del days[day]

        # Welford merge of the batch's RSSI samples into the running triple.
        nb = len(a["rssi"])
        if nb:
            mean_b = sum(a["rssi"]) / nb
            m2_b = sum((x - mean_b) ** 2 for x in a["rssi"])
            n = n0 + nb
            delta = mean_b - mean0
            mean = mean0 + delta * nb / n
            m2 = m2_0 + m2_b + delta * delta * n0 * nb / n
            n0, mean0, m2_0 = n, mean, m2

        for lat, lon in a["positions"]:
            for i, (clat, clon) in enumerate(clusters):
                if _haversine_m(lat, lon, clat, clon) <= LOCATION_CLUSTER_METERS:
                    break
            else:
                if len(clusters) < MAX_LOCATION_CLUSTERS:
                    clusters.append((round(lat, 5), round(lon, 5)))

        if not learning and learning_end is not None:
            fs = _parse_iso(first_seen)
            if fs is not None and fs <= learning_end:
                learning = True

        # A device is an AP if it has ever beaconed (beacon_evidence is upserted
        # per beaconing AP); any of the identity's MACs qualifies it.
        is_ap = any(
            conn.execute(
                "SELECT 1 FROM beacon_evidence WHERE bssid = ? LIMIT 1",
                (mac,)).fetchone()
            for mac in a["macs"])
        if row:
            is_ap = is_ap or bool(row["is_ap"])
        mac_type = ("randomized"
                    if all(is_randomized_mac(m) for m in a["macs"]) else "static")

        # Opportunistic session count: contact_registry keys 'mac:'-form
        # identities identically, so those rows get real cross-session visit
        # counts; 'fp:' keys keep whatever they have (0 until a better source).
        reg = conn.execute(
            "SELECT visits, distinct_days FROM contact_registry WHERE identity_key = ?",
            (key,)).fetchone()
        if reg:
            distinct_sessions = max(distinct_sessions, reg["visits"])
            distinct_days = max(distinct_days, reg["distinct_days"])

        std = math.sqrt(m2_0 / n0) if n0 > 0 else 0.0
        node_type = classify_node_type(
            is_ap=is_ap, total_sightings=total,
            active_hours=sum(1 for h in hours if h > 0),
            distinct_days=distinct_days,
            distinct_locations=len(clusters),
            rssi_n=n0, rssi_std=std)

        conn.execute(
            """
            INSERT OR REPLACE INTO device_state
            (identity_key, entity_type, first_seen, last_seen, total_sightings,
             learning_member, node_type, distinct_sessions, distinct_days,
             distinct_locations, hour_counts, day_counts, location_clusters,
             rssi_n, rssi_mean, rssi_m2, is_ap, mac_type, last_rollup_ts)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (key, a["entity_type"], first_seen, last_seen, total,
             int(learning), node_type, distinct_sessions, distinct_days,
             len(clusters), json.dumps(hours), json.dumps(days),
             json.dumps([list(c) for c in clusters]),
             n0, mean0, m2_0, int(is_ap), mac_type, a["last"]),
        )
