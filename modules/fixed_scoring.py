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

Phase 2.6 adds the **egregious-during-baseline** safety net (design 5.2): the
learning window no longer fully suppresses alerting. A device physically in the
operator's immediate space (very strong live signal), or already trending
stronger, still flags WHILE learning — so an already-present surveillance device
is not silently baked into "normal". The device is still learned into the
baseline; the alert is the safety net, not a substitute for a clean baseline.

Explicitly NOT in this phase: abnormal-dwell / session-state, day-of-week
patterning, adaptation, WiGLE. There is also **no location gate** (that gate is
the #50 bug for fixed nodes).

Keying (design 5.3): stable MACs are keyed by MAC; randomized MACs are keyed by a
strong content fingerprint — ``wifi-fp:`` (probed SSIDs + IE set, via
:mod:`modules.wifi_fingerprint`) or ``ble-fp:`` (vendor/services/name, via
:mod:`modules.ble_fingerprint`) — so rotating addresses collapse to one identity.
Randomized devices without a strong fingerprint fall back to ``mac:`` and are not
novelty-eligible.

Durability (design 5.1) lives in :class:`modules.baseline_store.BaselineStore`.
"""

import logging
import os
from datetime import datetime, timezone
from typing import Callable, Optional

from modules import device_identity
from modules import promotion_policy
from modules.baseline_store import BaselineStore, DeviceProfile, _DEFAULT_DB_PATH
from modules.mac_utils import (
    get_mac_type,
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

# Off-schedule activation guard: a device's off-schedule signal does NOT activate
# until that device's own hour-of-day mask spans at least this many DISTINCT
# hours — i.e. the baseline is rich enough to define a schedule. Below it,
# off-schedule contributes 0.0 ("insufficient baseline to judge", not "on
# schedule"). Per-device, env-overridable. Live testing showed a 1-distinct-hour
# baseline flags ~100% of known devices on any hour rollover; the default 12
# requires roughly half a day of distinct hours before the signal is trusted.
OFF_SCHEDULE_MIN_BASELINE_HOURS = 12

# Approaching trigger (Phase 2.5): a KNOWN device whose smoothed recent signal is
# meaningfully STRONGER (numerically higher dBm — less negative) than its frozen
# baseline mean is physically closing distance. RSSI is jittery, so three guards
# (each env-overridable, like the off-schedule guard above) keep it from firing
# on noise or thin data. Defaults below; calibrate against real RSSI on chase.
APPROACHING_MIN_BASELINE_SAMPLES = 10   # trust the baseline mean only past this
APPROACHING_MIN_RECENT_SAMPLES = 5      # trust the recent average only past this
APPROACHING_SIGMA_MARGIN = 2.0          # rise must exceed this many baseline std devs
APPROACHING_MIN_DB_MARGIN = 6.0         # ...and at least this many dB (absolute floor)

# Egregious-during-baseline (design 5.2, Phase 2.6): the learning window does NOT
# fully suppress alerting. A live signal at or above this strength (dBm; less
# negative = physically closer) flags as egregiously close even while learning —
# a device in the operator's immediate space, not street traffic. Env-overridable;
# the key knob the on-chase test calibrates so it flags a deliberately-close
# device without flooding on ordinary nearby traffic.
EGREGIOUS_SIGNAL_DBM = -45.0

# Environment-density presets for the egregious threshold (post-soak calibration).
# A DENSE node (apartment block, many close devices) needs a STRICTER, closer bar
# or it floods; a SPARSE/rural node (large yard, few neighbours) can use a more
# sensitive, farther bar because any close device is notable. NODE_DENSITY picks
# the default; an explicit EGREGIOUS_SIGNAL_DBM overrides it.
_EGREGIOUS_DENSITY_PRESETS = {"dense": -30.0, "suburban": -40.0, "rural": -50.0}

# Egregious threshold for BLE is a SEPARATE, modality-specific knob — the density
# presets above are Wi-Fi-calibrated and far too strict for Bluetooth. A BLE radio
# reports much lower RSSI than Wi-Fi for the same distance, and BLE is inherently a
# short-range (~10 m) proximity signal, so any reasonably strong advert is already
# "in the operator's space". On chase the ambient BLE floor clusters around -55 dBm
# (the persistent neighbour-beacon mass), while genuinely-close adverts reach
# -32..-45; -50 separates the two. Not density-keyed (BLE's short range makes the
# ambient floor roughly density-independent). Override with EGREGIOUS_BLE_SIGNAL_DBM;
# on a node where the operator's own device advertises at low TX power this may need
# calibration against that device's measured close-range RSSI.
EGREGIOUS_BLE_SIGNAL_DBM = -50.0


def _coerce_signal(value) -> Optional[float]:
    """Return *value* as float, or None if it should be skipped.

    Skipped: a missing reading, a non-numeric reading, OR a zero. Kismet reports
    0 when it tracked a device but never got a real signal sample, so 0 is a
    placeholder, not a measurement — it is treated identically to a missing
    reading everywhere signal is consumed (never counted into any statistic).
    """
    if value is None:
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if f == 0.0:
        return None
    return f


def _any_active(signals: dict) -> bool:
    return any(v > 0 for v in signals.values())


def _is_access_point(device_type) -> bool:
    """True if Kismet classifies the device as a Wi-Fi access point.

    Matches the standalone ``AP`` token in the type string, so it catches
    ``Wi-Fi AP`` and ``Wi-Fi WDS AP`` but deliberately NOT ``Wi-Fi Bridged``,
    ``Wi-Fi WDS``, ``Wi-Fi Ad-Hoc`` or ``Wi-Fi Client`` — a narrow infrastructure
    filter, used only to make access points ineligible for the approaching
    signal (an AP does not move, so its signal variation is environmental).
    """
    return "ap" in (device_type or "").lower().split()


def _is_bluetooth(device_type) -> bool:
    """True if Kismet classifies the device as Bluetooth/BLE.

    Used to select the modality-specific egregious threshold: BLE RSSI runs much
    lower than Wi-Fi, so the Wi-Fi-calibrated density presets would silence it.
    """
    dt = (device_type or "").lower()
    return "btle" in dt or "bluetooth" in dt


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
        # Off-schedule activation guard (per-device distinct-hour threshold).
        self._off_schedule_min_hours = int(
            os.getenv("OFF_SCHEDULE_MIN_BASELINE_HOURS", str(OFF_SCHEDULE_MIN_BASELINE_HOURS))
        )
        # Approaching trigger guards (per-device).
        self._approaching_min_baseline_samples = int(
            os.getenv("APPROACHING_MIN_BASELINE_SAMPLES", str(APPROACHING_MIN_BASELINE_SAMPLES))
        )
        self._approaching_min_recent_samples = int(
            os.getenv("APPROACHING_MIN_RECENT_SAMPLES", str(APPROACHING_MIN_RECENT_SAMPLES))
        )
        self._approaching_sigma_margin = float(
            os.getenv("APPROACHING_SIGMA_MARGIN", str(APPROACHING_SIGMA_MARGIN))
        )
        self._approaching_min_db_margin = float(
            os.getenv("APPROACHING_MIN_DB_MARGIN", str(APPROACHING_MIN_DB_MARGIN))
        )
        # Egregious-during-baseline strength threshold (design 5.2). An explicit
        # EGREGIOUS_SIGNAL_DBM wins; otherwise it follows the node's environment
        # density (NODE_DENSITY: dense | suburban | rural).
        _egr = os.getenv("EGREGIOUS_SIGNAL_DBM")
        if _egr is not None and _egr.strip():
            self._egregious_signal_dbm = float(_egr)
        else:
            _density = os.getenv("NODE_DENSITY", "suburban").strip().lower()
            self._egregious_signal_dbm = _EGREGIOUS_DENSITY_PRESETS.get(
                _density, EGREGIOUS_SIGNAL_DBM
            )
        # BLE uses a separate, modality-specific threshold (not density-keyed) — the
        # Wi-Fi presets are far too strict for Bluetooth's lower RSSI / short range.
        _egr_ble = os.getenv("EGREGIOUS_BLE_SIGNAL_DBM")
        self._egregious_ble_signal_dbm = (
            float(_egr_ble) if _egr_ble is not None and _egr_ble.strip()
            else EGREGIOUS_BLE_SIGNAL_DBM
        )
        # Distinct Wi-Fi APs suppressed from approaching that would otherwise have
        # qualified — observable so a soak can show exactly what the filter caught.
        self._approaching_excluded_aps: set = set()
        # Rolling-baseline adaptation (P3). Posture selects parameters only; "off"
        # (the default / fail-safe) leaves the baseline frozen forever — today's
        # behaviour. A recognised-but-misconfigured posture fails loud here at
        # construction (resolve_adaptation raises) rather than running unsafely.
        self._adaptation_posture, self._adaptation_params = (
            promotion_policy.resolve_adaptation(os.environ)
        )
        self._promotion_policy = promotion_policy.SustainedPresencePolicy()
        if self._adaptation_posture != "off":
            logger.info(
                "FixedScoring rolling adaptation: posture=%s params=%s",
                self._adaptation_posture, self._adaptation_params,
            )
        else:
            logger.info("FixedScoring rolling adaptation: off (baseline frozen)")
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

        Stable MAC -> ``mac:<normalized>``.
        Randomized MAC with a STRONG content fingerprint -> that fingerprint key
        (``wifi-fp:<hash>`` for a WiFi client by its probed SSIDs + IE set, or
        ``ble-fp:<hash>`` for a BLE advertiser by its vendor/services/name), so the
        device's rotating addresses collapse to one identity.
        Randomized MAC with no strong fingerprint -> falls back to
        ``mac:<normalized>``: it cannot be tracked across rotations, so it is never
        novelty-eligible (see :meth:`_signals`) and a rotating MAC seen once never
        reaches the persistence minimum — ephemeral randoms are suppressed.
        """
        raw = device.get("macaddr", "")
        if not raw:
            return None
        mac = normalize_mac(raw)
        if not is_randomized_mac(mac):
            return "mac:" + mac
        # Randomized: a strong content fingerprint (shared with mobile) or the MAC.
        return device_identity.strong_fingerprint(device) or "mac:" + mac

    @staticmethod
    def _is_ble_device(device: dict) -> bool:
        """True if the record is a BLE advertiser. Delegates to the shared resolver."""
        return device_identity.is_ble_device(device)

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
        # One commit per poll pass, not per device. At high ambient density
        # (~12.5k devices/poll, 2026-07-14) per-device commits kept the asyncio
        # poll thread inside this loop past the 2-minute systemd watchdog.
        with self._store.batch():
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
                    # Design 5.2 — learn the ordinary, but still shout about the
                    # obviously alarming so an already-present device in the
                    # operator's space isn't silently baked into the baseline.
                    egregious = self._egregious_signals(profile, signal)
                    if _any_active(egregious):
                        score, level = self._combine(egregious)
                        # A single egregious signal scores 0.5 ("suspicious"), below the
                        # WiFi paging bar — but this IS the during-learning safety net
                        # (design 5.2), so it must page anyway. force_page lets the
                        # orchestrator bypass the suspicious-display gate; the per-entity
                        # rate limiter still bounds it.
                        events.append(self._make_event(
                            device, profile, now, egregious, score, level, force_page=True))
                    continue

                signals = self._signals(profile, now, freeze_time)
                if not _any_active(signals):
                    continue
                score, level = self._combine(signals)
                events.append(self._make_event(device, profile, now, signals, score, level))

        if events:
            logger.info(
                "FixedScoring: %d device(s) flagged (%s)", len(events),
                "egregious-during-baseline" if learning else "novel/off-schedule/approaching",
            )
        return events

    def _egregious_signals(self, profile: DeviceProfile, signal: Optional[float]) -> dict:
        """Signals that flag DURING the learning window (design 5.2).

        The baseline safety net: a device physically in the operator's immediate
        space, or already closing in, must not be silently learned as "normal".
        ``signal`` is the live (coerced) reading this poll; RSSI is negative dBm,
        so "very strong / very close" means at or ABOVE the threshold (a less
        negative value). Wi-Fi APs are excluded — a nearby router is fixed
        infrastructure, not a device moving through the operator's space. BLE uses
        its own (looser) threshold because its RSSI runs much lower than Wi-Fi.
        """
        is_ble = _is_bluetooth(profile.device_type)
        threshold = (
            self._egregious_ble_signal_dbm if is_ble else self._egregious_signal_dbm
        )
        egregious_close = 0.0
        if (signal is not None
                and signal >= threshold
                and not _is_access_point(profile.device_type)):
            egregious_close = 1.0
        return {
            "egregious_close": egregious_close,
            # "Trending stronger during learning" reuses the approaching machinery.
            # is_novel is False during learning (the freeze time is in the future).
            "approaching": self._approaching(profile, is_novel=False),
        }

    # ------------------------------------------------------------------
    # Deviation signals + severity (Option B)
    # ------------------------------------------------------------------

    def _signals(self, profile: DeviceProfile, now: datetime, freeze_time: datetime) -> dict:
        """Return the active deviation signals for one post-freeze sighting.

        ``abnormal_dwell`` and ``approaching`` are reserved-but-inactive this
        phase (always 0.0) — populated columns exist, no trigger yet.
        """
        is_novel = profile.first_seen > freeze_time
        # A randomized MAC with no probe-SSID fingerprint (keyed "mac:") rotates its
        # identifier, so every rotation reads as brand-new: "novelty" on it is noise,
        # not signal. Soak #3 showed a higher observation bar only DELAYS the flood —
        # in a dense environment dozens of persistently-present neighbour devices each
        # cross any threshold and flag. Such a device cannot be de-duplicated against
        # the frozen baseline at all, so it is never novelty-eligible. A device that
        # is actually in the operator's space is still caught by the proximity signals
        # (egregious-during-baseline, approaching), which are MAC-type agnostic.
        # Novelty fires only for stable MACs and fingerprinted ("wifi-fp:"/"ble-fp:")
        # randomized devices, which ARE tracked across rotations so "not in the
        # baseline" means something.
        randomized_no_fp = (
            profile.mac_type == "randomized" and str(profile.key).startswith("mac:")
        )
        # A promoted fingerprint (P3 rolling adaptation) has earned its way into
        # the baseline by sustained presence, so it is no longer novel — until it
        # is demoted on prolonged absence, when it reads novel again. Original
        # frozen-baseline devices are never promoted (they are already not novel).
        novelty = 0.0
        if (
            is_novel
            and not profile.promoted
            and not randomized_no_fp
            and profile.observation_count >= _MIN_NOVELTY_OBSERVATIONS
        ):
            novelty = 1.0
        # Off-schedule applies ONLY to known (baseline) devices — a novel device
        # has no baseline schedule to deviate from. It also stays silent until the
        # device's baseline spans enough DISTINCT hours to define a schedule
        # (activation guard); below that it is "insufficient baseline to judge",
        # not "on schedule". Flags when this hour-of-day was never seen in baseline.
        #
        # A randomized-no-fp device is ALSO off-schedule-ineligible, for the same
        # reason it is novelty-ineligible: its identifier rotates, so its "schedule"
        # cannot be trusted across the rotation, and even a device that holds one
        # randomized MAC for a while is a weak identity we cannot confirm tomorrow.
        # The 2026-06 post-freeze read showed this is the dominant false-positive
        # source — ~50 flags/poll, 97% off-schedule on held-MAC randomized clients
        # seen in a single new hour. Flagging a device you cannot track across its
        # own rotation for "appearing in a new hour" is noise, not signal.
        off_schedule = 0.0
        if not is_novel and not randomized_no_fp:
            distinct_baseline_hours = bin(profile.hour_mask).count("1")
            if (
                distinct_baseline_hours >= self._off_schedule_min_hours
                and not (profile.hour_mask & (1 << now.hour))
            ):
                off_schedule = 1.0
        return {
            "novelty": novelty,
            "off_schedule": off_schedule,
            "abnormal_dwell": 0.0,
            "approaching": self._approaching(profile, is_novel),
        }

    def _approaching(self, profile: DeviceProfile, is_novel: bool) -> float:
        """1.0 if a KNOWN device's recent signal is meaningfully STRONGER than its
        baseline, else 0.0.

        Direction matters and is made explicit here: RSSI is negative dBm, and
        "stronger" means physically closer, i.e. a LESS negative reading, i.e. a
        numerically HIGHER value. So approaching means the recent average sits
        ABOVE (greater than) the frozen baseline mean by the margin.

        Known-device-only: a device first seen after freeze has no baseline
        signal to compare against, so it never receives an approaching
        escalation (it still flags on novelty as before).
        """
        if is_novel or profile.signal_mean is None:
            return 0.0
        if profile.signal_count < self._approaching_min_baseline_samples:
            return 0.0
        if (profile.recent_signal_mean is None
                or profile.recent_signal_count < self._approaching_min_recent_samples):
            return 0.0
        baseline_std = (profile.signal_var ** 0.5) if profile.signal_var else 0.0
        margin = max(self._approaching_sigma_margin * baseline_std,
                     self._approaching_min_db_margin)
        rise = profile.recent_signal_mean - profile.signal_mean  # >0 => stronger
        if rise < margin:
            return 0.0
        # Qualifies as approaching — but a Wi-Fi AP does not physically move, so
        # its apparent "approach" is environmental noise, not a closing device.
        # Exclude infrastructure APs (narrow filter); record what was suppressed.
        if _is_access_point(profile.device_type):
            if profile.key not in self._approaching_excluded_aps:
                self._approaching_excluded_aps.add(profile.key)
                logger.info(
                    "Approaching: suppressed Wi-Fi AP %s (type=%r) — APs are not "
                    "approaching-eligible", profile.key, profile.device_type,
                )
            return 0.0
        return 1.0

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
            "approaching_excluded_aps": len(self._approaching_excluded_aps),
            # Rolling adaptation (P3): posture + how many fingerprints have been
            # promoted into the baseline ("N learned + M promoted").
            "adaptation_posture": self._adaptation_posture,
            "promoted_devices": self._store.promoted_count(),
        }

    # ------------------------------------------------------------------
    # Rolling-baseline adaptation sweep (P3)
    # ------------------------------------------------------------------

    def _novelty_eligible(self, mac_type: str, key: str) -> bool:
        """Mirror the novelty-eligibility rule from :meth:`_signals`: a randomized
        MAC with no strong fingerprint (``mac:``-keyed) can't be tracked across
        rotations, so it is never novelty-eligible and must never be promoted."""
        return not (mac_type == "randomized" and str(key).startswith("mac:"))

    def run_adaptation_sweep(self, now: Optional[datetime] = None) -> list:
        """Run one promotion + demotion pass and return demotion event dicts.

        No-op (returns ``[]``) when the posture is ``off`` — the fail-safe, so a
        node that never opts in behaves exactly as today. The caller (orchestrator)
        owns writing the returned demotion events to ``events.jsonl``; this method
        only touches the store. Promotion is slow (sustained presence), demotion is
        fast (absent past the window) — the invariant enforced in AdaptationParams.
        """
        if self._adaptation_posture == "off" or self._adaptation_params is None:
            return []
        now = now or self._clock()
        params = self._adaptation_params
        freeze_time = self._store.freeze_time

        promoted = 0
        for cand in self._store.promotion_candidates(freeze_time):
            if not self._novelty_eligible(cand["mac_type"], cand["key"]):
                continue
            rec = promotion_policy.PresenceRecord(
                key=cand["key"], mac_type=cand["mac_type"],
                pf_first=cand["pf_first"], pf_last=cand["pf_last"],
                distinct_days=cand["distinct_days"],
                adapt_hour_mask=cand["adapt_hour_mask"],
                observation_count=cand["observation_count"], now=now,
            )
            if self._promotion_policy.should_promote(rec, params):
                self._store.promote(cand["key"], now)
                promoted += 1

        events = []
        demote_after_s = params.demote_after.total_seconds()
        for prof in self._store.promoted_profiles():
            absence_s = (now - prof["last_seen"]).total_seconds()
            if absence_s <= demote_after_s:
                continue
            self._store.demote(prof["key"])
            events.append({
                "event_type": "baseline_demotion",
                "fingerprint": prof["key"],
                "device_type": prof["device_type"],
                "manufacturer": prof["manufacturer"],
                "promotion_ts": prof["promotion_ts"].isoformat() if prof["promotion_ts"] else None,
                "demotion_ts": now.isoformat(),
                "absence_seconds": round(absence_s),
                "reason": "absent_past_demotion_window",
            })

        if promoted or events:
            logger.info(
                "Adaptation sweep: promoted %d, demoted %d (posture=%s)",
                promoted, len(events), self._adaptation_posture,
            )
        return events

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
        force_page: bool = False,
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
            ssid=device.get("name", ""),
            fingerprint=str(profile.key),
            fingerprint_label=device_identity.fingerprint_label(device),
            force_page=force_page,
        )

    def close(self) -> None:
        self._store.close()
