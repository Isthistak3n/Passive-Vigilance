"""FixedScoring — baseline-deviation scoring for a stationary node.

Phase 1 scope is deliberately minimal: the single highest-value deviation
signal, **novelty** — a device that was not part of the established baseline and
now appears and *persists* (design 5.4 / 6 "newcomer that stays"). This alone
makes a fixed node useful and resolves the #50 empty-GUI problem.

Explicitly NOT in this phase: egregious-during-baseline (5.2), off-schedule /
abnormal-dwell / signal-trend (5.4), adaptation (5.5), GPS-movement checks,
WiGLE. There is also **no location gate** — that gate is exactly the #50 bug for
fixed nodes (a stationary sensor only ever produces one location cluster).

Keying (design 5.3): stable MACs are keyed by MAC; randomized MACs are keyed by
their probe-SSID fingerprint via :func:`modules.mac_utils.group_by_fingerprint`,
so a logical device's rotating MACs map to one profile.

Durability (design 5.1) lives in :class:`modules.baseline_store.BaselineStore`.
"""

import logging
import os
from datetime import datetime, timezone
from typing import Callable, Optional

from modules.baseline_store import BaselineStore, DeviceProfile, _DEFAULT_DB_PATH
from modules.mac_utils import (
    get_mac_type,
    group_by_fingerprint,
    is_randomized_mac,
    normalize_mac,
)
from modules.persistence import DetectionEvent
from modules.scoring_engine import ScoringEngine

logger = logging.getLogger(__name__)

# A novel device must be seen at least this many times before it is flagged —
# "appears and persists", not a single ephemeral probe. Matches the existing
# 2-observation minimum convention in PersistenceEngine.
_MIN_NOVELTY_OBSERVATIONS = 2


class FixedScoring(ScoringEngine):
    """Baseline-deviation scorer (novelty only, Phase 1).

    Args:
        store:          Pre-built :class:`BaselineStore` (mainly for tests).
        db_path:        SQLite path; defaults to ``BASELINE_DB_PATH`` env or the
                        repo-relative default.
        baseline_hours: Learning window; defaults to ``FIXED_BASELINE_HOURS``
                        env (72).
        clock:          Callable returning the current UTC datetime (testing
                        hook); defaults to ``datetime.now(timezone.utc)``.
    """

    def __init__(
        self,
        store: Optional[BaselineStore] = None,
        db_path: Optional[str] = None,
        baseline_hours: Optional[float] = None,
        clock: Optional[Callable[[], datetime]] = None,
    ) -> None:
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        if store is not None:
            self._store = store
        else:
            path = db_path or os.getenv("BASELINE_DB_PATH") or _DEFAULT_DB_PATH
            hours = (
                baseline_hours
                if baseline_hours is not None
                else float(os.getenv("FIXED_BASELINE_HOURS", "72"))
            )
            self._store = BaselineStore(path, hours, now=self._clock())
        logger.info(
            "FixedScoring active — learning until %s (now %s)",
            self._store.freeze_time.isoformat(), self._clock().isoformat(),
        )

    # ------------------------------------------------------------------
    # Keying (design 5.3)
    # ------------------------------------------------------------------

    @staticmethod
    def _device_key(device: dict) -> Optional[str]:
        """Return the pattern-of-life key for *device*, or None if unusable.

        Stable MAC  -> ``mac:<normalized>``.
        Randomized MAC with probe SSIDs -> ``fp:<sorted|joined probe ssids>``.
        Randomized MAC without probe SSIDs -> falls back to ``mac:<normalized>``
        (it cannot be fingerprinted; a rotating MAC seen once never reaches the
        persistence minimum, so this naturally suppresses ephemeral randoms).
        """
        raw = device.get("macaddr", "")
        if not raw:
            return None
        mac = normalize_mac(raw)
        if is_randomized_mac(mac):
            fps = group_by_fingerprint([device])
            if fps and fps[0].probe_ssids:
                return "fp:" + "|".join(fps[0].probe_ssids)
            return "mac:" + mac
        return "mac:" + mac

    # ------------------------------------------------------------------
    # ScoringEngine interface
    # ------------------------------------------------------------------

    def update(
        self,
        devices: list,
        *,
        gps_fix: Optional[dict] = None,
    ) -> list:
        """Profile each device; flag novel-persistent devices once frozen.

        ``gps_fix`` is accepted for interface compatibility but unused — fixed
        mode has no location gate (that gate is the #50 bug for fixed nodes).
        """
        now = self._clock()
        learning = self._store.is_learning(now)
        freeze_time = self._store.freeze_time

        events: list = []
        for device in devices:
            key = self._device_key(device)
            if key is None:
                continue
            manuf = device.get("manuf", "") or ""
            dtype = device.get("type", "") or ""
            mac_type = get_mac_type(normalize_mac(device.get("macaddr", "")))
            profile = self._store.upsert(key, now, manuf, dtype, mac_type)

            # During the learning window we only build the baseline; we do not
            # flag (egregious-during-baseline is out of scope for Phase 1).
            if learning:
                continue

            # Novelty: first seen AFTER the baseline froze, and it has persisted.
            is_novel = profile.first_seen > freeze_time
            if is_novel and profile.observation_count >= _MIN_NOVELTY_OBSERVATIONS:
                events.append(self._make_event(device, profile, now))

        if events:
            logger.info("FixedScoring: %d novel-persistent device(s) flagged", len(events))
        return events

    def status(self) -> dict:
        now = self._clock()
        learning = self._store.is_learning(now)
        return {
            "mode": "fixed",
            "learning": learning,
            "learning_start": self._store.learning_start.isoformat(),
            "freeze_time": self._store.freeze_time.isoformat(),
            "baseline_devices": self._store.baseline_count(),
            "total_profiles": self._store.profile_count(),
        }

    # ------------------------------------------------------------------
    # Event construction — shaped identically to the mobile path
    # ------------------------------------------------------------------

    def _make_event(
        self,
        device: dict,
        profile: DeviceProfile,
        now: datetime,
    ) -> DetectionEvent:
        mac = normalize_mac(device.get("macaddr", ""))
        return DetectionEvent(
            mac=mac,
            score=1.0,
            score_breakdown={"novelty": 1.0},
            first_seen=profile.first_seen,
            last_seen=profile.last_seen,
            locations=[],  # no location gate / clustering in fixed mode
            observation_count=profile.observation_count,
            manufacturer=profile.manufacturer,
            device_type=profile.device_type,
            alert_level="high",
            mac_type=get_mac_type(mac),
        )

    def close(self) -> None:
        self._store.close()
