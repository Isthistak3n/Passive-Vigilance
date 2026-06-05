"""FixedScoring — baseline-deviation scoring for a stationary node.

Phase 1 flagged pure **novelty** — a device not in the frozen baseline that
appears and persists (design 6 "newcomer that stays").

Phase 2 adds graduated pattern-of-life deviation (design 5.4):

- **off-schedule** — a *known baseline* device seen in an hour-of-day that was
  never part of its baseline pattern. This is a new flag class: it lets a
  device that *was* in the baseline flag when it behaves abnormally.
- Severity is graduated (Option B): a device flags if it is novel OR has a
  known-device deviation; each additional active signal escalates the level
  (suspicious -> likely -> high) via the existing persistence thresholds.
  Novelty alone is now a *low* (suspicious) flag — it still flags (no
  regression), just no longer hardcoded to high.

Also populated during learning (no trigger yet — for Phase 2.5): per-device
``signal_mean`` / ``signal_var`` (RSSI stats). Explicitly NOT in this phase:
abnormal-dwell / session-state, the signal-trend/approaching trigger,
day-of-week patterning, adaptation, egregious-during-baseline, WiGLE. There is
also **no location gate** (that gate is the #50 bug for fixed nodes).

Keying (design 5.3): stable MACs are keyed by MAC; randomized MACs are keyed by
their probe-SSID fingerprint via :func:`modules.mac_utils.group_by_fingerprint`.

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
from modules.persistence import DetectionEvent, PersistenceEngine
from modules.scoring_engine import ScoringEngine

logger = logging.getLogger(__name__)

# A novel device must be seen at least this many times before it is flagged —
# "appears and persists", not a single ephemeral probe. Matches the existing
# 2-observation minimum convention in PersistenceEngine.
_MIN_NOVELTY_OBSERVATIONS = 2

# Severity mapping (Option B) — surfaced as constants for later calibration.
# A device that flags gets a base score for its first active signal; each
# additional active signal escalates. Levels come from the existing persistence
# thresholds (>=0.9 high, >=0.7 likely, else suspicious) — not reinvented here.
_BASE_SCORE = 0.5
_SIGNAL_INCREMENT = 0.2


def _coerce_signal(value) -> Optional[float]:
    """Return *value* as float, or None if missing/non-numeric (skip, don't crash)."""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _any_active(signals: dict) -> bool:
    return any(v > 0 for v in signals.values())


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
        """Profile each device; flag novel and off-schedule devices once frozen.

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
            signal = _coerce_signal(device.get("last_signal"))

            # Baseline stats (hour mask + RSSI mean/var) accumulate ONLY while
            # learning, so the frozen baseline is never moved by post-freeze
            # sightings. Recency (last_seen / count) advances either way.
            profile = self._store.upsert(
                key, now, manuf, dtype, mac_type,
                last_signal=signal, accumulate_baseline=learning,
            )

            if learning:
                continue

            signals = self._signals(profile, now, freeze_time)
            if not _any_active(signals):
                continue
            score, level = self._combine(signals)
            events.append(self._make_event(device, profile, now, signals, score, level))

        if events:
            logger.info("FixedScoring: %d device(s) flagged (novel/off-schedule)", len(events))
        return events

    # ------------------------------------------------------------------
    # Deviation signals + severity (Option B)
    # ------------------------------------------------------------------

    def _signals(self, profile: DeviceProfile, now: datetime, freeze_time: datetime) -> dict:
        """Return the active deviation signals for one post-freeze sighting.

        ``abnormal_dwell`` and ``approaching`` are reserved-but-inactive this
        phase (always 0.0) — populated columns exist, no trigger yet.
        """
        is_novel = profile.first_seen > freeze_time
        novelty = (
            1.0 if (is_novel and profile.observation_count >= _MIN_NOVELTY_OBSERVATIONS)
            else 0.0
        )
        # Off-schedule applies ONLY to known (baseline) devices — a novel device
        # has no baseline schedule to deviate from. Flags when this hour-of-day
        # was never seen during baseline.
        off_schedule = 0.0
        if (not is_novel) and profile.hour_mask and not (profile.hour_mask & (1 << now.hour)):
            off_schedule = 1.0
        return {
            "novelty": novelty,
            "off_schedule": off_schedule,
            "abnormal_dwell": 0.0,
            "approaching": 0.0,
        }

    @staticmethod
    def _combine(signals: dict) -> tuple:
        """Map active signals to ``(score, alert_level)`` — graduated severity.

        Base score for the first active signal, +increment per additional one,
        capped at 1.0; level via the existing persistence thresholds.
        """
        active = sum(1 for v in signals.values() if v > 0)
        if active == 0:
            return 0.0, None
        score = min(1.0, _BASE_SCORE + _SIGNAL_INCREMENT * (active - 1))
        return round(score, 4), PersistenceEngine._make_alert_level(score)

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
        score_breakdown: dict,
        score: float,
        alert_level: str,
    ) -> DetectionEvent:
        mac = normalize_mac(device.get("macaddr", ""))
        return DetectionEvent(
            mac=mac,
            score=score,
            score_breakdown=score_breakdown,
            first_seen=profile.first_seen,
            last_seen=profile.last_seen,
            locations=[],  # no location gate / clustering in fixed mode
            observation_count=profile.observation_count,
            manufacturer=profile.manufacturer,
            device_type=profile.device_type,
            alert_level=alert_level,
            mac_type=get_mac_type(mac),
        )

    def close(self) -> None:
        self._store.close()
