"""survey_store — durable SQLite backing the reconnaissance-node survey workflow.

Two nodes cooperate as a hunter/recon pair (docs/design-and-roadmap.md §5.5):

  * The **fixed** node flags a suspicious device and issues a *survey tasking* that
    names it by its rotation-stable content identity key (``wifi-fp:`` / ``ble-fp:``
    from :mod:`modules.device_identity`) — the one identifier that a *different*
    node can recognise, because it is derived from advertised content, not the MAC.
  * The **mobile** node pulls open taskings, and while it roams records a tagged
    ``survey_observation`` every poll it sees a tasked target. Back at base it
    clusters those observations into *findings* — where the target beds down
    (long dwell, repeat visits, overnight presence = residence) — and pushes them
    back to the fixed node.

The same class runs on both nodes; which tables carry data depends on the role.
Like :class:`modules.baseline_store.BaselineStore` the connection is opened on the
asyncio thread but read from the Flask GUI thread, so every method is serialised on
a reentrant lock paired with ``check_same_thread=False``. All writes are guarded by
the callers — a store failure must never touch capture or detection.
"""
from __future__ import annotations

import functools
import json
import logging
import os
import sqlite3
import threading
import uuid
from collections import Counter
from datetime import datetime, timedelta, timezone, tzinfo
from math import atan2, cos, radians, sin, sqrt
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# Default DB path derived from this file (modules/ -> repo root), so it resolves
# identically under systemd or by hand; lives under the gitignored data/ dir.
_DEFAULT_DB_PATH = str(Path(__file__).resolve().parent.parent / "data" / "survey.db")

# Spatial clustering threshold (metres): observations within this radius are one
# location. Matches PersistenceEngine.cluster_locations' 100 m default.
_CLUSTER_THRESHOLD_M = float(os.getenv("SURVEY_CLUSTER_METERS", "100"))

# A gap longer than this (seconds) between two in-cluster sightings ends a "visit"
# and is NOT counted toward dwell — so a blind spot in reception never inflates the
# time-present figure (the same gap-tolerance discipline as the air-scoring heading
# accumulation). Default 10 min bridges the normal poll cadence and short occlusions.
_VISIT_GAP_S = float(os.getenv("SURVEY_VISIT_GAP_SECONDS", "600"))

# Observation-history retention (mobile side); 0/negative = keep forever.
_RETENTION_DAYS = int(os.getenv("SURVEY_OBS_RETENTION_DAYS", "30"))

# "Overnight" local-time window, "HH-HH" (start inclusive, end exclusive, wraps
# midnight when start > end). Used to flag a bed-down cluster as an overnight stay.
_NIGHT_HOURS = os.getenv("SURVEY_NIGHT_HOURS", "22-06")

_OPEN_STATUSES = ("open", "surveying")


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def _parse_iso(s) -> Optional[datetime]:
    """Parse a stored UTC ISO timestamp, tolerant of a naive string; None on failure."""
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s)
    except (ValueError, TypeError):
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def _haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in metres — same formula as PersistenceEngine.haversine."""
    R = 6_371_000.0
    phi1, phi2 = radians(lat1), radians(lat2)
    dphi = radians(lat2 - lat1)
    dlambda = radians(lon2 - lon1)
    a = sin(dphi / 2) ** 2 + cos(phi1) * cos(phi2) * sin(dlambda / 2) ** 2
    return R * 2 * atan2(sqrt(a), sqrt(1.0 - a))


def _parse_night_hours(spec: str) -> "tuple[int, int]":
    """Parse a ``"HH-HH"`` night window into ``(start_hour, end_hour)``; fall back to
    22-06 on any malformed input so a bad env value never crashes finding computation."""
    try:
        start_s, end_s = spec.split("-", 1)
        start, end = int(start_s), int(end_s)
        if 0 <= start <= 23 and 0 <= end <= 23:
            return start, end
    except (ValueError, AttributeError):
        pass
    return 22, 6


def _is_night_hour(hour: int, start: int, end: int) -> bool:
    """True if *hour* falls in the night window, handling the midnight wrap
    (start > end, e.g. 22..06 spans 22,23,0..5)."""
    if start <= end:
        return start <= hour < end
    return hour >= start or hour < end


def _synchronized(method):
    """Serialise a SurveyStore method on the instance's reentrant lock — the
    connection is written on the asyncio thread and read from the Flask GUI thread
    (paired with ``check_same_thread=False``); reentrant so a guarded method may
    call another on the same thread without deadlocking."""
    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)
    return wrapper


class SurveyStore:
    """SQLite-backed store for survey taskings, observations, and findings.

    Args:
        db_path:        SQLite file path. Defaults to ``data/survey.db``.
        retention_days: Observation-history retention window; 0/negative = forever.
    """

    def __init__(
        self,
        db_path: Optional[str] = None,
        retention_days: Optional[int] = None,
    ) -> None:
        self._db_path = db_path or _DEFAULT_DB_PATH
        self._retention_days = (
            _RETENTION_DAYS if retention_days is None else int(retention_days)
        )
        if self._db_path != ":memory:":
            Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self._db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._apply_pragmas()
        self._create_schema()

    def _apply_pragmas(self) -> None:
        """WAL + synchronous=NORMAL keep commits off the per-write fsync path on the
        Pi's SD card; busy_timeout rides out a transient lock. Each pragma is guarded
        (a no-op on an in-memory DB). Matches the baseline/entity store hardening."""
        for pragma in (
            "PRAGMA journal_mode=WAL",
            "PRAGMA synchronous=NORMAL",
            "PRAGMA busy_timeout=5000",
        ):
            try:
                self._conn.execute(pragma)
            except sqlite3.Error as exc:  # pragma: no cover - defensive
                logger.debug("SurveyStore pragma failed (%s): %s", pragma, exc)

    # ------------------------------------------------------------------
    # Schema
    # ------------------------------------------------------------------

    def _create_schema(self) -> None:
        cur = self._conn.cursor()
        # The watchlist. identity_key is the portable content key; evidence is a JSON
        # blob of the fingerprint material (probe_fingerprint, fp_anchor, probed
        # SSIDs, BLE advert fields, vendor, type) so the mobile node can re-derive and
        # confirm the key against a device it observes.
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS survey_tasking (
                task_id      TEXT PRIMARY KEY,
                identity_key TEXT NOT NULL,
                designator   TEXT,
                reason       TEXT,
                evidence     TEXT,
                created_at   TEXT NOT NULL,
                origin_node  TEXT,
                status       TEXT NOT NULL DEFAULT 'open',
                -- Survey outcome once the mobile node reports: 'resident' (home AP found
                -- locally), 'seen' (device seen but no local home AP), 'not_located'
                -- (patrolled, found nothing). wigle_candidate is set when the home AP was
                -- NOT found in the local wardrive, so the operator may deliberately run a
                -- WiGLE lookup (the query itself is a separate, manual step — not here).
                outcome         TEXT,
                wigle_candidate INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_tasking_identity ON survey_tasking(identity_key)"
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_tasking_status ON survey_tasking(status)"
        )
        # Mobile-side raw hits — one row per poll a tasked target is seen. The only
        # table that grows on the mobile node; bounded by the retention sweep.
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS survey_observation (
                obs_id    INTEGER PRIMARY KEY,
                task_id   TEXT NOT NULL,
                timestamp TEXT NOT NULL,
                lat       REAL,
                lon       REAL,
                rssi      REAL,
                -- 'device' = the tasked device itself was seen here; 'ap' = a local AP
                -- beaconing the device's distinctive home network was heard here (its
                -- BSSID/SSID recorded) — the single-patrol bed-down signal.
                kind      TEXT NOT NULL DEFAULT 'device',
                bssid     TEXT,
                ssid      TEXT
            )
            """
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_survobs_task ON survey_observation(task_id, timestamp)"
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_survobs_ts ON survey_observation(timestamp)"
        )
        # Computed bed-down clusters — written mobile-side by compute_findings and
        # received fixed-side by ingest_findings. Ranked (rank 0 = the headline
        # bed-down). One task's findings are replaced wholesale on recompute/ingest.
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS survey_finding (
                finding_id      INTEGER PRIMARY KEY,
                task_id         TEXT NOT NULL,
                rank            INTEGER NOT NULL DEFAULT 0,
                cluster_lat     REAL,
                cluster_lon     REAL,
                dwell_seconds   REAL,
                visit_count     INTEGER,
                distinct_days   INTEGER,
                distinct_nights INTEGER,
                first_seen      TEXT,
                last_seen       TEXT,
                max_rssi        REAL,
                is_overnight    INTEGER,
                obs_count       INTEGER,
                survey_node     TEXT,
                computed_at     TEXT,
                -- 'device_cluster' = a place the device itself was seen; 'home_ap' = the
                -- located home AP (bed-down headline), with its BSSID/SSID.
                kind            TEXT NOT NULL DEFAULT 'device_cluster',
                bssid           TEXT,
                ssid            TEXT
            )
            """
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_finding_task ON survey_finding(task_id, rank)"
        )
        # Operator-bounded patrols (design §10). One row per walk. A patrol is ACTIVE
        # while ended_at IS NULL — during it the poll-quota task closure is suspended so
        # a task never expires mid-walk. Ending a patrol (ended_at set, finalized=0) is
        # the signal for the sync loop to finalize every still-open task as a unit.
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS survey_patrol (
                patrol_id  INTEGER PRIMARY KEY,
                started_at TEXT NOT NULL,
                ended_at   TEXT,
                finalized  INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        # Wardrive index (design §11). While a patrol runs the mobile node banks EVERY
        # AP it hears here — independent of what's tasked — so a bed-down can be resolved
        # by querying this index for a task's anchor SSID (retroactively, even for a
        # device tasked after the walk). Deduped by BSSID so re-walking a street never
        # inflates it; the row keeps the best-signal position. Bounded by a retention
        # sweep. Node-local, gitignored, never committed (captured third-party data).
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS survey_wardrive (
                bssid      TEXT PRIMARY KEY,
                ssid       TEXT,
                lat        REAL,
                lon        REAL,
                rssi       REAL,
                first_seen TEXT NOT NULL,
                last_seen  TEXT NOT NULL,
                patrol_id  INTEGER,
                obs_count  INTEGER NOT NULL DEFAULT 1
            )
            """
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_wardrive_ssid ON survey_wardrive(ssid)"
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_wardrive_last ON survey_wardrive(last_seen)"
        )
        self._migrate(cur)
        self._conn.commit()

    def _migrate(self, cur) -> None:
        """Add columns introduced after a dev DB may already exist (SQLite has no
        ADD COLUMN IF NOT EXISTS). No-op on a fresh DB created by the statements above."""
        want = {
            "survey_tasking": [("outcome", "TEXT"),
                               ("wigle_candidate", "INTEGER NOT NULL DEFAULT 0")],
            "survey_observation": [("kind", "TEXT NOT NULL DEFAULT 'device'"),
                                   ("bssid", "TEXT"), ("ssid", "TEXT")],
            "survey_finding": [("kind", "TEXT NOT NULL DEFAULT 'device_cluster'"),
                               ("bssid", "TEXT"), ("ssid", "TEXT")],
        }
        for table, cols in want.items():
            have = {r["name"] for r in cur.execute(f"PRAGMA table_info({table})").fetchall()}
            for name, decl in cols:
                if name not in have:
                    cur.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")

    # ------------------------------------------------------------------
    # Taskings
    # ------------------------------------------------------------------

    @_synchronized
    def enqueue_tasking(
        self,
        identity_key: str,
        *,
        designator: Optional[str] = None,
        reason: str = "operator",
        evidence: Optional[dict] = None,
        origin_node: Optional[str] = None,
        task_id: Optional[str] = None,
        now: Optional[datetime] = None,
    ) -> str:
        """Create a tasking for *identity_key*; return its task_id.

        De-duplicated on the identity: if an OPEN (open/surveying) tasking already
        names this device, its existing task_id is returned unchanged rather than a
        second task created — so re-flagging the same contact never floods the
        watchlist. Callers enforce the surveyable-key rule (a bare ``mac:`` key is
        not portable to another node); the store accepts whatever key it is given.
        """
        existing = self._conn.execute(
            "SELECT task_id FROM survey_tasking WHERE identity_key = ? "
            "AND status IN (%s)" % ",".join("?" * len(_OPEN_STATUSES)),
            (identity_key, *_OPEN_STATUSES),
        ).fetchone()
        if existing is not None:
            return existing["task_id"]

        tid = task_id or uuid.uuid4().hex
        ts = _iso(now or datetime.now(timezone.utc))
        self._conn.execute(
            "INSERT INTO survey_tasking "
            "(task_id, identity_key, designator, reason, evidence, created_at, "
            " origin_node, status) VALUES (?, ?, ?, ?, ?, ?, ?, 'open')",
            (tid, identity_key, designator, reason,
             json.dumps(evidence) if evidence is not None else None,
             ts, origin_node),
        )
        self._conn.commit()
        return tid

    @_synchronized
    def upsert_tasking(self, row: dict) -> None:
        """Store a tasking pulled from a peer node (mobile side), keyed on task_id so
        the origin's id is preserved. Does not clobber a status the local node has
        already advanced past 'open' (e.g. a 'complete' survey stays complete)."""
        tid = row.get("task_id")
        if not tid:
            return
        evidence = row.get("evidence")
        if isinstance(evidence, (dict, list)):
            evidence = json.dumps(evidence)
        local = self._conn.execute(
            "SELECT status FROM survey_tasking WHERE task_id = ?", (tid,)
        ).fetchone()
        if local is not None:
            # Keep a locally-advanced status; only refresh the descriptive fields.
            self._conn.execute(
                "UPDATE survey_tasking SET identity_key = ?, designator = ?, "
                "reason = ?, evidence = ?, origin_node = ? WHERE task_id = ?",
                (row.get("identity_key"), row.get("designator"), row.get("reason"),
                 evidence, row.get("origin_node"), tid),
            )
        else:
            self._conn.execute(
                "INSERT INTO survey_tasking "
                "(task_id, identity_key, designator, reason, evidence, created_at, "
                " origin_node, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (tid, row.get("identity_key"), row.get("designator"),
                 row.get("reason"), evidence,
                 row.get("created_at") or _iso(datetime.now(timezone.utc)),
                 row.get("origin_node"), row.get("status") or "open"),
            )
        self._conn.commit()

    @_synchronized
    def open_taskings(self) -> list:
        """All open/surveying taskings as dicts (the list the mobile node pulls)."""
        rows = self._conn.execute(
            "SELECT * FROM survey_tasking WHERE status IN (%s) ORDER BY created_at"
            % ",".join("?" * len(_OPEN_STATUSES)),
            _OPEN_STATUSES,
        ).fetchall()
        return [self._tasking_dict(r) for r in rows]

    @_synchronized
    def all_taskings(self) -> list:
        rows = self._conn.execute(
            "SELECT * FROM survey_tasking ORDER BY created_at DESC"
        ).fetchall()
        return [self._tasking_dict(r) for r in rows]

    @_synchronized
    def get_tasking(self, task_id: str) -> Optional[dict]:
        row = self._conn.execute(
            "SELECT * FROM survey_tasking WHERE task_id = ?", (task_id,)
        ).fetchone()
        return self._tasking_dict(row) if row is not None else None

    @_synchronized
    def open_identity_keys(self) -> dict:
        """Map of ``identity_key -> task_id`` for open taskings — the mobile matcher's
        lookup for "is this observed device on the watchlist?"."""
        rows = self._conn.execute(
            "SELECT identity_key, task_id FROM survey_tasking WHERE status IN (%s)"
            % ",".join("?" * len(_OPEN_STATUSES)),
            _OPEN_STATUSES,
        ).fetchall()
        return {r["identity_key"]: r["task_id"] for r in rows}

    @_synchronized
    def set_status(self, task_id: str, status: str) -> None:
        self._conn.execute(
            "UPDATE survey_tasking SET status = ? WHERE task_id = ?", (status, task_id)
        )
        self._conn.commit()

    @staticmethod
    def _tasking_dict(row: sqlite3.Row) -> dict:
        d = dict(row)
        raw = d.get("evidence")
        if raw:
            try:
                d["evidence"] = json.loads(raw)
            except (ValueError, TypeError):
                d["evidence"] = None
        return d

    # ------------------------------------------------------------------
    # Patrols (mobile side, design §10)
    # ------------------------------------------------------------------

    @_synchronized
    def start_patrol(self, now: Optional[datetime] = None) -> int:
        """Begin an operator-bounded patrol. Ends any dangling active patrol first (a
        prior walk the operator never closed), so there is at most one active patrol."""
        ts = _iso(now or datetime.now(timezone.utc))
        cur = self._conn.cursor()
        cur.execute("UPDATE survey_patrol SET ended_at = ? WHERE ended_at IS NULL", (ts,))
        cur.execute("INSERT INTO survey_patrol (started_at) VALUES (?)", (ts,))
        self._conn.commit()
        return int(cur.lastrowid)

    @_synchronized
    def end_patrol(self, now: Optional[datetime] = None) -> bool:
        """End the active patrol. Returns True if one was active (so the caller knows a
        finalize pass is due)."""
        ts = _iso(now or datetime.now(timezone.utc))
        cur = self._conn.cursor()
        cur.execute("UPDATE survey_patrol SET ended_at = ? WHERE ended_at IS NULL", (ts,))
        self._conn.commit()
        return cur.rowcount > 0

    @_synchronized
    def active_patrol(self) -> Optional[dict]:
        """The currently-running patrol (ended_at IS NULL), or None."""
        row = self._conn.execute(
            "SELECT * FROM survey_patrol WHERE ended_at IS NULL "
            "ORDER BY patrol_id DESC LIMIT 1"
        ).fetchone()
        return dict(row) if row is not None else None

    @_synchronized
    def patrol_pending_finalize(self) -> Optional[dict]:
        """A patrol that has ended but whose open tasks have not yet been finalized —
        the sync loop's cue to close out the walk as a unit."""
        row = self._conn.execute(
            "SELECT * FROM survey_patrol WHERE ended_at IS NOT NULL AND finalized = 0 "
            "ORDER BY patrol_id DESC LIMIT 1"
        ).fetchone()
        return dict(row) if row is not None else None

    @_synchronized
    def mark_patrol_finalized(self, patrol_id: int) -> None:
        self._conn.execute(
            "UPDATE survey_patrol SET finalized = 1 WHERE patrol_id = ?", (patrol_id,)
        )
        self._conn.commit()

    @_synchronized
    def patrol_status(self) -> dict:
        """Compact state for the mobile GUI: whether a patrol is active and since when."""
        row = self._conn.execute(
            "SELECT * FROM survey_patrol ORDER BY patrol_id DESC LIMIT 1"
        ).fetchone()
        if row is None:
            return {"active": False, "patrol_id": None,
                    "started_at": None, "ended_at": None}
        d = dict(row)
        return {"active": d["ended_at"] is None, "patrol_id": d["patrol_id"],
                "started_at": d["started_at"], "ended_at": d["ended_at"]}

    # ------------------------------------------------------------------
    # Observations (mobile side)
    # ------------------------------------------------------------------

    @_synchronized
    def record_survey_observation(
        self,
        task_id: str,
        *,
        timestamp: Optional[datetime] = None,
        lat: Optional[float] = None,
        lon: Optional[float] = None,
        rssi: Optional[float] = None,
        kind: str = "device",
        bssid: Optional[str] = None,
        ssid: Optional[str] = None,
    ) -> None:
        """Record one survey sighting. ``kind='device'`` = the tasked device itself was
        seen here; ``kind='ap'`` = a local AP beaconing the device's distinctive home
        network was heard here (its BSSID/SSID captured) — the single-patrol bed-down
        signal. Called on the poll thread; the node's own GPS fix is the position."""
        ts = _iso(timestamp or datetime.now(timezone.utc))
        self._conn.execute(
            "INSERT INTO survey_observation "
            "(task_id, timestamp, lat, lon, rssi, kind, bssid, ssid) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (task_id, ts, lat, lon, rssi, kind, bssid, ssid),
        )
        self._conn.commit()

    @_synchronized
    def observation_count(self, task_id: str) -> int:
        return self._conn.execute(
            "SELECT COUNT(*) FROM survey_observation WHERE task_id = ?", (task_id,)
        ).fetchone()[0]

    @_synchronized
    def tasks_with_observations(self) -> list:
        """Task ids that have at least one recorded observation — the mobile sync
        loop only computes/pushes findings for these."""
        rows = self._conn.execute(
            "SELECT DISTINCT task_id FROM survey_observation"
        ).fetchall()
        return [r["task_id"] for r in rows]

    # ------------------------------------------------------------------
    # Findings — cluster observations into bed-down locations
    # ------------------------------------------------------------------

    @_synchronized
    def compute_findings(
        self,
        task_id: str,
        *,
        threshold_meters: float = _CLUSTER_THRESHOLD_M,
        visit_gap_seconds: float = _VISIT_GAP_S,
        night_hours: Optional[str] = None,
        tz: Optional[tzinfo] = None,
        survey_node: Optional[str] = None,
        persist: bool = True,
    ) -> dict:
        """Resolve a task's survey observations into a **structured result**.

        The bed-down is resolved by AP ASSOCIATION first: if the mobile node heard a
        local AP beaconing the device's distinctive home network (``kind='ap'``
        observations), that AP's location is the residence — a single patrol suffices,
        no dwell accumulation needed. The device's own sightings (``kind='device'``)
        are clustered too and carry the dwell/return/overnight annotation as
        *confidence*, not as the primary signal.

        Returns a dict::

            {task_id, survey_node, located, home_ap, clusters, wigle_candidate, outcome}

        * ``home_ap`` — ``{bssid, ssid, lat, lon, max_rssi, obs_count}`` or None.
        * ``outcome`` — ``resident`` (home AP found locally) / ``seen`` (device seen,
          no local home AP) / ``not_located`` (patrolled, found nothing).
        * ``wigle_candidate`` — True when the home AP was NOT found locally, so the
          operator may deliberately run a WiGLE lookup (a separate, manual step).

        When *persist*, the result replaces the task's stored findings and updates its
        outcome/wigle flag. *tz* fixes the night-test zone for deterministic tests.
        """
        start_h, end_h = _parse_night_hours(night_hours or _NIGHT_HOURS)
        local_tz = tz if tz is not None else datetime.now().astimezone().tzinfo

        rows = self._conn.execute(
            "SELECT timestamp, lat, lon, rssi, kind, bssid, ssid "
            "FROM survey_observation WHERE task_id = ? ORDER BY timestamp",
            (task_id,),
        ).fetchall()
        device_rows = [r for r in rows if (r["kind"] or "device") == "device"]
        ap_rows = [r for r in rows if r["kind"] == "ap"]

        # Device sightings -> ranked spatial clusters (dwell/return = annotation).
        clusters = [
            self._cluster_finding(c, visit_gap_seconds, start_h, end_h, local_tz)
            for c in self._spatial_clusters(device_rows, threshold_meters)
        ]
        clusters.sort(key=lambda f: f["dwell_seconds"] * max(f["visit_count"], 1),
                      reverse=True)
        for rank, f in enumerate(clusters):
            f["rank"] = rank

        # AP associations -> the dominant cluster is the located home AP.
        home_ap = self._resolve_home_ap(
            self._spatial_clusters(ap_rows, threshold_meters))

        located = bool(clusters or home_ap)
        if home_ap is not None:
            outcome, wigle_candidate = "resident", False
        elif clusters:
            outcome, wigle_candidate = "seen", True
        else:
            outcome, wigle_candidate = "not_located", True

        result = {
            "task_id": task_id, "survey_node": survey_node,
            "located": located, "home_ap": home_ap, "clusters": clusters,
            "wigle_candidate": wigle_candidate, "outcome": outcome,
        }
        if persist:
            self._store_result(task_id, result, survey_node)
        return result

    @staticmethod
    def _spatial_clusters(rows: list, threshold_meters: float) -> list:
        """Greedy nearest-centroid clustering (the PersistenceEngine approach); each
        cluster keeps its observation rows so temporal/AP features derive afterwards."""
        clusters: list = []
        for r in rows:
            lat, lon = r["lat"], r["lon"]
            if lat is None or lon is None:
                continue  # no position — can't place it on the map
            placed = False
            for c in clusters:
                if _haversine(lat, lon, c["lat"], c["lon"]) <= threshold_meters:
                    n = c["count"]
                    c["lat"] = (c["lat"] * n + lat) / (n + 1)
                    c["lon"] = (c["lon"] * n + lon) / (n + 1)
                    c["count"] = n + 1
                    c["obs"].append(r)
                    placed = True
                    break
            if not placed:
                clusters.append({"lat": lat, "lon": lon, "count": 1, "obs": [r]})
        return clusters

    @staticmethod
    def _resolve_home_ap(ap_clusters: list) -> Optional[dict]:
        """The located home AP: the AP-association cluster with the most sightings.
        Its dominant BSSID + SSID name it; its centroid locates it."""
        if not ap_clusters:
            return None
        best = max(ap_clusters, key=lambda c: c["count"])
        bssids = Counter(o["bssid"] for o in best["obs"] if o["bssid"])
        ssids = Counter(o["ssid"] for o in best["obs"] if o["ssid"])
        rssis = [o["rssi"] for o in best["obs"]
                 if o["rssi"] is not None and o["rssi"] != 0]
        return {
            "bssid": bssids.most_common(1)[0][0] if bssids else None,
            "ssid": ssids.most_common(1)[0][0] if ssids else None,
            "lat": best["lat"], "lon": best["lon"],
            "max_rssi": max(rssis) if rssis else None,
            "obs_count": best["count"],
        }

    @staticmethod
    def _cluster_finding(cluster: dict, visit_gap_seconds: float,
                         start_h: int, end_h: int, local_tz) -> dict:
        """Derive the dwell/return/overnight features for one spatial cluster."""
        times = sorted(
            t for t in (_parse_iso(o["timestamp"]) for o in cluster["obs"]) if t
        )
        dwell = 0.0
        visits = 1 if times else 0
        for prev, cur in zip(times, times[1:]):
            gap = (cur - prev).total_seconds()
            if gap <= visit_gap_seconds:
                dwell += gap
            else:
                visits += 1

        distinct_days = {t.astimezone(local_tz).date() for t in times}
        night_dates = set()
        for t in times:
            lt = t.astimezone(local_tz)
            if _is_night_hour(lt.hour, start_h, end_h):
                # Attribute a pre-dawn sighting to the evening it began, so a single
                # overnight stay counts as one night, not two.
                anchor = lt.date() if lt.hour >= max(start_h, end_h) else (lt - timedelta(days=1)).date()
                night_dates.add(anchor)

        # RSSI: strongest (closest to 0) real reading; 0/None are placeholders, not
        # measurements (the project-wide zero-RSSI rule), so they are skipped.
        rssis = [o["rssi"] for o in cluster["obs"]
                 if o["rssi"] is not None and o["rssi"] != 0]
        max_rssi = max(rssis) if rssis else None

        return {
            "cluster_lat": cluster["lat"],
            "cluster_lon": cluster["lon"],
            "dwell_seconds": dwell,
            "visit_count": visits,
            "distinct_days": len(distinct_days),
            "distinct_nights": len(night_dates),
            "first_seen": _iso(times[0]) if times else None,
            "last_seen": _iso(times[-1]) if times else None,
            "max_rssi": max_rssi,
            "is_overnight": bool(night_dates),
            "obs_count": cluster["count"],
        }

    @_synchronized
    def _store_result(self, task_id: str, result: dict,
                      survey_node: Optional[str]) -> None:
        """Replace a task's findings wholesale from a structured result (idempotent
        recompute/ingest): the located home AP as one ``home_ap`` finding row plus the
        device-sighting clusters, and the task's outcome/wigle flag."""
        computed_at = _iso(datetime.now(timezone.utc))
        self._conn.execute("DELETE FROM survey_finding WHERE task_id = ?", (task_id,))

        ap = result.get("home_ap")
        if ap:
            self._conn.execute(
                "INSERT INTO survey_finding "
                "(task_id, rank, cluster_lat, cluster_lon, max_rssi, obs_count, "
                " survey_node, computed_at, kind, bssid, ssid) "
                "VALUES (?,?,?,?,?,?,?,?, 'home_ap', ?, ?)",
                (task_id, 0, ap.get("lat"), ap.get("lon"), ap.get("max_rssi"),
                 ap.get("obs_count", 0), survey_node, computed_at,
                 ap.get("bssid"), ap.get("ssid")),
            )
        for f in result.get("clusters", []):
            self._conn.execute(
                "INSERT INTO survey_finding "
                "(task_id, rank, cluster_lat, cluster_lon, dwell_seconds, "
                " visit_count, distinct_days, distinct_nights, first_seen, "
                " last_seen, max_rssi, is_overnight, obs_count, survey_node, "
                " computed_at, kind) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?, 'device_cluster')",
                (task_id, f.get("rank", 0), f.get("cluster_lat"), f.get("cluster_lon"),
                 f.get("dwell_seconds", 0.0), f.get("visit_count", 0),
                 f.get("distinct_days", 0), f.get("distinct_nights", 0),
                 f.get("first_seen"), f.get("last_seen"), f.get("max_rssi"),
                 1 if f.get("is_overnight") else 0, f.get("obs_count", 0),
                 survey_node, computed_at),
            )
        self._conn.execute(
            "UPDATE survey_tasking SET outcome = ?, wigle_candidate = ? WHERE task_id = ?",
            (result.get("outcome"), 1 if result.get("wigle_candidate") else 0, task_id),
        )
        self._conn.commit()

    @_synchronized
    def ingest_result(self, task_id: str, result: dict,
                      survey_node: Optional[str] = None,
                      complete: bool = True) -> None:
        """Fixed-side: store a structured survey result pushed by a mobile node and (by
        default) mark the tasking complete."""
        self._store_result(task_id, result, survey_node or result.get("survey_node"))
        if complete:
            self._conn.execute(
                "UPDATE survey_tasking SET status = 'complete' WHERE task_id = ?",
                (task_id,),
            )
            self._conn.commit()

    @_synchronized
    def findings_for(self, task_id: str) -> dict:
        """The stored survey result for a task: ``{home_ap, clusters}`` (home_ap is
        None when no local home AP was located)."""
        rows = self._conn.execute(
            "SELECT * FROM survey_finding WHERE task_id = ? ORDER BY rank",
            (task_id,),
        ).fetchall()
        home_ap = None
        clusters = []
        for r in rows:
            d = dict(r)
            if d.get("kind") == "home_ap":
                home_ap = {"bssid": d.get("bssid"), "ssid": d.get("ssid"),
                           "lat": d.get("cluster_lat"), "lon": d.get("cluster_lon"),
                           "max_rssi": d.get("max_rssi"), "obs_count": d.get("obs_count")}
            else:
                d["is_overnight"] = bool(d.get("is_overnight"))
                clusters.append(d)
        return {"home_ap": home_ap, "clusters": clusters}

    @_synchronized
    def taskings_with_findings(self) -> list:
        """Fixed-side GUI feed: every tasking with its resolved survey result —
        ``outcome`` (resident/seen/not_located), ``wigle_candidate``, the located
        ``home_ap``, and the device-sighting ``clusters``."""
        out = []
        for t in self.all_taskings():
            t = dict(t)
            res = self.findings_for(t["task_id"])
            t["home_ap"] = res["home_ap"]
            t["clusters"] = res["clusters"]
            t["wigle_candidate"] = bool(t.get("wigle_candidate"))
            out.append(t)
        return out

    # ------------------------------------------------------------------
    # Wardrive index (design §11)
    # ------------------------------------------------------------------

    @_synchronized
    def upsert_wardrive_ap(self, *, bssid: str, ssid: Optional[str],
                           lat: Optional[float], lon: Optional[float],
                           rssi: Optional[float], timestamp: datetime,
                           patrol_id: Optional[int] = None) -> None:
        """Bank one heard AP into the wardrive index, deduped by BSSID. Re-hearing the
        same AP bumps its observation count and last-seen; its position is replaced only
        when the new reading is stronger (best-signal fix wins), so the located point is
        the closest pass, not the last."""
        ts = _iso(timestamp)
        self._conn.execute(
            """
            INSERT INTO survey_wardrive
                (bssid, ssid, lat, lon, rssi, first_seen, last_seen, patrol_id, obs_count)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1)
            ON CONFLICT(bssid) DO UPDATE SET
                last_seen = excluded.last_seen,
                obs_count = obs_count + 1,
                ssid      = COALESCE(excluded.ssid, ssid),
                patrol_id = COALESCE(excluded.patrol_id, patrol_id),
                lat  = CASE WHEN excluded.rssi IS NOT NULL
                            AND (rssi IS NULL OR excluded.rssi > rssi)
                       THEN excluded.lat ELSE lat END,
                lon  = CASE WHEN excluded.rssi IS NOT NULL
                            AND (rssi IS NULL OR excluded.rssi > rssi)
                       THEN excluded.lon ELSE lon END,
                rssi = CASE WHEN excluded.rssi IS NOT NULL
                            AND (rssi IS NULL OR excluded.rssi > rssi)
                       THEN excluded.rssi ELSE rssi END
            """,
            (bssid, ssid, lat, lon, rssi, ts, ts, patrol_id),
        )
        self._conn.commit()

    @_synchronized
    def wardrive_aps_for_ssid(self, ssid: str) -> list:
        """Every banked AP beaconing this SSID (case-insensitive) — the retroactive
        bed-down lookup for a task's anchor. Returns dicts with a located position."""
        rows = self._conn.execute(
            "SELECT bssid, ssid, lat, lon, rssi, first_seen, last_seen, obs_count "
            "FROM survey_wardrive WHERE lower(ssid) = ?",
            ((ssid or "").strip().lower(),),
        ).fetchall()
        return [dict(r) for r in rows]

    @_synchronized
    def wardrive_count(self) -> int:
        """Number of distinct APs banked in the wardrive index."""
        return int(self._conn.execute(
            "SELECT count(*) FROM survey_wardrive").fetchone()[0])

    # ------------------------------------------------------------------
    # Maintenance
    # ------------------------------------------------------------------

    @_synchronized
    def prune_observations(self, now: Optional[datetime] = None) -> int:
        """Delete survey observations older than the retention window; return the row
        count removed. No-op when retention is 0/negative (keep forever)."""
        if self._retention_days <= 0:
            return 0
        cutoff = _iso((now or datetime.now(timezone.utc))
                      - timedelta(days=self._retention_days))
        cur = self._conn.execute(
            "DELETE FROM survey_observation WHERE timestamp < ?", (cutoff,)
        )
        self._conn.commit()
        return cur.rowcount

    @_synchronized
    def prune_wardrive(self, retention_days: int,
                       now: Optional[datetime] = None) -> int:
        """Delete wardrive APs not heard within the retention window; return rows
        removed. No-op when retention is 0/negative (keep forever). Dedup by BSSID keeps
        the index bounded by area, so this only sheds long-unseen ground."""
        if retention_days <= 0:
            return 0
        cutoff = _iso((now or datetime.now(timezone.utc))
                      - timedelta(days=retention_days))
        cur = self._conn.execute(
            "DELETE FROM survey_wardrive WHERE last_seen < ?", (cutoff,)
        )
        self._conn.commit()
        return cur.rowcount

    @_synchronized
    def close(self) -> None:
        try:
            self._conn.close()
        except sqlite3.Error:  # pragma: no cover - defensive
            pass
