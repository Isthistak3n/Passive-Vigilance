"""Node-relative geometry for the air picture (pure, no I/O).

All functions are side-effect free and unit-tested against synthetic tracks — the
foundation for the aircraft/UAS persistence scorer (see
docs/design-aircraft-of-interest.md). Distances are nautical miles, angles degrees.

A "track" here is the orchestrator's ``positions`` list: ``{lat, lon, altitude,
timestamp}`` dicts, possibly interleaved with ``{"gap": True, ...}`` sentinels that
mark a returning airframe's absence (P6). Geometry never spans a gap — a gap ends
one segment and starts the next, so a turn or dwell is never fabricated across the
time the aircraft was gone.
"""
from __future__ import annotations

import math
import os
from typing import Optional

# Earth radius in nautical miles; feet per nautical mile.
_R_NM = 3440.065
_FT_PER_NM = 6076.12


def resolve_reference(gps_fix: Optional[dict], env=None) -> Optional[tuple]:
    """Resolve the node reference position all air geometry keys off.

    Priority: a manually pinned home (``AIR_HOME_LAT`` / ``AIR_HOME_LON`` — set via
    the GUI override, persisted to env) wins; otherwise the live GPS fix's lat/lon;
    otherwise ``None`` (geometry is unavailable and the scorer should no-op).
    """
    env = os.environ if env is None else env
    try:
        hlat, hlon = env.get("AIR_HOME_LAT"), env.get("AIR_HOME_LON")
        if hlat not in (None, "") and hlon not in (None, ""):
            return (float(hlat), float(hlon))
    except (TypeError, ValueError):
        pass
    if gps_fix:
        lat, lon = gps_fix.get("lat"), gps_fix.get("lon")
        if lat is not None and lon is not None:
            try:
                return (float(lat), float(lon))
            except (TypeError, ValueError):
                return None
    return None


def haversine_nm(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle ground distance between two points, in nautical miles."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2) ** 2
    return 2 * _R_NM * math.asin(min(1.0, math.sqrt(a)))


def slant_range_nm(ref_lat: float, ref_lon: float,
                   lat: float, lon: float, alt_ft) -> float:
    """3-D slant range from the node to an aircraft, in nautical miles.

    Folds altitude in so a high airliner directly overhead reads *far*, not close —
    the node is assumed near ground level (its own altitude is negligible vs the
    aircraft's for this discrimination).
    """
    ground = haversine_nm(ref_lat, ref_lon, lat, lon)
    try:
        alt_nm = (float(alt_ft) if alt_ft is not None else 0.0) / _FT_PER_NM
    except (TypeError, ValueError):
        alt_nm = 0.0
    return math.hypot(ground, alt_nm)


def bearing_deg(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Initial great-circle bearing from point 1 to point 2, degrees [0, 360)."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dlam = math.radians(lon2 - lon1)
    y = math.sin(dlam) * math.cos(phi2)
    x = math.cos(phi1) * math.sin(phi2) - math.sin(phi1) * math.cos(phi2) * math.cos(dlam)
    return (math.degrees(math.atan2(y, x)) + 360.0) % 360.0


def cumulative_heading_change(points: list) -> float:
    """Total absolute heading change (degrees) along a run of ``(lat, lon)`` points.

    Each turn is normalised to [-180, 180] before summing, so a full circle reads
    ~360° regardless of turn direction and a straight transit reads ~0°. Needs 3+
    points (2 legs) to register any turn.
    """
    pts = [p for p in points if p is not None]
    if len(pts) < 3:
        return 0.0
    bearings = [bearing_deg(pts[i][0], pts[i][1], pts[i + 1][0], pts[i + 1][1])
                for i in range(len(pts) - 1)]
    total = 0.0
    for i in range(len(bearings) - 1):
        delta = (bearings[i + 1] - bearings[i] + 180.0) % 360.0 - 180.0
        total += abs(delta)
    return total


def track_segments(positions: list) -> list:
    """Split a ``positions`` track into segments at gap sentinels.

    Returns a list of segments, each a list of positioned points with parsed time:
    ``{"lat", "lon", "altitude", "ts"}`` where ``ts`` is an epoch-seconds float (or
    ``None`` if the timestamp was missing/unparseable). Gap sentinels and points
    without coordinates are dropped, ending the current segment at each gap.
    """
    from datetime import datetime

    segments: list = []
    current: list = []
    for pos in positions or []:
        if pos.get("gap"):
            if current:
                segments.append(current)
                current = []
            continue
        lat, lon = pos.get("lat"), pos.get("lon")
        if lat is None or lon is None:
            continue
        ts = None
        raw = pos.get("timestamp")
        if raw:
            try:
                ts = datetime.fromisoformat(raw).timestamp()
            except (ValueError, TypeError):
                ts = None
        current.append({"lat": float(lat), "lon": float(lon),
                        "altitude": pos.get("altitude"), "ts": ts})
    if current:
        segments.append(current)
    return segments
