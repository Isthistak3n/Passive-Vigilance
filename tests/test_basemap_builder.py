"""Tests for modules.basemap_builder — the offline-pack writer (pure parts).

Network paths (internet_reachable / build_pack download loop) are not exercised here;
the orchestrator boot test covers the policy (off-node default vs. opt-in fetch).
"""

from modules import basemap_builder as bb


def test_deg2num_origin_and_known():
    # At zoom 0 the whole world is one tile (0, 0).
    assert bb.deg2num(0.0, 0.0, 0) == (0, 0)
    # At zoom 1 the NE quadrant of (lat>0, lon>0) is tile (1, 0).
    assert bb.deg2num(10.0, 10.0, 1) == (1, 0)


def test_bbox_from_center_is_symmetric():
    w, s, e, n = bb.bbox_from_center(21.4, -157.7, 3.0)
    assert w < -157.7 < e and s < 21.4 < n
    # Latitude half-span ≈ radius/111 km/deg.
    assert abs(((n - s) / 2) - (3.0 / 111.0)) < 1e-6


def test_plan_tiles_counts_and_zoom_span():
    bounds = bb.bbox_from_center(21.4, -157.7, 1.0)
    plan = bb.plan_tiles(bounds, 12, 14)
    zs = {z for z, _, _ in plan}
    assert zs == {12, 13, 14}
    # Higher zoom never has fewer tiles than lower for the same box.
    by_zoom = {z: sum(1 for zz, _, _ in plan if zz == z) for z in zs}
    assert by_zoom[14] >= by_zoom[13] >= by_zoom[12] >= 1


def test_suggest_command_shape():
    cmd = bb.suggest_command(21.41503, -157.76468, radius_km=3, min_zoom=11,
                             max_zoom=17, out_path="data/tiles/basemap.mbtiles")
    assert cmd.startswith("python3 scripts/fetch_basemap.py --center 21.41503,-157.76468")
    assert "--min-zoom 11 --max-zoom 17" in cmd
    assert "--out data/tiles/basemap.mbtiles" in cmd
