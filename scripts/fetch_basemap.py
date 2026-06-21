#!/usr/bin/env python3
"""fetch_basemap.py — build an offline MBTiles basemap for a field deployment.

Run this ONCE, while online, to bundle the operating area's map tiles into a single
MBTiles file. The Passive Vigilance GUI (and tar1090, pointed at the same endpoint)
then serve the basemap with no internet. See docs / README "Boot sequence".

Example — a ~5 km box around a point, street-level detail:

    python3 scripts/fetch_basemap.py --center 21.31,-157.86 --radius-km 5 \
        --min-zoom 11 --max-zoom 17 --out data/tiles/basemap.mbtiles

or an explicit bounding box (min_lon,min_lat,max_lon,max_lat):

    python3 scripts/fetch_basemap.py --bbox -158.0,21.2,-157.7,21.4 --max-zoom 16

Tile-usage note: the default source is the public OSM tile server, whose usage policy
forbids bulk/heavy downloading. Keep areas small and zoom ranges sane (each +1 zoom is
~4x the tiles). For larger areas use your own tile server or a provider that permits
bulk export via --tile-url. A polite delay + User-Agent is sent by default.
"""

import argparse
import math
import os
import sqlite3
import sys
import time
import urllib.error
import urllib.request

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
    dlat = radius_km / 111.0
    dlon = radius_km / (111.0 * max(0.01, math.cos(math.radians(lat))))
    return (lon - dlon, lat - dlat, lon + dlon, lat + dlat)


def init_mbtiles(conn, name, bounds, center, minz, maxz):
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
        # MBTiles spec: center is "longitude,latitude,zoom" (center is (lat, lon) here).
        "center": f"{center[1]},{center[0]},{min(maxz, (minz + maxz) // 2)}",
        "attribution": "© OpenStreetMap contributors",
        "description": "Passive Vigilance offline basemap",
    }
    conn.executemany(
        "INSERT INTO metadata (name, value) VALUES (?, ?)", list(meta.items())
    )
    conn.commit()


def main(argv=None):
    p = argparse.ArgumentParser(description="Build an offline MBTiles basemap.")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--bbox", help="min_lon,min_lat,max_lon,max_lat")
    g.add_argument("--center", help="lat,lon (use with --radius-km)")
    p.add_argument("--radius-km", type=float, default=5.0, help="with --center (default 5)")
    p.add_argument("--min-zoom", type=int, default=11)
    p.add_argument("--max-zoom", type=int, default=16)
    p.add_argument("--out", default="data/tiles/basemap.mbtiles")
    p.add_argument("--tile-url", default=DEFAULT_TILE_URL,
                   help="XYZ template {z}/{x}/{y} (default public OSM)")
    p.add_argument("--delay", type=float, default=0.1, help="seconds between fetches")
    args = p.parse_args(argv)

    if args.center:
        lat, lon = (float(v) for v in args.center.split(","))
        bounds = bbox_from_center(lat, lon, args.radius_km)
        center = (lat, lon)
    else:
        w, s, e, n = (float(v) for v in args.bbox.split(","))
        bounds = (w, s, e, n)
        center = ((s + n) / 2.0, (w + e) / 2.0)

    # Pre-count so the operator sees the size before committing.
    plan = []
    for z in range(args.min_zoom, args.max_zoom + 1):
        x0, y0 = deg2num(bounds[3], bounds[0], z)  # NW
        x1, y1 = deg2num(bounds[1], bounds[2], z)  # SE
        for x in range(min(x0, x1), max(x0, x1) + 1):
            for y in range(min(y0, y1), max(y0, y1) + 1):
                plan.append((z, x, y))
    total = len(plan)
    print(f"Area bounds={bounds} zooms {args.min_zoom}-{args.max_zoom} → {total} tiles")
    if total > 50000:
        print(f"WARNING: {total} tiles is a lot for the public OSM server. "
              f"Narrow the area / zoom, or use --tile-url for your own server.",
              file=sys.stderr)

    os.makedirs(os.path.dirname(os.path.abspath(args.out)) or ".", exist_ok=True)
    conn = sqlite3.connect(args.out)
    init_mbtiles(conn, os.path.basename(args.out), bounds, center, args.min_zoom, args.max_zoom)

    fetched = skipped = failed = 0
    for i, (z, x, y) in enumerate(plan, 1):
        tms_y = (1 << z) - 1 - y
        if conn.execute(
            "SELECT 1 FROM tiles WHERE zoom_level=? AND tile_column=? AND tile_row=?",
            (z, x, tms_y),
        ).fetchone():
            skipped += 1
            continue
        url = args.tile_url.format(z=z, x=x, y=y)
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=20) as r:
                blob = r.read()
            conn.execute(
                "INSERT OR REPLACE INTO tiles VALUES (?, ?, ?, ?)",
                (z, x, tms_y, sqlite3.Binary(blob)),
            )
            fetched += 1
            if fetched % 50 == 0:
                conn.commit()
                print(f"  {i}/{total} (fetched={fetched} skipped={skipped} failed={failed})")
            time.sleep(args.delay)
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as exc:
            failed += 1
            print(f"  tile {z}/{x}/{y} failed: {exc}", file=sys.stderr)

    conn.commit()
    conn.close()
    print(f"Done: {args.out} — fetched={fetched} skipped={skipped} failed={failed} "
          f"(size {os.path.getsize(args.out)//1024} KiB)")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
