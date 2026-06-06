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

import json
import logging
import os
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

# Weight (0 < alpha <= 1) for the bounded post-freeze recent-signal EMA: higher
# is more responsive to the latest readings. Env-overridable. The EMA is what the
# Phase 2.5 approaching trigger compares against the frozen baseline mean.
_RECENT_SIGNAL_EMA_ALPHA = float(os.getenv("APPROACHING_RECENT_EMA_ALPHA", "0.3"))


@dataclass
class DeviceProfile:
    """One per-device behavioral profile row.

    Phase 1 populated the identity/recency fields. Phase 2 adds the baseline
    behavioral statistics accumulated during the learning window:

    - ``hour_mask`` — 24-bit mask of hours-of-day the device was seen during
      baseline (bit ``h`` set => seen in hour ``h``). Used for off-schedule.
    - ``signal_mean`` / ``signal_var`` — running mean/population variance of
      ``last_signal`` (RSSI) over baseline (``None`` until a non-None sample is
      seen). Populated for Phase 2.5; no trigger uses them yet.
    - ``signal_count`` — number of non-None/non-zero RSSI samples behind them.

    Phase 2.5 adds a separate POST-FREEZE recent-signal accumulator (kept apart
    from the frozen baseline stats above), used by the approaching trigger:

    - ``recent_signal_mean`` — bounded EMA of recent post-freeze RSSI readings
      (``None`` until a reading is folded in).
    - ``recent_signal_count`` — number of post-freeze readings behind the EMA.

    The ``dwell_seconds`` column remains reserved (later-phase abnormal-dwell).
    """

    key: str
    first_seen: datetime
    last_seen: datetime
    observation_count: int
    manufacturer: str = ""
    device_type: str = ""
    mac_type: str = "static"
    hour_mask: int = 0
    signal_mean: Optional[float] = None
    signal_var: Optional[float] = None
    signal_count: int = 0
    recent_signal_mean: Optional[float] = None
    recent_signal_count: int = 0


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

    # Default accumulator state (stored as JSON in time_histogram). The sig_*
    # fields are the frozen baseline stats; rec_* are the post-freeze EMA.
    _EMPTY_STATE = {
        "hour_mask": 0, "sig_n": 0, "sig_mean": 0.0, "sig_m2": 0.0,
        "rec_n": 0, "rec_ema": 0.0,
    }

    def upsert(
        self,
        key: str,
        now: datetime,
        manufacturer: str = "",
        device_type: str = "",
        mac_type: str = "static",
        last_signal: Optional[float] = None,
        accumulate_baseline: bool = True,
    ) -> DeviceProfile:
        """Insert or update the profile for ``key`` and return it.

        On first sight ``first_seen`` is set to ``now`` and never changed
        thereafter; ``last_seen`` and ``observation_count`` advance each call.

        When ``accumulate_baseline`` is True (i.e. during the learning window)
        the baseline behavioral stats are folded in: the hour-of-day bit for
        ``now`` is set, and a non-``None`` ``last_signal`` updates the running
        RSSI mean/variance (Welford). After freeze, callers pass
        ``accumulate_baseline=False`` so post-freeze sightings never mutate the
        frozen baseline — only recency (``last_seen`` / ``observation_count``)
        advances. ``None`` RSSI samples are skipped and never counted.
        """
        existing = self.get_profile(key)
        state = self._load_state(key) if existing is not None else dict(self._EMPTY_STATE)

        # A zero RSSI reading is Kismet's "no real sample" placeholder, so it is
        # skipped exactly like a missing reading (never folded into any stat).
        usable_signal = last_signal is not None and last_signal != 0

        if accumulate_baseline:
            state["hour_mask"] |= (1 << now.hour)
            if usable_signal:
                n = state["sig_n"] + 1
                delta = last_signal - state["sig_mean"]
                mean = state["sig_mean"] + delta / n
                state["sig_n"] = n
                state["sig_mean"] = mean
                state["sig_m2"] = state["sig_m2"] + delta * (last_signal - mean)
        else:
            # Post-freeze: fold the reading into the bounded recent-signal EMA
            # (seed with the first reading, then exponentially smooth).
            if usable_signal:
                if state["rec_n"] == 0:
                    state["rec_ema"] = float(last_signal)
                else:
                    a = _RECENT_SIGNAL_EMA_ALPHA
                    state["rec_ema"] = a * last_signal + (1 - a) * state["rec_ema"]
                state["rec_n"] = state["rec_n"] + 1

        hist_json = json.dumps(state)
        sig_n = state["sig_n"]
        sig_mean = state["sig_mean"] if sig_n > 0 else None
        sig_var = (state["sig_m2"] / sig_n) if sig_n > 0 else None
        rec_n = state["rec_n"]
        rec_mean = state["rec_ema"] if rec_n > 0 else None

        if existing is None:
            self._conn.execute(
                "INSERT INTO device_profiles "
                "(key, first_seen, last_seen, observation_count, "
                " manufacturer, device_type, mac_type, "
                " time_histogram, signal_mean, signal_var) "
                "VALUES (?, ?, ?, 1, ?, ?, ?, ?, ?, ?)",
                (key, _iso(now), _iso(now), manufacturer, device_type, mac_type,
                 hist_json, sig_mean, sig_var),
            )
            first_seen, obs = now, 1
            manuf_final, dtype_final = manufacturer, device_type
        else:
            self._conn.execute(
                "UPDATE device_profiles SET last_seen = ?, "
                "observation_count = observation_count + 1, "
                "manufacturer = ?, device_type = ?, mac_type = ?, "
                "time_histogram = ?, signal_mean = ?, signal_var = ? WHERE key = ?",
                (_iso(now), manufacturer or existing.manufacturer,
                 device_type or existing.device_type, mac_type,
                 hist_json, sig_mean, sig_var, key),
            )
            first_seen = existing.first_seen
            obs = existing.observation_count + 1
            manuf_final = manufacturer or existing.manufacturer
            dtype_final = device_type or existing.device_type
        self._conn.commit()
        return DeviceProfile(
            key=key,
            first_seen=first_seen,
            last_seen=now,
            observation_count=obs,
            manufacturer=manuf_final,
            device_type=dtype_final,
            mac_type=mac_type,
            hour_mask=state["hour_mask"],
            signal_mean=sig_mean,
            signal_var=sig_var,
            signal_count=sig_n,
            recent_signal_mean=rec_mean,
            recent_signal_count=rec_n,
        )

    def _load_state(self, key: str) -> dict:
        """Return the raw baseline accumulator dict for *key* (defaults if absent)."""
        row = self._conn.execute(
            "SELECT time_histogram FROM device_profiles WHERE key = ?", (key,)
        ).fetchone()
        if row is None or row["time_histogram"] is None:
            return dict(self._EMPTY_STATE)
        try:
            state = json.loads(row["time_histogram"])
        except (ValueError, TypeError):
            return dict(self._EMPTY_STATE)
        for k, v in self._EMPTY_STATE.items():
            state.setdefault(k, v)
        return state

    def get_profile(self, key: str) -> Optional[DeviceProfile]:
        row = self._conn.execute(
            "SELECT * FROM device_profiles WHERE key = ?", (key,)
        ).fetchone()
        if row is None:
            return None
        hour_mask, sig_count = 0, 0
        rec_count, rec_mean = 0, None
        raw = row["time_histogram"]
        if raw:
            try:
                state = json.loads(raw)
                hour_mask = int(state.get("hour_mask", 0))
                sig_count = int(state.get("sig_n", 0))
                rec_count = int(state.get("rec_n", 0))
                if rec_count > 0:
                    rec_mean = float(state.get("rec_ema", 0.0))
            except (ValueError, TypeError):
                pass
        return DeviceProfile(
            key=row["key"],
            first_seen=_parse(row["first_seen"]),
            last_seen=_parse(row["last_seen"]),
            observation_count=row["observation_count"],
            manufacturer=row["manufacturer"] or "",
            device_type=row["device_type"] or "",
            mac_type=row["mac_type"] or "static",
            hour_mask=hour_mask,
            signal_mean=row["signal_mean"],
            signal_var=row["signal_var"],
            signal_count=sig_count,
            recent_signal_mean=rec_mean,
            recent_signal_count=rec_count,
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
