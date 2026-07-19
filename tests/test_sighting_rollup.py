"""Tests for the sighting rollup (device_state fold + FIXED/MOBILE classifier).

The rollup runs against a real file-backed EntityStore DB (it opens its own
connection, as in production); :memory: stores can't be shared across
connections, so every test uses tmp_path.
"""

import json
import math
from datetime import datetime, timedelta, timezone

from modules.entity_store import EntityStore
from modules.sighting_rollup import SightingRollup, classify_node_type

T0 = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
NOW = T0 + timedelta(days=30)          # rollup "tonight"; T0-era rows are aged


def _device(mac="aa:bb:cc:dd:ee:ff", fp=None, signal=-55):
    return {"macaddr": mac, "type": "Wi-Fi Client", "name": "", "manuf": "Acme",
            "last_signal": signal, "probe_ssids": [], "probe_fingerprint": fp,
            "num_probed_ssids": 0}


def _ap(bssid="ff:ee:dd:cc:bb:aa", ssid="HomeWiFi", signal=-40):
    return {"macaddr": bssid, "type": "Wi-Fi AP", "name": ssid, "manuf": "N",
            "last_signal": signal, "is_ap": True, "beaconed_ssid": ssid,
            "beacon_channel": 6, "beacon_crypt": 0, "probe_ssids": [],
            "probe_fingerprint": None, "num_probed_ssids": 0}


def _store(tmp_path):
    return EntityStore(str(tmp_path / "e.db"))


def _rollup(tmp_path, **kw):
    return SightingRollup(str(tmp_path / "e.db"), **kw)


def _state(tmp_path, key):
    import sqlite3
    conn = sqlite3.connect(str(tmp_path / "e.db"))
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT * FROM device_state WHERE identity_key = ?", (key,)).fetchone()
    conn.close()
    return row


# ---------------------------------------------------------------------------
# Fold mechanics
# ---------------------------------------------------------------------------


def test_fold_moves_aged_rows_into_state_and_deletes_them(tmp_path):
    s = _store(tmp_path)
    for i in range(3):
        s.record_poll([_device()], now=T0 + timedelta(minutes=i))     # aged
    s.record_poll([_device()], now=NOW - timedelta(hours=1))          # recent
    s.close()

    summary = _rollup(tmp_path).run(now=NOW)
    assert summary == {"folded_rows": 3, "identities": 1, "exhausted": True}

    row = _state(tmp_path, "mac:aa:bb:cc:dd:ee:ff")
    assert row["total_sightings"] == 3
    assert row["first_seen"].startswith("2026-01-01")

    s = EntityStore(str(tmp_path / "e.db"))
    assert s.count("observations") == 1          # only the in-window row remains
    s.close()


def test_fold_is_idempotent_second_run_folds_nothing(tmp_path):
    s = _store(tmp_path)
    s.record_poll([_device()], now=T0)
    s.close()
    r = _rollup(tmp_path)
    assert r.run(now=NOW)["folded_rows"] == 1
    assert r.run(now=NOW)["folded_rows"] == 0
    assert _state(tmp_path, "mac:aa:bb:cc:dd:ee:ff")["total_sightings"] == 1


def test_fold_drains_backlog_across_small_batches(tmp_path):
    s = _store(tmp_path)
    for i in range(7):
        s.record_poll([_device()], now=T0 + timedelta(minutes=i))
    s.close()
    summary = _rollup(tmp_path, batch_rows=2).run(now=NOW)
    assert summary["folded_rows"] == 7 and summary["exhausted"]
    assert _state(tmp_path, "mac:aa:bb:cc:dd:ee:ff")["total_sightings"] == 7


def test_hour_and_day_counts_and_distinct_days(tmp_path):
    s = _store(tmp_path)
    s.record_poll([_device()], now=T0.replace(hour=9))
    s.record_poll([_device()], now=T0.replace(hour=9, minute=30))
    s.record_poll([_device()], now=(T0 + timedelta(days=1)).replace(hour=21))
    s.close()
    _rollup(tmp_path).run(now=NOW)
    row = _state(tmp_path, "mac:aa:bb:cc:dd:ee:ff")
    hours = json.loads(row["hour_counts"])
    assert hours[9] == 2 and hours[21] == 1 and sum(hours) == 3
    assert row["distinct_days"] == 2
    assert json.loads(row["day_counts"])["2026-01-01"] == 2


def test_rssi_welford_matches_manual_and_skips_placeholder_zero(tmp_path):
    s = _store(tmp_path)
    sigs = [-50, -60, -70, 0, None]           # 0/None must be skipped
    for i, sig in enumerate(sigs):
        s.record_poll([_device(signal=sig)], now=T0 + timedelta(minutes=i))
    s.close()
    _rollup(tmp_path, batch_rows=2).run(now=NOW)  # merge across batches too
    row = _state(tmp_path, "mac:aa:bb:cc:dd:ee:ff")
    assert row["rssi_n"] == 3
    assert row["rssi_mean"] == -60.0
    assert math.isclose(math.sqrt(row["rssi_m2"] / 3), 8.1649, rel_tol=1e-3)


def test_fp_identity_merges_rotated_macs_into_one_state_row(tmp_path):
    s = _store(tmp_path)
    s.record_poll([_device(mac="aa:11:11:11:11:11", fp=777)], now=T0)
    s.record_poll([_device(mac="bb:22:22:22:22:22", fp=777)],
                  now=T0 + timedelta(minutes=5))
    s.close()
    _rollup(tmp_path).run(now=NOW)
    row = _state(tmp_path, "fp:777")
    assert row is not None and row["total_sightings"] == 2
    assert _state(tmp_path, "mac:aa:11:11:11:11:11") is None


def test_location_clusters_and_distinct_locations(tmp_path):
    s = _store(tmp_path)
    fix_a = {"lat": 51.5000, "lon": -0.1000}
    fix_b = {"lat": 51.5500, "lon": -0.1000}   # ~5.5 km away — new cluster
    for i in range(6):
        s.record_poll([_device()], gps_fix=fix_a, now=T0 + timedelta(minutes=i))
    for i in range(6):
        s.record_poll([_device()], gps_fix=fix_b,
                      now=T0 + timedelta(hours=1, minutes=i))
    s.close()
    _rollup(tmp_path).run(now=NOW)
    row = _state(tmp_path, "mac:aa:bb:cc:dd:ee:ff")
    assert row["distinct_locations"] == 2
    assert row["node_type"] == "mobile"        # ≥2 locations, ≥10 sightings


def test_learning_member_set_from_learning_end_and_sticky(tmp_path):
    s = _store(tmp_path)
    s.record_poll([_device()], now=T0)
    s.record_poll([_device(mac="cc:33:33:33:33:33")],
                  now=T0 + timedelta(days=10))       # first seen post-learning
    s.close()
    _rollup(tmp_path).run(now=NOW, learning_end=T0 + timedelta(days=3))
    assert _state(tmp_path, "mac:aa:bb:cc:dd:ee:ff")["learning_member"] == 1
    assert _state(tmp_path, "mac:cc:33:33:33:33:33")["learning_member"] == 0


def test_last_run_recorded(tmp_path):
    s = _store(tmp_path)
    s.record_poll([_device()], now=T0)
    s.close()
    r = _rollup(tmp_path)
    assert r.last_run() is None
    r.run(now=NOW)
    assert r.last_run() == NOW


# ---------------------------------------------------------------------------
# Classifier
# ---------------------------------------------------------------------------


def _cls(**kw):
    base = dict(is_ap=False, total_sightings=100, active_hours=4,
                distinct_days=1, distinct_locations=1, rssi_n=50, rssi_std=3.0)
    base.update(kw)
    return classify_node_type(**base)


def test_classifier_ap_is_always_fixed():
    assert _cls(is_ap=True, total_sightings=1) == "fixed"


def test_classifier_thin_evidence_is_unknown():
    assert _cls(total_sightings=5, active_hours=24, distinct_days=30) == "unknown"


def test_classifier_round_the_clock_multi_day_steady_rssi_is_fixed():
    assert _cls(active_hours=22, distinct_days=7) == "fixed"


def test_classifier_high_rssi_variance_blocks_the_fixed_call():
    assert _cls(active_hours=22, distinct_days=7, rssi_std=15.0) == "unknown"


def test_classifier_multi_location_is_mobile():
    assert _cls(distinct_locations=3) == "mobile"


def test_classifier_intermittent_recurrence_is_mobile():
    assert _cls(active_hours=3, distinct_days=4) == "mobile"


def test_classifier_ap_beacon_evidence_marks_fixed_end_to_end(tmp_path):
    s = _store(tmp_path)
    s.record_poll([_ap()], now=T0)
    s.close()
    _rollup(tmp_path).run(now=NOW)
    row = _state(tmp_path, "mac:ff:ee:dd:cc:bb:aa")
    assert row["is_ap"] == 1 and row["node_type"] == "fixed"
