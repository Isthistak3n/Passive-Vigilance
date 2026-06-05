"""baseline_store — durable SQLite store for fixed-mode pattern-of-life.

The fixed-node model learns an environment's normal RF "pattern of life" over a
configurable window, then scores deviations from it. That baseline **must
survive service restarts and reboots** — an in-memory baseline is destroyed by
any restart, and during the 2026-06 crash-loop incident the service restarted
~60 times in 70 minutes. A node whose learning window reset on every restart
would re-enter learning forever and never alert (design 5.1).

The single most important correctness property of this module:

    The learning-window START timestamp is persisted on first init and is
    NEVER recomputed from "now" on a later open. A restart RESUMES the existing
    window; it does not reset it.

Per-device profiles are keyed by the caller (stable MAC, or probe-fingerprint
for randomized MACs — see :mod:`modules.fixed_scoring`). This module is keying-
agnostic: it stores whatever string key it is given.
"""

import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# Default DB path derived from this file's location (modules/ -> repo root), so
# it resolves to the same absolute path whether run by systemd (with a
# WorkingDirectory) or by hand from any CWD. Lives under the gitignored data/.
_DEFAULT_DB_PATH = str(Path(__file__).resolve().parent.parent / "data" / "baseline.db")


@dataclass
class DeviceProfile:
    """One per-device behavioral profile row.

    Phase 1 populates ``key``, ``first_seen``, ``last_seen``,
    ``observation_count`` and the lightweight identity fields. The dwell /
    time-histogram / signal columns are reserved in the schema for later phases
    and are not populated here.
    """

    key: str
    first_seen: datetime
    last_seen: datetime
    observation_count: int
    manufacturer: str = ""
    device_type: str = ""
    mac_type: str = "static"


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def _parse(s: str) -> datetime:
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


class BaselineStore:
    """SQLite-backed durable store for the fixed-mode baseline.

    Args:
        db_path:        Path to the SQLite file. Defaults to ``_DEFAULT_DB_PATH``.
        baseline_hours: Length of the learning window in hours.
        now:            Override "current time" used ONLY for the first-ever
                        ``learning_start`` insert (testing hook). On a reopen the
                        persisted ``learning_start`` is kept and this is ignored.
    """

    def __init__(
        self,
        db_path: Optional[str] = None,
        baseline_hours: float = 72.0,
        now: Optional[datetime] = None,
    ) -> None:
        self._db_path = db_path or _DEFAULT_DB_PATH
        self._baseline_hours = float(baseline_hours)
        if self._db_path != ":memory:":
            Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self._db_path)
        self._conn.row_factory = sqlite3.Row
        self._create_schema()
        self._init_meta(now or datetime.now(timezone.utc))

    # ------------------------------------------------------------------
    # Schema / meta
    # ------------------------------------------------------------------

    def _create_schema(self) -> None:
        cur = self._conn.cursor()
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS baseline_meta (
                id             INTEGER PRIMARY KEY CHECK (id = 1),
                learning_start TEXT    NOT NULL,
                baseline_hours REAL    NOT NULL
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS device_profiles (
                key               TEXT PRIMARY KEY,
                first_seen        TEXT    NOT NULL,
                last_seen         TEXT    NOT NULL,
                observation_count INTEGER NOT NULL DEFAULT 0,
                manufacturer      TEXT    DEFAULT '',
                device_type       TEXT    DEFAULT '',
                mac_type          TEXT    DEFAULT 'static',
                -- reserved for later phases; NOT populated in Phase 1
                dwell_seconds     REAL,
                time_histogram    TEXT,
                signal_mean       REAL,
                signal_var        REAL
            )
            """
        )
        self._conn.commit()

    def _init_meta(self, now: datetime) -> None:
        """Insert the learning_start row on first init only; never overwrite it.

        ``baseline_hours`` is refreshed to the current value on every open so an
        operator can retune ``FIXED_BASELINE_HOURS`` — but ``learning_start`` is
        immutable once written, which is the crash-loop-safety property.
        """
        row = self._conn.execute(
            "SELECT learning_start FROM baseline_meta WHERE id = 1"
        ).fetchone()
        if row is None:
            self._conn.execute(
                "INSERT INTO baseline_meta (id, learning_start, baseline_hours) "
                "VALUES (1, ?, ?)",
                (_iso(now), self._baseline_hours),
            )
            logger.info(
                "Baseline learning window started at %s (%.1fh) — db=%s",
                _iso(now), self._baseline_hours, self._db_path,
            )
        else:
            # Resume the existing window; only refresh the (mutable) duration.
            self._conn.execute(
                "UPDATE baseline_meta SET baseline_hours = ? WHERE id = 1",
                (self._baseline_hours,),
            )
            logger.info(
                "Resumed existing baseline learning window (start=%s, %.1fh)",
                row["learning_start"], self._baseline_hours,
            )
        self._conn.commit()

    # ------------------------------------------------------------------
    # Lifecycle queries
    # ------------------------------------------------------------------

    @property
    def learning_start(self) -> datetime:
        row = self._conn.execute(
            "SELECT learning_start FROM baseline_meta WHERE id = 1"
        ).fetchone()
        return _parse(row["learning_start"])

    @property
    def freeze_time(self) -> datetime:
        return self.learning_start + timedelta(hours=self._baseline_hours)

    def is_learning(self, now: datetime) -> bool:
        """True while still within the learning window (baseline not yet frozen)."""
        return now < self.freeze_time

    # ------------------------------------------------------------------
    # Profiles
    # ------------------------------------------------------------------

    def upsert(
        self,
        key: str,
        now: datetime,
        manufacturer: str = "",
        device_type: str = "",
        mac_type: str = "static",
    ) -> DeviceProfile:
        """Insert or update the profile for ``key`` and return it.

        On first sight ``first_seen`` is set to ``now`` and never changed
        thereafter; ``last_seen`` and ``observation_count`` advance each call.
        """
        existing = self.get_profile(key)
        if existing is None:
            self._conn.execute(
                "INSERT INTO device_profiles "
                "(key, first_seen, last_seen, observation_count, "
                " manufacturer, device_type, mac_type) "
                "VALUES (?, ?, ?, 1, ?, ?, ?)",
                (key, _iso(now), _iso(now), manufacturer, device_type, mac_type),
            )
            self._conn.commit()
            return DeviceProfile(
                key=key, first_seen=now, last_seen=now, observation_count=1,
                manufacturer=manufacturer, device_type=device_type, mac_type=mac_type,
            )
        self._conn.execute(
            "UPDATE device_profiles SET last_seen = ?, "
            "observation_count = observation_count + 1, "
            "manufacturer = ?, device_type = ?, mac_type = ? WHERE key = ?",
            (_iso(now), manufacturer or existing.manufacturer,
             device_type or existing.device_type, mac_type, key),
        )
        self._conn.commit()
        return DeviceProfile(
            key=key,
            first_seen=existing.first_seen,
            last_seen=now,
            observation_count=existing.observation_count + 1,
            manufacturer=manufacturer or existing.manufacturer,
            device_type=device_type or existing.device_type,
            mac_type=mac_type,
        )

    def get_profile(self, key: str) -> Optional[DeviceProfile]:
        row = self._conn.execute(
            "SELECT * FROM device_profiles WHERE key = ?", (key,)
        ).fetchone()
        if row is None:
            return None
        return DeviceProfile(
            key=row["key"],
            first_seen=_parse(row["first_seen"]),
            last_seen=_parse(row["last_seen"]),
            observation_count=row["observation_count"],
            manufacturer=row["manufacturer"] or "",
            device_type=row["device_type"] or "",
            mac_type=row["mac_type"] or "static",
        )

    def profile_count(self) -> int:
        return self._conn.execute(
            "SELECT COUNT(*) AS n FROM device_profiles"
        ).fetchone()["n"]

    def baseline_count(self) -> int:
        """Number of profiles first seen during the learning window (the baseline)."""
        return self._conn.execute(
            "SELECT COUNT(*) AS n FROM device_profiles WHERE first_seen <= ?",
            (_iso(self.freeze_time),),
        ).fetchone()["n"]

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug("BaselineStore close error: %s", exc)
