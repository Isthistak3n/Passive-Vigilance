"""Unit tests for modules/air_scoring.py — air persistence scoring on synthetic tracks."""

import math
from datetime import datetime, timedelta, timezone

from modules.air_scoring import AirParams, InterestFlags, score_air_contact

REF = (51.5, -0.1)
_T0 = datetime(2026, 6, 16, 0, 0, 0, tzinfo=timezone.utc)


def _orbit_track(center=REF, radius_nm=0.5, n=12, dt_s=60, alt=1500, t0=_T0):
    """A near-full circle around `center` — the loiter case."""
    clat, clon = center
    r = radius_nm / 60.0
    pts = []
    for i in range(n):
        ang = 2 * math.pi * i / (n - 1)
        pts.append({
            "lat": clat + r * math.cos(ang),
            "lon": clon + r * math.sin(ang) / math.cos(math.radians(clat)),
            "altitude": alt,
            "timestamp": (t0 + timedelta(seconds=i * dt_s)).isoformat(),
        })
    return pts


def _transit_track(alt=1500, t0=_T0):
    """A short straight pass close to the node — the burn-by case."""
    clat, clon = REF
    pts = []
    for i in range(3):
        pts.append({
            "lat": clat - 0.01 + 0.01 * i,
            "lon": clon,
            "altitude": alt,
            "timestamp": (t0 + timedelta(seconds=i * 30)).isoformat(),
        })
    return pts


def test_orbit_scores_of_interest():
    s = score_air_contact(_orbit_track(), REF)
    assert s.of_interest
    assert s.score >= 0.7           # likely+ on geometry alone
    assert s.breakdown["orbit"] > 0.9
    assert s.breakdown["dwell"] > 0.9


def test_transit_does_not_alert():
    s = score_air_contact(_transit_track(), REF)
    assert not s.of_interest
    assert s.severity is None
    assert s.score < 0.5


def test_high_overflight_not_in_range():
    s = score_air_contact(_orbit_track(alt=35000), REF)
    assert s.score == 0.0
    assert not s.of_interest


def test_single_inrange_point_is_not_scorable():
    one = [{"lat": 51.5, "lon": -0.1, "altitude": 1500,
            "timestamp": _T0.isoformat()}]
    s = score_air_contact(one, REF)
    assert s.score == 0.0


def test_returns_amplify_score():
    base = score_air_contact(_orbit_track(), REF, return_count=0)
    returned = score_air_contact(_orbit_track(), REF, return_count=3)
    assert returned.score > base.score
    assert returned.breakdown["return"] == 1.0


def test_return_count_alone_never_scores_a_distant_contact():
    # A far transit with a big return count must still not alert — returns only
    # amplify a real in-range presence.
    far = [{"lat": 30.0, "lon": -0.1, "altitude": 1500,
            "timestamp": (_T0 + timedelta(seconds=i * 60)).isoformat()} for i in range(5)]
    s = score_air_contact(far, REF, return_count=10)
    assert s.score == 0.0


def test_interest_flags_raise_score():
    plain = score_air_contact(_orbit_track(), REF)
    flagged = score_air_contact(_orbit_track(), REF,
                                flags=InterestFlags(military=True, no_callsign=True))
    assert flagged.score > plain.score
    assert flagged.breakdown["interest_mult"] > 1.0


def test_no_reference_is_noop():
    assert score_air_contact(_orbit_track(), None).score == 0.0


def test_gap_does_not_fabricate_dwell_across_absence():
    # Two short in-range runs split by a gap: dwell is the longest single run, not
    # the span across the absence.
    seg = _orbit_track(n=3, dt_s=30)  # ~60s run
    gap = [{"gap": True, "timestamp": (_T0 + timedelta(minutes=40)).isoformat()}]
    seg2 = _orbit_track(n=3, dt_s=30, t0=_T0 + timedelta(minutes=45))
    s = score_air_contact(seg + gap + seg2, REF)
    assert s.breakdown["dwell_s"] < 120  # not ~2700s across the gap


def test_params_from_env_override():
    p = AirParams.from_env({"AIR_RADIUS_NM": "1", "AIR_DWELL_TARGET_S": "60"})
    assert p.radius_nm == 1.0
    assert p.dwell_target_s == 60.0
