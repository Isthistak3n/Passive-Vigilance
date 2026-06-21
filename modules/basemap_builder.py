"""basemap_builder — build an offline MBTiles basemap from XYZ raster tiles.

Shared by ``scripts/fetch_basemap.py`` (the CLI) and the orchestrator's boot-time
provisioning. The *reader* lives in :mod:`modules.offline_tiles`; this is the *writer*,
kept separate so the GUI's reader import stays light and free of network code.

OPSEC: fetching tiles for a tight area centered on the node's GPS position reveals
that position — the tile provider sees exact coordinates server-side, and a passive
network observer sees the request burst, SNI, and timing. So the node does **not**
fetch by default (see the orchestrator's boot provisioning and the README): the safe
path is to build the pack OFF-NODE and copy it on. This module just provides the
mechanism; the policy (whether the node may fetch) lives at the call site.
"""

import logging
import math
import os
import sqlite3
import time
import urllib.error
import urllib.request

logger = logging.getLogger(__name__)

DEFAULT_TILE_URL = "https://tile.openstreetmap.org/{z}/{x}/{y}.png"
USER_AGENT = "PassiveVigilance-basemap-fetch/1.0 (offline field node; personal use)"


def deg2num(lat, lon, z):
    """Slippy-map XYZ tile covering (lat, lon) at zoom z."""
    lat_r = math.radians(lat)
    n = 1 << z
    x = int((lon + 180.0) / 360.0 * n)
    y = int((1.0 - math.asinh(math.tan(lat_r)) / math.pi) / 2.0 * n)
    return max(0, min(n - 1, x)), max(0, min(n - 1, y))


def bbox_from_center(lat, lon, radius_km):
    """A (min_lon, min_lat, max_lon, max_lat) box ~radius_km around a point."""
    dlat = radius_km / 111.0
    dlon = radius_km / (111.0 * max(0.01, math.cos(math.radians(lat))))
    return (lon - dlon, lat - dlat, lon + dlon, lat + dlat)


def plan_tiles(bounds, min_zoom, max_zoom):
    """List the XYZ (z, x, y) tiles covering ``bounds`` across the zoom range."""
    w, s, e, n = bounds
    plan = []
    for z in range(min_zoom, max_zoom + 1):
        x0, y0 = deg2num(n, w, z)  # NW corner
        x1, y1 = deg2num(s, e, z)  # SE corner
        for x in range(min(x0, x1), max(x0, x1) + 1):
            for y in range(min(y0, y1), max(y0, y1) + 1):
                plan.append((z, x, y))
    return plan


def _init_mbtiles(conn, name, bounds, center, minz, maxz):
    conn.executescript(
        "CREATE TABLE IF NOT EXISTS metadata (name TEXT, value TEXT);"
        "CREATE TABLE IF NOT EXISTS tiles ("
        "  zoom_level INTEGER, tile_column INTEGER, tile_row INTEGER, tile_data BLOB);"
        "CREATE UNIQUE INDEX IF NOT EXISTS tile_index "
        "  ON tiles (zoom_level, tile_column, tile_row);"
    )
    w, s, e, n = bounds
    meta = {
        "name": name, "format": "png", "type": "baselayer", "version": "1.0",
        "minzoom": str(minz), "maxzoom": str(maxz),
        "bounds": f"{w},{s},{e},{n}",
        # MBTiles spec: center is "longitude,latitude,zoom" (center is (lat, lon)).
        "center": f"{center[1]},{center[0]},{min(maxz, (minz + maxz) // 2)}",
        "attribution": "© OpenStreetMap contributors",
        "description": "Passive Vigilance offline basemap",
    }
    conn.executemany("INSERT INTO metadata (name, value) VALUES (?, ?)", list(meta.items()))
    conn.commit()


def suggest_command(lat, lon, radius_km=3.0, min_zoom=11, max_zoom=17, out_path=None):
    """The ready-to-run ``fetch_basemap.py`` command for this location — logged at
    boot so the operator can build the pack OFF-NODE (no node-side location leak)."""
    out = out_path or "data/tiles/basemap.mbtiles"
    return (
        f"python3 scripts/fetch_basemap.py --center {lat:.5f},{lon:.5f} "
        f"--radius-km {radius_km:g} --min-zoom {min_zoom} --max-zoom {max_zoom} "
        f"--out {out}"
    )


def internet_reachable(url=DEFAULT_TILE_URL, timeout=5.0):
    """True if the tile host answers. NOTE: this probe itself touches the network and
    reveals the node is reaching the tile provider — call it ONLY on the opt-in fetch
    path, never on the default off-node path."""
    host = url.format(z=0, x=0, y=0)
    try:
        req = urllib.request.Request(host, method="HEAD", headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return 200 <= r.status < 400
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError):
        return False


def build_pack(center, radius_km, min_zoom, max_zoom, out_path,
               tile_url=DEFAULT_TILE_URL, delay=0.1, progress=None):
    """Download tiles for ``center`` (lat, lon) into an MBTiles file at ``out_path``.

    Returns ``{"fetched", "skipped", "failed", "total", "path"}``. Resumable (skips
    tiles already present). ``progress(done, total)`` is called periodically if given.
    Raises nothing for individual tile failures (counted); raises only on unusable args.
    """
    lat, lon = center
    bounds = bbox_from_center(lat, lon, radius_km)
    plan = plan_tiles(bounds, min_zoom, max_zoom)
    total = len(plan)

    os.makedirs(os.path.dirname(os.path.abspath(out_path)) or ".", exist_ok=True)
    conn = sqlite3.connect(out_path)
    try:
        _init_mbtiles(conn, os.path.basename(out_path), bounds, (lat, lon), min_zoom, max_zoom)
        fetched = skipped = failed = 0
        for i, (z, x, y) in enumerate(plan, 1):
            tms_y = (1 << z) - 1 - y
            if conn.execute(
                "SELECT 1 FROM tiles WHERE zoom_level=? AND tile_column=? AND tile_row=?",
                (z, x, tms_y),
            ).fetchone():
                skipped += 1
                continue
            try:
                req = urllib.request.Request(
                    tile_url.format(z=z, x=x, y=y), headers={"User-Agent": USER_AGENT}
                )
                with urllib.request.urlopen(req, timeout=20) as r:
                    blob = r.read()
                conn.execute("INSERT OR REPLACE INTO tiles VALUES (?, ?, ?, ?)",
                             (z, x, tms_y, sqlite3.Binary(blob)))
                fetched += 1
                if fetched % 50 == 0:
                    conn.commit()
                    if progress:
                        progress(i, total)
                time.sleep(delay)
            except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as exc:
                failed += 1
                logger.debug("tile %s/%s/%s failed: %s", z, x, y, exc)
        conn.commit()
    finally:
        conn.close()
    return {"fetched": fetched, "skipped": skipped, "failed": failed,
            "total": total, "path": out_path}
