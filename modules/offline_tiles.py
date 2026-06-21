"""offline_tiles — serve a local raster basemap from an MBTiles file (offline GUI).

A field node has no internet, so the GUI's default online OSM tile layer renders a
blank grey map. This module reads a pre-built **MBTiles** pack (a single SQLite file
of raster tiles, the de-facto offline-map format) so the Flask GUI — and, pointed at
the same endpoint, tar1090 — can show a real basemap with no network.

Design notes:
- **Read-only, stdlib-only** (``sqlite3``). The pack is built once, online, by
  ``scripts/fetch_basemap.py`` for the operating area; this is just the reader.
- **MBTiles stores rows TMS-flipped** (y origin at the bottom), while Leaflet/XYZ
  request y from the top. :meth:`get_tile` does the ``y -> 2^z-1-y`` flip, so callers
  pass ordinary XYZ coordinates.
- **Thread-safe**: Flask serves tiles from worker threads, so the connection is opened
  ``check_same_thread=False`` and every read is guarded by a lock.
- **Absent / unreadable pack is not an error** — :func:`load_basemap` returns ``None``
  and the GUI falls back to online tiles. Offline is a capability, not a requirement.
"""

import logging
import os
import sqlite3
import threading
from typing import Optional

logger = logging.getLogger(__name__)

# Default location for the bundled pack; override with OFFLINE_TILES_MBTILES.
DEFAULT_MBTILES_PATH = "data/tiles/basemap.mbtiles"


class MBTilesReader:
    """Read-only accessor for a raster MBTiles pack."""

    def __init__(self, path: str):
        self.path = path
        self._lock = threading.Lock()
        # One shared read-only connection (URI mode) across threads.
        self._conn = sqlite3.connect(
            f"file:{path}?mode=ro", uri=True, check_same_thread=False
        )
        self._conn.row_factory = sqlite3.Row
        self._meta = self._read_metadata()

    def _read_metadata(self) -> dict:
        meta: dict = {}
        try:
            with self._lock:
                for row in self._conn.execute("SELECT name, value FROM metadata"):
                    meta[row["name"]] = row["value"]
        except sqlite3.Error:
            pass  # metadata table is optional; tiles still work without it
        return meta

    @property
    def tile_format(self) -> str:
        """Image format declared in metadata (``png`` / ``jpg``); defaults to png."""
        return (self._meta.get("format") or "png").lower()

    @property
    def content_type(self) -> str:
        fmt = self.tile_format
        return "image/jpeg" if fmt in ("jpg", "jpeg") else "image/png"

    def get_tile(self, z: int, x: int, y: int) -> Optional[bytes]:
        """Return the tile blob for XYZ ``(z, x, y)`` or ``None`` if absent.

        Converts the XYZ ``y`` to the TMS ``y`` MBTiles stores under.
        """
        tms_y = (1 << z) - 1 - y
        try:
            with self._lock:
                row = self._conn.execute(
                    "SELECT tile_data FROM tiles "
                    "WHERE zoom_level=? AND tile_column=? AND tile_row=?",
                    (z, x, tms_y),
                ).fetchone()
        except sqlite3.Error as exc:
            logger.warning("MBTiles read error at z%s/%s/%s: %s", z, x, y, exc)
            return None
        return bytes(row["tile_data"]) if row else None

    def describe(self) -> dict:
        """A small client-facing summary for the GUI: where to centre and the zoom
        span. ``center`` metadata is ``lon,lat,zoom``; we return ``[lat, lon]`` for
        Leaflet. Falls back to bounds midpoint, then null (client keeps its default).
        """
        center = None
        raw = self._meta.get("center")
        if raw:
            try:
                lon, lat, *_ = (float(p) for p in raw.split(","))
                center = [lat, lon]
            except (ValueError, TypeError):
                center = None
        if center is None and self._meta.get("bounds"):
            try:
                w, s, e, n = (float(p) for p in self._meta["bounds"].split(","))
                center = [(s + n) / 2.0, (w + e) / 2.0]
            except (ValueError, TypeError):
                center = None

        def _int(key):
            try:
                return int(self._meta[key])
            except (KeyError, ValueError, TypeError):
                return None

        return {
            "available": True,
            "name": self._meta.get("name") or os.path.basename(self.path),
            "center": center,
            "minzoom": _int("minzoom"),
            "maxzoom": _int("maxzoom"),
            "attribution": self._meta.get("attribution") or "© OpenStreetMap contributors",
        }

    def close(self) -> None:
        with self._lock:
            try:
                self._conn.close()
            except sqlite3.Error:
                pass


def resolve_path() -> str:
    """The configured MBTiles path (``OFFLINE_TILES_MBTILES`` or the default)."""
    return (os.getenv("OFFLINE_TILES_MBTILES") or DEFAULT_MBTILES_PATH).strip()


def load_basemap(path: Optional[str] = None) -> Optional[MBTilesReader]:
    """Open the basemap pack if it exists and is readable, else ``None``.

    Never raises — a missing or corrupt pack just means the GUI uses online tiles.
    """
    path = path or resolve_path()
    if not path or not os.path.isfile(path):
        logger.info("Offline basemap: no pack at %s — GUI will use online tiles", path)
        return None
    try:
        reader = MBTilesReader(path)
        logger.info("Offline basemap: serving %s (%s)", path, reader.describe().get("name"))
        return reader
    except sqlite3.Error as exc:
        logger.warning("Offline basemap: %s is not a readable MBTiles (%s)", path, exc)
        return None
