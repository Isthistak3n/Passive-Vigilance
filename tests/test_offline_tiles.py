"""Tests for modules.offline_tiles — the offline MBTiles basemap reader."""

import sqlite3

import pytest

from modules import offline_tiles


def _make_mbtiles(path, *, tiles, metadata=None):
    """Write a minimal MBTiles file. ``tiles`` is {(z, x, tms_y): bytes}."""
    conn = sqlite3.connect(path)
    conn.executescript(
        "CREATE TABLE metadata (name TEXT, value TEXT);"
        "CREATE TABLE tiles (zoom_level INTEGER, tile_column INTEGER, "
        "tile_row INTEGER, tile_data BLOB);"
    )
    for (z, x, tms_y), blob in tiles.items():
        conn.execute("INSERT INTO tiles VALUES (?, ?, ?, ?)",
                     (z, x, tms_y, sqlite3.Binary(blob)))
    for k, v in (metadata or {}).items():
        conn.execute("INSERT INTO metadata VALUES (?, ?)", (k, v))
    conn.commit()
    conn.close()


def test_get_tile_applies_tms_flip(tmp_path):
    # An XYZ y is stored at TMS row (2^z - 1 - y). At z=2, y=1 -> tms_row 2.
    p = tmp_path / "m.mbtiles"
    _make_mbtiles(p, tiles={(2, 1, 2): b"PNGDATA"})
    r = offline_tiles.MBTilesReader(str(p))
    assert r.get_tile(2, 1, 1) == b"PNGDATA"   # found via the flip
    assert r.get_tile(2, 1, 2) is None         # the un-flipped row is empty


def test_missing_tile_returns_none(tmp_path):
    p = tmp_path / "m.mbtiles"
    _make_mbtiles(p, tiles={(5, 10, 10): b"x"})
    r = offline_tiles.MBTilesReader(str(p))
    assert r.get_tile(5, 999, 999) is None


def test_describe_center_is_lat_lon(tmp_path):
    # MBTiles center metadata is "lon,lat,zoom"; describe() returns [lat, lon].
    p = tmp_path / "m.mbtiles"
    _make_mbtiles(p, tiles={(1, 0, 0): b"x"},
                  metadata={"center": "-157.86,21.31,12", "minzoom": "10",
                            "maxzoom": "16", "name": "honolulu"})
    d = offline_tiles.MBTilesReader(str(p)).describe()
    assert d["available"] is True
    assert d["center"] == [21.31, -157.86]
    assert d["minzoom"] == 10 and d["maxzoom"] == 16
    assert d["name"] == "honolulu"


def test_describe_falls_back_to_bounds_midpoint(tmp_path):
    p = tmp_path / "m.mbtiles"
    _make_mbtiles(p, tiles={(1, 0, 0): b"x"}, metadata={"bounds": "-158,21,-157,22"})
    d = offline_tiles.MBTilesReader(str(p)).describe()
    assert d["center"] == [21.5, -157.5]   # midpoint of the bounds


def test_content_type_png_default_and_jpg(tmp_path):
    p1 = tmp_path / "png.mbtiles"
    _make_mbtiles(p1, tiles={(1, 0, 0): b"x"})            # no format -> png
    assert offline_tiles.MBTilesReader(str(p1)).content_type == "image/png"
    p2 = tmp_path / "jpg.mbtiles"
    _make_mbtiles(p2, tiles={(1, 0, 0): b"x"}, metadata={"format": "jpg"})
    assert offline_tiles.MBTilesReader(str(p2)).content_type == "image/jpeg"


def test_load_basemap_missing_file_returns_none(tmp_path):
    assert offline_tiles.load_basemap(str(tmp_path / "nope.mbtiles")) is None


def test_load_basemap_opens_existing(tmp_path):
    p = tmp_path / "m.mbtiles"
    _make_mbtiles(p, tiles={(0, 0, 0): b"x"})
    r = offline_tiles.load_basemap(str(p))
    assert r is not None and r.get_tile(0, 0, 0) == b"x"


def test_resolve_path_env_override(monkeypatch):
    monkeypatch.setenv("OFFLINE_TILES_MBTILES", "/custom/path.mbtiles")
    assert offline_tiles.resolve_path() == "/custom/path.mbtiles"
    monkeypatch.delenv("OFFLINE_TILES_MBTILES", raising=False)
    assert offline_tiles.resolve_path() == offline_tiles.DEFAULT_MBTILES_PATH
