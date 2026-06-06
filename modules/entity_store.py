"""entity_store — durable SQLite tables for the entity/observation foundation.

Phase A: persists, additively and alongside the existing in-memory
``PersistenceEngine._observations`` path, the probe-SSID evidence, per-device
fingerprint, logical entities, and a growing observation history. It does NOT
participate in scoring and is never on the path that produces alerts — a write
failure here must never affect detection (callers guard the call).

The single most important correctness property: every per-device, per-poll write
except the observation history is a real UPSERT (``INSERT ... ON CONFLICT ... DO
UPDATE``). A miskeyed upsert that inserts a fresh row every poll would recreate
the in-memory growth problem on disk; the row counts for a stable device set
must level off, not climb per poll. Only the ``observations`` table grows by
design (history; pruning is a later phase).
"""

import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# Default DB path derived from this file's location (modules/ -> repo root), so
# it resolves identically under systemd or by hand; lives under gitignored data/.
_DEFAULT_ENTITY_DB_PATH = str(Path(__file__).resolve().parent.parent / "data" / "entities.db")


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


class EntityStore:
    """SQLite-backed durable store for probe evidence, fingerprints, entities,
    and observation history. All writes are additive; none affect scoring."""

    def __init__(self, db_path: Optional[str] = None) -> None:
        self._db_path = db_path or _DEFAULT_ENTITY_DB_PATH
        if self._db_path != ":memory:":
            Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self._db_path)
        self._conn.row_factory = sqlite3.Row
        self._create_schema()

    # ------------------------------------------------------------------
    # Schema
    # ------------------------------------------------------------------

    def _create_schema(self) -> None:
        cur = self._conn.cursor()
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS probe_evidence (
                mac        TEXT    NOT NULL,
                ssid       TEXT    NOT NULL,
                first_seen TEXT    NOT NULL,
                last_seen  TEXT    NOT NULL,
                probe_count INTEGER NOT NULL DEFAULT 1,
                PRIMARY KEY (mac, ssid)
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS device_fingerprint (
                mac               TEXT PRIMARY KEY,
                probe_fingerprint INTEGER,
                num_probed_ssids  INTEGER,
                first_seen        TEXT NOT NULL,
                last_seen         TEXT NOT NULL
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS entities (
                entity_id   INTEGER PRIMARY KEY,
                entity_type TEXT    NOT NULL,
                identifier  TEXT    NOT NULL,
                first_seen  TEXT    NOT NULL,
                last_seen   TEXT    NOT NULL,
                obs_count   INTEGER NOT NULL DEFAULT 0,
                UNIQUE (entity_type, identifier)
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS observations (
                obs_id         INTEGER PRIMARY KEY,
                entity_id      INTEGER NOT NULL REFERENCES entities(entity_id),
                timestamp      TEXT    NOT NULL,
                lat            REAL,
                lon            REAL,
                pos_source     TEXT,
                pos_confidence REAL,
                signal         REAL
            )
            """
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_obs_entity ON observations(entity_id, timestamp)"
        )
        self._conn.commit()

    # ------------------------------------------------------------------
    # Write path — one call per poll, all writes for the poll in one commit
    # ------------------------------------------------------------------

    def record_poll(self, devices: list, *, gps_fix: Optional[dict] = None,
                    now: Optional[datetime] = None) -> None:
        """Persist one poll cycle: per device, upsert probe evidence / fingerprint /
        entity and insert one observation row. The node's own GPS fix is the
        position for every device this poll (a fixed/mobile node reports its own
        location); all four position fields are null when there is no fix."""
        now = now or datetime.now(timezone.utc)
        ts = _iso(now)
        if gps_fix:
            lat, lon = gps_fix.get("lat"), gps_fix.get("lon")
            pos_source, pos_confidence = "gps_node", 1.0
        else:
            lat = lon = pos_source = pos_confidence = None

        cur = self._conn.cursor()
        for device in devices:
            mac = device.get("macaddr", "")
            if not mac:
                continue

            # device_fingerprint — one row per device (covers wildcard-only ones).
            cur.execute(
                "INSERT INTO device_fingerprint "
                "(mac, probe_fingerprint, num_probed_ssids, first_seen, last_seen) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(mac) DO UPDATE SET "
                "  last_seen = excluded.last_seen, "
                "  probe_fingerprint = excluded.probe_fingerprint, "
                "  num_probed_ssids = excluded.num_probed_ssids",
                (mac, device.get("probe_fingerprint"),
                 device.get("num_probed_ssids", 0), ts, ts),
            )

            # probe_evidence — one row per NAMED ssid; wildcard/blank excluded.
            for ssid in device.get("probe_ssids", []) or []:
                if not isinstance(ssid, str) or not ssid.strip():
                    continue
                cur.execute(
                    "INSERT INTO probe_evidence "
                    "(mac, ssid, first_seen, last_seen, probe_count) "
                    "VALUES (?, ?, ?, ?, 1) "
                    "ON CONFLICT(mac, ssid) DO UPDATE SET "
                    "  last_seen = excluded.last_seen, "
                    "  probe_count = probe_count + 1",
                    (mac, ssid, ts, ts),
                )

            # entities — upsert the wifi entity, then read its id for the FK.
            cur.execute(
                "INSERT INTO entities "
                "(entity_type, identifier, first_seen, last_seen, obs_count) "
                "VALUES ('wifi', ?, ?, ?, 1) "
                "ON CONFLICT(entity_type, identifier) DO UPDATE SET "
                "  last_seen = excluded.last_seen, "
                "  obs_count = obs_count + 1",
                (mac, ts, ts),
            )
            entity_id = cur.execute(
                "SELECT entity_id FROM entities WHERE entity_type = 'wifi' AND identifier = ?",
                (mac,),
            ).fetchone()[0]

            # observations — history; this is the only table that grows per poll.
            cur.execute(
                "INSERT INTO observations "
                "(entity_id, timestamp, lat, lon, pos_source, pos_confidence, signal) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (entity_id, ts, lat, lon, pos_source, pos_confidence,
                 device.get("last_signal")),
            )

        self._conn.commit()

    # ------------------------------------------------------------------
    # Read helpers (tests / inspection)
    # ------------------------------------------------------------------

    def count(self, table: str) -> int:
        if table not in ("probe_evidence", "device_fingerprint", "entities", "observations"):
            raise ValueError(f"unknown table {table!r}")
        return self._conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]

    def probe_evidence_row(self, mac: str, ssid: str):
        return self._conn.execute(
            "SELECT * FROM probe_evidence WHERE mac = ? AND ssid = ?", (mac, ssid)
        ).fetchone()

    def device_fingerprint_row(self, mac: str):
        return self._conn.execute(
            "SELECT * FROM device_fingerprint WHERE mac = ?", (mac,)
        ).fetchone()

    def entity_row(self, identifier: str, entity_type: str = "wifi"):
        return self._conn.execute(
            "SELECT * FROM entities WHERE entity_type = ? AND identifier = ?",
            (entity_type, identifier),
        ).fetchone()

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug("EntityStore close error: %s", exc)
