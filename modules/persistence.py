"""Persistence engine — detects persistent or suspicious devices over time.

Inspired by Chasing-Your-Tail. Scores each device across multiple time
windows using temporal, location, frequency, and signal strength criteria
to produce a surveillance confidence score.
"""

import logging
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from math import atan2, cos, radians, sin, sqrt
from typing import Optional

from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

# Scoring weights — must sum to 1.0
_W_TEMPORAL  = 0.35
_W_LOCATION  = 0.35
_W_FREQUENCY = 0.20
_W_SIGNAL    = 0.10

# Signal normalisation bounds (dBm)
_SIGNAL_WEAK   = -85.0
_SIGNAL_STRONG = -40.0


@dataclass
class DetectionEvent:
    """A device that has crossed the persistence alert threshold."""

    mac: str
    score: float
    score_breakdown: dict       # {temporal, location, frequency, signal}
    first_seen: datetime
    last_seen: datetime
    locations: list             # GPS cluster centroids: [{lat, lon, count}]
    observation_count: int
    manufacturer: str
    device_type: str
    alert_level: str            # "suspicious" (0.5-0.7) | "likely" (0.7-0.9) | "high" (0.9+)
    mac_type: str = "static"    # "static" | "randomized"


class PersistenceEngine:
    """Multi-window device persistence scorer for counter-surveillance detection.

    Call :meth:`update` on every ``poll_devices()`` result.  Devices whose
    weighted score crosses *alert_threshold* are returned as
    :class:`DetectionEvent` objects.

    Scoring weights:
    - temporal  (35%) — fraction of time windows in which device was seen
    - location  (35%) — number of distinct GPS clusters (100 m threshold)
    - frequency (20%) — observation density within the largest window
    - signal    (10%) — average signal strength (strong → nearby)
    """

    def __init__(
        self,
        window_minutes: Optional[list] = None,
        alert_threshold: Optional[float] = None,
        min_locations: Optional[int] = None,
        poll_interval_seconds: Optional[int] = None,
        handle_randomized: Optional[bool] = None,
    ) -> None:
        self._windows = window_minutes if window_minutes is not None else [5, 10, 15, 20]
        self._threshold = float(
            alert_threshold
            if alert_threshold is not None
            else os.getenv("PERSISTENCE_ALERT_THRESHOLD", "0.7")
        )
        self._min_locations = int(
            min_locations
            if min_locations is not None
            else os.getenv("PERSISTENCE_MIN_LOCATIONS", "2")
        )
        self._poll_interval = int(
            poll_interval_seconds
            if poll_interval_seconds is not None
            else os.getenv("PERSISTENCE_POLL_INTERVAL_SECONDS", "30")
        )
        self._handle_randomized = bool(
            handle_randomized
            if handle_randomized is not None
            else os.getenv("HANDLE_MAC_RANDOMIZATION", "true").lower() == "true"
        )
        # {mac: [{"timestamp", "gps_lat", "gps_lon", "signal", "manuf", "type", "name"}]}
        self._observations: dict = {}
        self._purge_counter: int = 0

    # ------------------------------------------------------------------
    # Main update cycle
    # ------------------------------------------------------------------

    def update(
        self,
        devices: list,
        gps_fix: Optional[dict] = None,
    ) -> list:
        """Ingest a ``poll_devices()`` result and return :class:`DetectionEvent` objects.

        Stores each device observation, purges old data, then scores every
        tracked device.  Devices above *alert_threshold* that also satisfy the
        GPS location gate are returned as :class:`DetectionEvent` objects.

        Args:
            devices:  Device dicts from :meth:`~modules.kismet.KismetModule.poll_devices`.
            gps_fix:  Current GPS fix dict from :meth:`~modules.gps.GPSModule.get_fix`,
                      or ``None`` when no fix is available.

        Returns:
            List of :class:`DetectionEvent` — may be empty.
        """
        now = datetime.now(timezone.utc)
        self._purge_counter += 1
        if self._purge_counter % 10 == 0:
            self.purge_old_observations()

        lat = gps_fix.get("lat") if gps_fix else None
        lon = gps_fix.get("lon") if gps_fix else None

        for device in devices:
            mac = device.get("macaddr", "")
            if not mac:
                continue
            self._observations.setdefault(mac, []).append({
                "timestamp": now,
                "gps_lat":   lat,
                "gps_lon":   lon,
                "signal":    device.get("last_signal"),
                "manuf":     device.get("manuf", ""),
                "type":      device.get("type", ""),
                "name":      device.get("name", ""),
            })

        events = []
        for mac, observations in self._observations.items():
            components = self._compute_score_components(mac)
            score = self._components_to_score(components)
            if score < self._threshold:
                continue

            # Location gate: only apply when GPS data has been collected
            obs_with_gps = [o for o in observations if o.get("gps_lat") is not None]
            if obs_with_gps:
                clusters = self.cluster_locations(obs_with_gps)
                if len(clusters) < self._min_locations:
                    continue

            events.append(self._make_event(mac, score, components, observations))

        if events:
            logger.info(
                "PersistenceEngine: %d device(s) above threshold (%.2f)",
                len(events), self._threshold,
            )
        return events

    # ------------------------------------------------------------------
    # Scoring
    # ------------------------------------------------------------------

    def score_device(self, mac: str) -> float:
        """Return weighted persistence score 0.0–1.0 for *mac*."""
        return self._components_to_score(self._compute_score_components(mac))

    def _compute_score_components(self, mac: str) -> dict:
        """Compute and return the four raw score components for *mac*."""
        observations = self._observations.get(mac, [])

        # Require at least 2 sightings to avoid false-positive on first encounter
        if len(observations) < 2:
            return {"temporal": 0.0, "location": 0.0, "frequency": 0.0, "signal": 0.0}

        now = datetime.now(timezone.utc)

        # Temporal — fraction of configured windows in which device was seen
        windows_seen = sum(
            1 for w in self._windows
            if any(o["timestamp"] >= now - timedelta(minutes=w) for o in observations)
        )
        temporal = windows_seen / len(self._windows)

        # Location — number of distinct GPS clusters
        obs_with_gps = [o for o in observations if o.get("gps_lat") is not None]
        if obs_with_gps:
            n_clusters = len(self.cluster_locations(obs_with_gps))
            if n_clusters <= 1:
                location = 0.0
            elif n_clusters == 2:
                location = 0.5
            else:
                location = 1.0
        else:
            location = 0.0

        # Frequency — observation density within the largest window
        max_window = max(self._windows)
        recent = [
            o for o in observations
            if o["timestamp"] >= now - timedelta(minutes=max_window)
        ]
        expected = (max_window * 60) / max(self._poll_interval, 1)
        frequency = min(len(recent) / max(expected, 1), 1.0)

        # Signal — average dBm of recent observations normalised to [0.0, 1.0]
        signals = [
            o["signal"] for o in observations[-20:]
            if o.get("signal") is not None
        ]
        if signals:
            avg = sum(signals) / len(signals)
            signal = max(0.0, min(1.0, (avg - _SIGNAL_WEAK) / (_SIGNAL_STRONG - _SIGNAL_WEAK)))
        else:
            signal = 0.0

        return {
            "temporal":  round(temporal,  4),
            "location":  round(location,  4),
            "frequency": round(frequency, 4),
            "signal":    round(signal,    4),
        }

    @staticmethod
    def _components_to_score(components: dict) -> float:
        score = (
            _W_TEMPORAL  * components["temporal"] +
            _W_LOCATION  * components["location"] +
            _W_FREQUENCY * components["frequency"] +
            _W_SIGNAL    * components["signal"]
        )
        return max(0.0, min(1.0, round(score, 4)))

    @staticmethod
    def _make_alert_level(score: float) -> str:
        """Map a 0.0–1.0 score to a human-readable alert level."""
        if score >= 0.9:
            return "high"
        elif score >= 0.7:
            return "likely"
        return "suspicious"

    def _make_event(
        self,
        mac: str,
        score: float,
        components: dict,
        observations: list,
    ) -> DetectionEvent:
        from modules.mac_utils import get_mac_type
        first_seen = min(o["timestamp"] for o in observations)
        last_seen  = max(o["timestamp"] for o in observations)
        obs_gps    = [o for o in observations if o.get("gps_lat") is not None]
        clusters   = self.cluster_locations(obs_gps) if obs_gps else []
        manuf = next((o["manuf"] for o in reversed(observations) if o.get("manuf")), "")
        dtype = next((o["type"]  for o in reversed(observations) if o.get("type")),  "")
        return DetectionEvent(
            mac=mac,
            score=score,
            score_breakdown=components,
            first_seen=first_seen,
            last_seen=last_seen,
            locations=clusters,
            observation_count=len(observations),
            manufacturer=manuf,
            device_type=dtype,
            alert_level=self._make_alert_level(score),
            mac_type=get_mac_type(mac),
        )

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    def get_device_history(self, mac: str) -> list:
        """Return full observation history list for *mac*."""
        return list(self._observations.get(mac, []))

    def get_active_devices(self, window_minutes: int = 20) -> list:
        """Return all devices seen within the last *window_minutes* with scores."""
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=window_minutes)
        results = []
        for mac, observations in self._observations.items():
            if any(o["timestamp"] >= cutoff for o in observations):
                results.append({
                    "mac":               mac,
                    "score":             self.score_device(mac),
                    "observation_count": len(observations),
                })
        return results

    def get_suspicious_devices(self, threshold: Optional[float] = None) -> list:
        """Return all devices above *threshold* with full score breakdown."""
        if threshold is None:
            threshold = self._threshold
        results = []
        for mac, observations in self._observations.items():
            components = self._compute_score_components(mac)
            score = self._components_to_score(components)
            if score >= threshold:
                results.append({
                    "mac":               mac,
                    "score":             score,
                    "score_breakdown":   components,
                    "observation_count": len(observations),
                    "alert_level":       self._make_alert_level(score),
                })
        return results

    # ------------------------------------------------------------------
    # GPS utilities
    # ------------------------------------------------------------------

    def haversine(
        self,
        lat1: float,
        lon1: float,
        lat2: float,
        lon2: float,
    ) -> float:
        """Return the great-circle distance in metres between two GPS coordinates."""
        R = 6_371_000.0
        phi1, phi2 = radians(lat1), radians(lat2)
        dphi    = radians(lat2 - lat1)
        dlambda = radians(lon2 - lon1)
        a = sin(dphi / 2) ** 2 + cos(phi1) * cos(phi2) * sin(dlambda / 2) ** 2
        return R * 2 * atan2(sqrt(a), sqrt(1.0 - a))

    def cluster_locations(
        self,
        observations: list,
        threshold_meters: float = 100.0,
    ) -> list:
        """Group GPS observations into spatial clusters.

        Uses greedy nearest-centroid assignment with a running-average centroid
        update.  Points without GPS coordinates are silently skipped.

        Returns:
            List of ``{lat, lon, count}`` cluster centroid dicts.
        """
        clusters: list = []
        for obs in observations:
            lat = obs.get("gps_lat")
            lon = obs.get("gps_lon")
            if lat is None or lon is None:
                continue
            placed = False
            for cluster in clusters:
                if self.haversine(lat, lon, cluster["lat"], cluster["lon"]) <= threshold_meters:
                    n = cluster["count"]
                    cluster["lat"]   = (cluster["lat"] * n + lat) / (n + 1)
                    cluster["lon"]   = (cluster["lon"] * n + lon) / (n + 1)
                    cluster["count"] = n + 1
                    placed = True
                    break
            if not placed:
                clusters.append({"lat": lat, "lon": lon, "count": 1})
        return clusters

    # ------------------------------------------------------------------
    # Maintenance
    # ------------------------------------------------------------------

    def purge_old_observations(self, max_age_minutes: int = 60) -> None:
        """Remove observations older than *max_age_minutes* to bound memory use.

        Called automatically at the start of each :meth:`update` cycle.
        MACs with no remaining observations are removed entirely.
        """
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=max_age_minutes)
        stale = []
        for mac, observations in self._observations.items():
            fresh = [o for o in observations if o["timestamp"] >= cutoff]
            if fresh:
                self._observations[mac] = fresh
            else:
                stale.append(mac)
        for mac in stale:
            del self._observations[mac]
        if stale:
            logger.debug("Purged %d stale MAC(s) from observation history", len(stale))

    def get_fingerprint_summary(self) -> list:
        """Return fingerprint clusters for currently tracked randomized MACs.

        Delegates to :func:`~modules.mac_utils.group_by_fingerprint` using
        the most recent observation for each randomized MAC as the probe data.

        Returns an empty list when :attr:`handle_randomized` is False or when
        no randomized MACs are currently tracked.
        """
        from modules.mac_utils import group_by_fingerprint, is_randomized_mac

        if not self._handle_randomized:
            return []

        devices = []
        for mac, obs_list in self._observations.items():
            if not is_randomized_mac(mac) or not obs_list:
                continue
            latest = obs_list[-1]
            devices.append({
                "macaddr":    mac,
                "last_signal": latest.get("signal"),
                "name":       latest.get("name", ""),
            })

        return group_by_fingerprint(devices) if devices else []

    def stats(self) -> dict:
        """Return summary statistics for the current observation window."""
        all_ts = [
            o["timestamp"]
            for obs_list in self._observations.values()
            for o in obs_list
n        ]
        suspicious = sum(
            1 for mac in self._observations
            if self.score_device(mac) >= self._threshold
        )
        return {
            "total_devices_tracked": len(self._observations),
            "suspicious_count":      suspicious,
            "oldest_observation":    min(all_ts) if all_ts else None,
            "newest_observation":    max(all_ts) if all_ts else None,
        }
