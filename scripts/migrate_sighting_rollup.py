#!/usr/bin/env python3
"""One-time offline migration to the sighting-rollup model
(docs/rollup-investigation.md §E).

Collapses the existing observation backlog into device_state rows, optionally
seeds hour/RSSI aggregates from a fixed-node baseline DB, trims observations to
the retention window, and optionally VACUUMs the file (which also adopts
incremental auto-vacuum on the rebuilt file).

RUN WITH THE NODE STOPPED (`sudo systemctl stop passive-vigilance`) — the fold
tolerates a live writer, but VACUUM and a truthful "done" report do not.
Keep a copy first, following the repo convention:
    cp data/entities.db data/entities.db.pre-rollup-$(date +%Y%m%d)

Usage:
    pv-python scripts/migrate_sighting_rollup.py \
        --db data/entities.db [--baseline data/baseline.db] [--vacuum]

The fold is idempotent and abortable: folded rows are deleted in the same
transaction that updates their state row, so a rerun after an abort resumes
where it left off and double-counts nothing.
"""

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from modules.sighting_rollup import SightingRollup, _iso, _parse_iso  # noqa: E402


def seed_from_baseline(entities_db: str, baseline_db: str) -> int:
    """Seed device_state from a fixed-node baseline: hour_mask becomes 0/1 hour
    counts, the banked RSSI mean/variance becomes an approximate Welford triple,
    and profiles first seen inside the learning window are marked learning
    members. Only fills rows the fold didn't already create richer data for
    (existing state rows just gain the learning flag and any missing hours)."""
    src = sqlite3.connect(baseline_db)
    src.row_factory = sqlite3.Row
    dst = sqlite3.connect(entities_db)
    dst.row_factory = sqlite3.Row
    SightingRollup._ensure_schema(dst)

    meta = src.execute(
        "SELECT learning_start, baseline_hours FROM baseline_meta WHERE id = 1"
    ).fetchone()
    freeze = None
    if meta:
        start = _parse_iso(meta["learning_start"])
        if start is not None:
            freeze = start + timedelta(hours=float(meta["baseline_hours"]))

    seeded = 0
    for p in src.execute("SELECT * FROM device_profiles"):
        key = p["key"]  # already mac:/fp: form (FixedScoring._device_key)
        # The hour mask and the RSSI Welford accumulator live INSIDE the
        # time_histogram column as JSON (BaselineStore packs its whole
        # accumulator state there); the bare signal_mean/signal_var columns are
        # a derived snapshot, kept only as a fallback for old rows.
        try:
            state = json.loads(p["time_histogram"]) if p["time_histogram"] else {}
        except (TypeError, ValueError):
            state = {}
        mask = int(state.get("hour_mask") or 0)
        hours = [1 if mask >> h & 1 else 0 for h in range(24)]
        learning = 0
        fs = _parse_iso(p["first_seen"])
        if freeze is not None and fs is not None and fs <= freeze:
            learning = 1
        n = int(state.get("sig_n") or 0)
        mean = float(state.get("sig_mean") or 0.0)
        m2 = float(state.get("sig_m2") or 0.0)
        if n == 0 and p["signal_mean"] is not None:
            n = p["observation_count"] or 0
            mean = p["signal_mean"]
            m2 = (p["signal_var"] or 0.0) * n
        row = dst.execute(
            "SELECT identity_key, hour_counts, learning_member FROM device_state "
            "WHERE identity_key = ?", (key,)).fetchone()
        if row:
            merged = [max(a, b) for a, b in
                      zip(json.loads(row["hour_counts"]) or [0] * 24, hours)]
            dst.execute(
                "UPDATE device_state SET hour_counts = ?, learning_member = ? "
                "WHERE identity_key = ?",
                (json.dumps(merged), max(row["learning_member"], learning), key))
        else:
            dst.execute(
                "INSERT INTO device_state (identity_key, entity_type, first_seen, "
                "last_seen, total_sightings, learning_member, hour_counts, "
                "rssi_n, rssi_mean, rssi_m2, mac_type) "
                "VALUES (?, 'wifi', ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (key, p["first_seen"], p["last_seen"],
                 p["observation_count"] or 0, learning,
                 json.dumps(hours), n, mean, m2, p["mac_type"] or "static"))
        seeded += 1
    dst.commit()
    dst.close()
    src.close()
    return seeded


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--db", default="data/entities.db")
    ap.add_argument("--baseline", default=None,
                    help="fixed-node baseline DB to seed hour/RSSI aggregates from")
    ap.add_argument("--retention-days", type=int, default=None,
                    help="override ENTITY_SIGHTING_RETENTION_DAYS for the trim")
    ap.add_argument("--vacuum", action="store_true",
                    help="VACUUM the file afterwards (node must be stopped)")
    args = ap.parse_args()

    if not Path(args.db).exists():
        print(f"error: {args.db} not found", file=sys.stderr)
        return 1

    rollup = SightingRollup(args.db, retention_days=args.retention_days,
                            time_budget_s=86400)  # offline: no budget pressure
    now = datetime.now(timezone.utc)
    summary = rollup.run(now=now)
    print(f"folded {summary['folded_rows']} observation(s) into "
          f"{summary['identities']} device state row(s); "
          f"exhausted={summary['exhausted']}")

    if args.baseline:
        if Path(args.baseline).exists():
            print(f"seeded {seed_from_baseline(args.db, args.baseline)} "
                  f"baseline profile(s)")
        else:
            print(f"warning: baseline {args.baseline} not found — skipped",
                  file=sys.stderr)

    if args.vacuum:
        conn = sqlite3.connect(args.db)
        conn.execute("PRAGMA auto_vacuum=INCREMENTAL")
        conn.execute("VACUUM")
        conn.close()
        print("vacuumed")

    conn = sqlite3.connect(args.db)
    left = conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0]
    states = conn.execute("SELECT COUNT(*) FROM device_state").fetchone()[0]
    conn.close()
    print(f"done: {states} device state row(s), {left} observation row(s) "
          f"remain in the retention window (stamped {_iso(now)})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
