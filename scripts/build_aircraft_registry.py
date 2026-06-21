#!/usr/bin/env python3
"""build_aircraft_registry.py — build the offline ICAO→registration SQLite.

Run this ONCE, OFF-NODE (on a connected machine), then copy the resulting .sqlite to
the node at data/registry/aircraft.sqlite (or AIRCRAFT_REGISTRY_DB). The node then
resolves ACARS tail ↔ ADS-B contact offline, with no outbound queries — the same
opsec stance as the offline basemap. (Online adsb.lol enrichment augments it when the
node has internet and ADSBXLOL_API_KEY is set.)

Default source: the OpenSky aircraft database CSV (public). Columns used:
``icao24``, ``registration``, ``typecode``, ``operator`` (header names vary slightly
across snapshots; we map flexibly).

    # build from the public OpenSky CSV
    python3 scripts/build_aircraft_registry.py --out data/registry/aircraft.sqlite

    # or from an already-downloaded CSV
    python3 scripts/build_aircraft_registry.py --csv aircraftDatabase.csv --out aircraft.sqlite

OPSEC: this downloads a full public dataset (not per-aircraft queries), so it reveals
nothing about what the node tracks. Still, prefer running it off-node.
"""

import argparse
import csv
import io
import os
import sqlite3
import sys
import urllib.request

DEFAULT_CSV_URL = "https://opensky-network.org/datasets/metadata/aircraftDatabase.csv"
USER_AGENT = "PassiveVigilance-registry-build/1.0"

# Flexible header mapping (OpenSky snapshots have varied slightly over time).
_ICAO_KEYS = ("icao24", "icao", "icaohex")
_REG_KEYS = ("registration", "reg", "tail")
_TYPE_KEYS = ("typecode", "type", "icaoaircrafttype", "model")
_OP_KEYS = ("operator", "operatorcallsign", "owner")


def _pick(row, keys):
    for k in keys:
        if k in row and row[k]:
            return row[k].strip()
    return ""


def _open_csv(args):
    if args.csv:
        return open(args.csv, newline="", encoding="utf-8", errors="ignore")
    print(f"Downloading {args.url} …")
    req = urllib.request.Request(args.url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=120) as resp:
        data = resp.read().decode("utf-8", "ignore")
    return io.StringIO(data)


def main(argv=None):
    p = argparse.ArgumentParser(description="Build the offline aircraft registry SQLite.")
    p.add_argument("--out", default="data/registry/aircraft.sqlite")
    p.add_argument("--csv", help="path to a local CSV (skip the download)")
    p.add_argument("--url", default=DEFAULT_CSV_URL, help="CSV URL (default OpenSky)")
    args = p.parse_args(argv)

    os.makedirs(os.path.dirname(os.path.abspath(args.out)) or ".", exist_ok=True)
    if os.path.exists(args.out):
        os.remove(args.out)
    conn = sqlite3.connect(args.out)
    conn.executescript(
        "CREATE TABLE aircraft (icao TEXT PRIMARY KEY, registration TEXT, "
        "aircraft_type TEXT, operator TEXT);"
    )

    rows = 0
    fh = _open_csv(args)
    try:
        reader = csv.DictReader(fh)
        # Normalize headers to lowercase for flexible matching.
        reader.fieldnames = [(f or "").strip().lower() for f in (reader.fieldnames or [])]
        batch = []
        for raw in reader:
            row = {(k or "").strip().lower(): v for k, v in raw.items()}
            icao = _pick(row, _ICAO_KEYS).lower()
            reg = _pick(row, _REG_KEYS)
            if not icao or not reg:
                continue
            batch.append((icao, reg, _pick(row, _TYPE_KEYS), _pick(row, _OP_KEYS)))
            if len(batch) >= 5000:
                conn.executemany("INSERT OR REPLACE INTO aircraft VALUES (?,?,?,?)", batch)
                rows += len(batch)
                batch = []
        if batch:
            conn.executemany("INSERT OR REPLACE INTO aircraft VALUES (?,?,?,?)", batch)
            rows += len(batch)
    finally:
        fh.close()
    conn.commit()
    conn.close()
    size_kib = os.path.getsize(args.out) // 1024
    print(f"Done: {args.out} — {rows} aircraft ({size_kib} KiB). "
          f"Copy it to the node at data/registry/aircraft.sqlite")
    return 0 if rows else 1


if __name__ == "__main__":
    sys.exit(main())
