#!/usr/bin/env python3
"""fetch_basemap.py — build an offline MBTiles basemap for a field deployment.

Run this ONCE, while online, to bundle the operating area's map tiles into a single
MBTiles file. The Passive Vigilance GUI (and tar1090, via mbtiles_to_tar1090.py) then
serve the basemap with no internet. See README "Boot sequence".

    python3 scripts/fetch_basemap.py --center 21.41,-157.76 --radius-km 3 \
        --min-zoom 11 --max-zoom 17 --out data/tiles/basemap.mbtiles

or an explicit bounding box (min_lon,min_lat,max_lon,max_lat):

    python3 scripts/fetch_basemap.py --bbox -157.80,21.39,-157.74,21.44 --max-zoom 16

OPSEC: building a tight area around a point reveals that location to the tile provider
(exact coords, server-side) and, by request burst/timing, to a passive network observer.
For a deployed sensor, prefer building this OFF-NODE (on a different machine/network) and
copying the .mbtiles onto the node — the node then never touches a tile server. See the
README "Boot sequence" opsec note.

Tile-usage note: the default source is the public OSM tile server, whose policy forbids
bulk/heavy downloading. Keep areas small and zoom ranges sane (each +1 zoom is ~4x the
tiles). For larger areas use your own tile server or a provider that permits bulk export
via --tile-url. A polite delay + User-Agent is sent by default.
"""

import argparse
import os
import sys

# Allow running as a plain script (python3 scripts/fetch_basemap.py).
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from modules import basemap_builder as bb  # noqa: E402


def main(argv=None):
    p = argparse.ArgumentParser(description="Build an offline MBTiles basemap.")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--bbox", help="min_lon,min_lat,max_lon,max_lat")
    g.add_argument("--center", help="lat,lon (use with --radius-km)")
    p.add_argument("--radius-km", type=float, default=3.0, help="with --center (default 3)")
    p.add_argument("--min-zoom", type=int, default=11)
    p.add_argument("--max-zoom", type=int, default=17)
    p.add_argument("--out", default="data/tiles/basemap.mbtiles")
    p.add_argument("--tile-url", default=bb.DEFAULT_TILE_URL,
                   help="XYZ template {z}/{x}/{y} (default public OSM)")
    p.add_argument("--delay", type=float, default=0.1, help="seconds between fetches")
    args = p.parse_args(argv)

    if args.center:
        lat, lon = (float(v) for v in args.center.split(","))
        bounds = bb.bbox_from_center(lat, lon, args.radius_km)
        radius = args.radius_km
    else:
        w, s, e, n = (float(v) for v in args.bbox.split(","))
        lat, lon = (s + n) / 2.0, (w + e) / 2.0
        bounds = (w, s, e, n)
        # Effective radius for build_pack's center-based box: half the larger span.
        radius = max((n - s) * 111.0, (e - w) * 111.0) / 2.0

    total = len(bb.plan_tiles(bounds, args.min_zoom, args.max_zoom))
    print(f"Area bounds={bounds} zooms {args.min_zoom}-{args.max_zoom} → {total} tiles")
    if total > 50000:
        print(f"WARNING: {total} tiles is a lot for the public OSM server. Narrow the "
              f"area/zoom or use --tile-url for your own server.", file=sys.stderr)

    def progress(done, tot):
        print(f"  {done}/{tot} …")

    stats = bb.build_pack((lat, lon), radius, args.min_zoom, args.max_zoom, args.out,
                          tile_url=args.tile_url, delay=args.delay, progress=progress)
    size_kib = os.path.getsize(stats["path"]) // 1024 if os.path.isfile(stats["path"]) else 0
    print(f"Done: {stats['path']} — fetched={stats['fetched']} skipped={stats['skipped']} "
          f"failed={stats['failed']} (size {size_kib} KiB)")
    return 0 if stats["failed"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
