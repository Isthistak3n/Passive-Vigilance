"""Unit tests for modules/air_geometry.py — pure node-relative geometry."""

import math

from modules import air_geometry as g


def test_haversine_one_degree_latitude_is_about_60nm():
    d = g.haversine_nm(0.0, 0.0, 1.0, 0.0)
    assert math.isclose(d, 60.04, rel_tol=0.01)


def test_slant_range_directly_overhead_is_altitude():
    # ~6076 ft directly overhead -> ~1 nm slant, ground distance ~0.
    s = g.slant_range_nm(21.4, -157.7, 21.4, -157.7, 6076.12)
    assert math.isclose(s, 1.0, rel_tol=0.02)


def test_slant_range_folds_altitude_in():
    # A high airframe overhead reads far, not close.
    s = g.slant_range_nm(21.4, -157.7, 21.4, -157.7, 35000)
    assert s > 5.5  # 35000 ft ~ 5.76 nm


def test_bearing_cardinetals():
    assert math.isclose(g.bearing_deg(0, 0, 1, 0), 0.0, abs_tol=0.5)     # north
    assert math.isclose(g.bearing_deg(0, 0, 0, 1), 90.0, abs_tol=0.5)    # east


def test_cumulative_heading_change_straight_line_is_zero():
    pts = [(0.0, 0.0), (0.1, 0.0), (0.2, 0.0), (0.3, 0.0)]
    assert g.cumulative_heading_change(pts) < 1.0


def test_cumulative_heading_change_full_circle_near_360():
    clat, clon, r = 21.4, -157.7, 0.5 / 60.0
    pts = []
    for i in range(13):
        ang = 2 * math.pi * i / 12
        pts.append((clat + r * math.cos(ang),
                    clon + r * math.sin(ang) / math.cos(math.radians(clat))))
    total = g.cumulative_heading_change(pts)
    assert total > 300.0  # a near-full loop


def test_cumulative_heading_change_needs_three_points():
    assert g.cumulative_heading_change([(0.0, 0.0), (1.0, 1.0)]) == 0.0


def test_track_segments_splits_on_gap_and_drops_positionless():
    positions = [
        {"lat": 21.4, "lon": -157.7, "altitude": 1500, "timestamp": "2026-06-16T00:00:00+00:00"},
        {"lat": 21.41, "lon": -157.7, "altitude": 1500, "timestamp": "2026-06-16T00:01:00+00:00"},
        {"gap": True, "timestamp": "2026-06-16T00:40:00+00:00"},
        {"altitude": 1500, "timestamp": "2026-06-16T00:45:00+00:00"},  # no lat/lon -> dropped
        {"lat": 21.42, "lon": -157.7, "altitude": 1500, "timestamp": "2026-06-16T00:46:00+00:00"},
        {"lat": 21.43, "lon": -157.7, "altitude": 1500, "timestamp": "2026-06-16T00:47:00+00:00"},
    ]
    segs = g.track_segments(positions)
    assert len(segs) == 2
    assert len(segs[0]) == 2
    assert len(segs[1]) == 2
    assert segs[0][0]["ts"] is not None


def test_resolve_reference_env_home_wins():
    env = {"AIR_HOME_LAT": "21.4", "AIR_HOME_LON": "-157.7"}
    assert g.resolve_reference({"lat": 1.0, "lon": 2.0}, env=env) == (21.4, -157.7)


def test_resolve_reference_falls_back_to_gps():
    assert g.resolve_reference({"lat": 21.4, "lon": -157.7}, env={}) == (21.4, -157.7)


def test_resolve_reference_none_when_no_source():
    assert g.resolve_reference(None, env={}) is None
    assert g.resolve_reference({"lat": None, "lon": None}, env={}) is None
