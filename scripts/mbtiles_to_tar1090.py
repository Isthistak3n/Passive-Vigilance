#!/usr/bin/env python3
"""mbtiles_to_tar1090.py — unpack an MBTiles pack into tar1090's offline tile tree.

The Passive Vigilance GUI serves the basemap straight from the MBTiles file, but
tar1090 wants an XYZ directory tree at ``<tar1090 html>/osm_tiles_offline/{z}/{x}/{y}.png``
and a ``offlineMapDetail = <maxzoom>`` line in its ``config.js``. This exports the same
pack to that tree so both maps share ONE basemap source (build it once with
``fetch_basemap.py``, serve it in the GUI, export it here for tar1090).

    sudo python3 scripts/mbtiles_to_tar1090.py data/tiles/basemap.mbtiles

Then add to /usr/local/share/tar1090/html/config.js (maxzoom from the run output):

    offlineMapDetail = 16;

MBTiles rows are TMS (y from the bottom); tar1090 requests XYZ (y from the top), so
this flips ``y -> 2^z-1-y`` on the way out.
"""

import argparse
import os
import sqlite3
import sys

DEFAULT_DEST = "/usr/local/share/tar1090/html/osm_tiles_offline"


def main(argv=None):
    p = argparse.ArgumentParser(description="Export MBTiles to a tar1090 offline tree.")
    p.add_argument("mbtiles", help="source .mbtiles (e.g. data/tiles/basemap.mbtiles)")
    p.add_argument("--dest", default=DEFAULT_DEST,
                   help=f"tar1090 offline tile dir (default {DEFAULT_DEST})")
    args = p.parse_args(argv)

    if not os.path.isfile(args.mbtiles):
        print(f"no such MBTiles: {args.mbtiles}", file=sys.stderr)
        return 1

    conn = sqlite3.connect(f"file:{args.mbtiles}?mode=ro", uri=True)
    rows = conn.execute(
        "SELECT zoom_level, tile_column, tile_row, tile_data FROM tiles"
    )
    written = 0
    maxz = -1
    for z, x, tms_y, blob in rows:
        y = (1 << z) - 1 - tms_y            # TMS -> XYZ
        d = os.path.join(args.dest, str(z), str(x))
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, f"{y}.png"), "wb") as fh:
            fh.write(blob)
        written += 1
        maxz = max(maxz, z)
    conn.close()
    print(f"Exported {written} tiles to {args.dest} (max zoom {maxz}).")
    print(f"Now set 'offlineMapDetail = {maxz};' in tar1090's config.js and reload.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
