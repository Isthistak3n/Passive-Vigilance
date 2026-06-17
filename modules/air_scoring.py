"""Persistence scoring for the air picture (pure, no I/O).

Mirrors the mobile WiFi/BT persistence model (modules/persistence.py) for aircraft
and UAS: a transit "burns by" and scores ~0; an aircraft that loiters/orbits the
node, or returns to it repeatedly, climbs toward an alert. See
docs/design-aircraft-of-interest.md for the full rationale.

The score is a weighted sum of four 0–1 features against the node reference:
  - closeness  — closest slant-range approach vs AIR_RADIUS_NM
  - dwell      — longest in-range presence vs AIR_DWELL_TARGET_S
  - orbit      — cumulative heading change vs AIR_HEADING_TARGET_DEG
  - return     — caller-supplied return count vs AIR_RETURN_TARGET (long horizon)
times an interest multiplier (military / anonymous / no-callsign / rotorcraft /
low-slow), crossing the same suspicious/likely/high tiers as the mobile engine.

This module is deliberately I/O-free: the long-horizon return count is an *input*
so the core can be unit-tested against synthetic tracks. The durable per-ICAO
history that supplies it, the reference position, and the orchestrator/alert wiring
live in later phases.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Optional

from modules import air_geometry


def _f(env, key, default):
    try:
        return float(env.get(key, default))
    except (TypeError, ValueError):
        return float(default)


@dataclass(frozen=True)
class AirParams:
    """Tunable scoring parameters (the old hard thresholds, now normalizers)."""

    radius_nm: float = 5.0
    ceiling_ft: float = 5000.0
    dwell_target_s: float = 480.0
    heading_target_deg: float = 270.0
    return_target: float = 3.0
    w_dwell: float = 0.30
    w_orbit: float = 0.30
    w_close: float = 0.20
    w_return: float = 0.20
    interest_cap: float = 1.3
    tier_suspicious: float = 0.5
    tier_likely: float = 0.7
    tier_high: float = 0.9

    @classmethod
    def from_env(cls, env=None) -> "AirParams":
        env = os.environ if env is None else env
        return cls(
            radius_nm=_f(env, "AIR_RADIUS_NM", 5.0),
            ceiling_ft=_f(env, "AIR_CEILING_FT", 5000.0),
            dwell_target_s=_f(env, "AIR_DWELL_TARGET_S", 480.0),
            heading_target_deg=_f(env, "AIR_HEADING_TARGET_DEG", 270.0),
            return_target=_f(env, "AIR_RETURN_TARGET", 3.0),
            w_dwell=_f(env, "AIR_WEIGHT_DWELL", 0.30),
            w_orbit=_f(env, "AIR_WEIGHT_ORBIT", 0.30),
            w_close=_f(env, "AIR_WEIGHT_CLOSE", 0.20),
            w_return=_f(env, "AIR_WEIGHT_RETURN", 0.20),
            interest_cap=_f(env, "AIR_INTEREST_CAP", 1.3),
        )


@dataclass(frozen=True)
class InterestFlags:
    """Enrichment-derived modifiers that raise confidence beyond geometry."""

    military: bool = False
    anonymous_callsign: bool = False   # LADD / PIA / blocked
    no_callsign: bool = False
    rotorcraft: bool = False
    low_slow: bool = False             # UAS / very low groundspeed

    def multiplier(self, cap: float) -> float:
        """1.0 + 0.1 per active flag, capped (so enrichment nudges, never dominates)."""
        bonus = 0.1 * sum((self.military, self.anonymous_callsign, self.no_callsign,
                           self.rotorcraft, self.low_slow))
        return min(cap, 1.0 + bonus)


@dataclass
class AirScore:
    score: float = 0.0
    severity: Optional[str] = None     # None | suspicious | likely | high
    of_interest: bool = False
    breakdown: dict = field(default_factory=dict)


def _clamp01(x: float) -> float:
    return 0.0 if x < 0 else (1.0 if x > 1 else x)


def _severity(score: float, p: AirParams) -> Optional[str]:
    if score >= p.tier_high:
        return "high"
    if score >= p.tier_likely:
        return "likely"
    if score >= p.tier_suspicious:
        return "suspicious"
    return None


def score_air_contact(
    positions: list,
    reference: Optional[tuple],
    *,
    return_count: int = 0,
    flags: Optional[InterestFlags] = None,
    params: Optional[AirParams] = None,
) -> AirScore:
    """Score one contact's track against the node reference.

    ``positions`` is the orchestrator's per-ICAO track (with optional gap
    sentinels); ``reference`` is ``(lat, lon)`` or ``None``; ``return_count`` is the
    long-horizon return tally the caller supplies from the durable store. Returns an
    :class:`AirScore` — 0/None for a transit or thin track, climbing for a
    loiter/orbit/returner. Pure: no I/O, no clock.
    """
    p = params or AirParams()
    flags = flags or InterestFlags()
    if reference is None:
        return AirScore()
    ref_lat, ref_lon = reference

    segments = air_geometry.track_segments(positions)

    best_dwell_s = 0.0
    best_orbit_deg = 0.0
    min_slant = None
    has_inrange_run = False

    for seg in segments:
        inrange = []
        for pt in seg:
            slant = air_geometry.slant_range_nm(ref_lat, ref_lon, pt["lat"], pt["lon"], pt["altitude"])
            alt_ok = True
            if pt["altitude"] is not None:
                try:
                    alt_ok = float(pt["altitude"]) <= p.ceiling_ft
                except (TypeError, ValueError):
                    alt_ok = True
            if slant <= p.radius_nm and alt_ok:
                inrange.append(pt)
                if min_slant is None or slant < min_slant:
                    min_slant = slant
        if len(inrange) < 2:
            continue
        has_inrange_run = True
        # Dwell: span of the in-range run (covers reception gaps within a segment;
        # a true absence is a gap sentinel that ends the segment).
        ts = [pt["ts"] for pt in inrange if pt["ts"] is not None]
        if len(ts) >= 2:
            best_dwell_s = max(best_dwell_s, max(ts) - min(ts))
        # Orbit: heading change over the in-range run.
        orbit = air_geometry.cumulative_heading_change([(pt["lat"], pt["lon"]) for pt in inrange])
        best_orbit_deg = max(best_orbit_deg, orbit)

    # Min-observation guard: no in-range run of 2+ points → not scorable (a burn-by
    # or a contact that never came close). Return history alone never scores; it can
    # only amplify a real in-range presence.
    if not has_inrange_run:
        return AirScore(breakdown={"reason": "no in-range run of 2+ points"})

    closeness = _clamp01(1.0 - (min_slant / p.radius_nm)) if min_slant is not None else 0.0
    dwell = _clamp01(best_dwell_s / p.dwell_target_s) if p.dwell_target_s > 0 else 0.0
    orbit = _clamp01(best_orbit_deg / p.heading_target_deg) if p.heading_target_deg > 0 else 0.0
    ret = _clamp01(return_count / p.return_target) if p.return_target > 0 else 0.0

    base = p.w_dwell * dwell + p.w_orbit * orbit + p.w_close * closeness + p.w_return * ret
    mult = flags.multiplier(p.interest_cap)
    score = _clamp01(base * mult)
    sev = _severity(score, p)
    return AirScore(
        score=round(score, 4),
        severity=sev,
        of_interest=sev is not None,
        breakdown={
            "closeness": round(closeness, 3),
            "dwell": round(dwell, 3),
            "orbit": round(orbit, 3),
            "return": round(ret, 3),
            "interest_mult": round(mult, 3),
            "min_slant_nm": round(min_slant, 2) if min_slant is not None else None,
            "dwell_s": round(best_dwell_s, 1),
            "heading_deg": round(best_orbit_deg, 1),
        },
    )
