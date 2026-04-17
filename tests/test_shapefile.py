"""Tests for ShapefileWriter — GIS output from sensor session events.

Requires geopandas; skipped automatically if not installed.
"""

import json

import pytest

geopandas = pytest.importorskip("geopandas")

from modules.shapefile import ShapefileWriter


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _wifi_events(n: int = 2) -> list[dict]:
    return [
        {
            "event_type":       "wifi",
            "mac":              f"aa:bb:cc:dd:ee:{i:02x}",
            "device_type":      "phone",
            "score":            0.8 + i * 0.05,
            "alert_level":      "likely",
            "manufacturer":     "Apple",
            "first_seen":       "2026-01-01T12:00:00+00:00",
            "last_seen":        "2026-01-01T12:20:00+00:00",
            "observation_count": 10 + i,
            "lat":              51.5074 + i * 0.001,
            "lon":              -0.1278 + i * 0.001,
            "timestamp":        "2026-01-01T12:20:00+00:00",
        }
        for i in range(n)
    ]


def _aircraft_events(n: int = 1) -> list[dict]:
    return [
        {
            "event_type":   "aircraft",
            "icao":         f"ABC{i:03d}",
            "callsign":     f"BAW{i:03d}",
            "registration": "G-EUPT",
            "operator":     "British Airways",
            "country":      "UK",
            "altitude":     35000,
            "lat":          51.5 + i * 0.1,
            "lon":          -0.1 + i * 0.1,
            "timestamp":    "2026-01-01T12:10:00+00:00",
        }
        for i in range(n)
    ]


def _drone_events(n: int = 1) -> list[dict]:
    return [
        {
            "event_type": "drone",
            "freq_mhz":   915.0,
            "power_db":   -18.5,
            "lat":        51.5,
            "lon":        -0.1,
            "timestamp":  "2026-01-01T12:05:00+00:00",
        }
        for _ in range(n)
    ]


# ---------------------------------------------------------------------------
# write_session() tests
# ---------------------------------------------------------------------------


def test_write_session_creates_shp_file(tmp_path):
    writer = ShapefileWriter(output_dir=str(tmp_path))
    events = _wifi_events(2)
    shp_path = writer.write_session("test_session", events)

    assert shp_path.endswith(".shp"), "write_session must return a .shp path"
    from pathlib import Path
    assert Path(shp_path).exists(), "The .shp file must exist on disk"


def test_write_session_creates_aircraft_shp(tmp_path):
    writer = ShapefileWriter(output_dir=str(tmp_path))
    events = _aircraft_events(2)
    writer.write_session("test_session", events)

    from pathlib import Path
    assert (Path(tmp_path) / "test_session" / "detections_aircraft.shp").exists()


def test_write_session_handles_empty_events_gracefully(tmp_path):
    writer = ShapefileWriter(output_dir=str(tmp_path))
    # Must not raise even with no events
    result = writer.write_session("empty_session", [])
    assert "empty_session" in result


def test_write_session_device_events_have_point_geometry(tmp_path):
    writer = ShapefileWriter(output_dir=str(tmp_path))
    events = _wifi_events(3)
    shp_path = writer.write_session("geom_session", events)

    gdf = geopandas.read_file(shp_path)
    assert len(gdf) == 3
    for geom in gdf.geometry:
        assert geom.geom_type == "Point"


# ---------------------------------------------------------------------------
# write_geojson() tests
# ---------------------------------------------------------------------------


def test_write_geojson_creates_file(tmp_path):
    writer = ShapefileWriter(output_dir=str(tmp_path))
    events = _wifi_events(2) + _aircraft_events(1)
    path = writer.write_geojson("geojson_session", events)

    from pathlib import Path
    assert Path(path).exists()
    assert path.endswith(".geojson")


def test_write_geojson_output_is_valid_json(tmp_path):
    writer = ShapefileWriter(output_dir=str(tmp_path))
    events = _wifi_events(2) + _drone_events(1)
    path = writer.write_geojson("json_session", events)

    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    assert data["type"] == "FeatureCollection"
    assert len(data["features"]) == 3


def test_write_geojson_empty_events_produces_empty_feature_collection(tmp_path):
    writer = ShapefileWriter(output_dir=str(tmp_path))
    path = writer.write_geojson("empty_geojson", [])

    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    assert data["type"] == "FeatureCollection"
    assert data["features"] == []
