"""Tests for the Phase A entity/observation store + the PersistenceEngine write path.

The headline test is the probe_evidence upsert: the same (mac, ssid) seen over
many polls must produce exactly ONE row with a climbing probe_count — proof the
upsert keys correctly and does not recreate the memory growth on disk.
"""

from datetime import datetime, timedelta, timezone

from modules.entity_store import EntityStore
from modules.persistence import PersistenceEngine

T0 = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)


def _store():
    return EntityStore(":memory:")


def _device(mac="aa:bb:cc:dd:ee:ff", probe_ssids=None, fp=12345, num=2, signal=-55):
    return {
        "macaddr": mac,
        "type": "Wi-Fi Client",
        "name": "",
        "manuf": "Acme",
        "last_signal": signal,
        "probe_ssids": [] if probe_ssids is None else probe_ssids,
        "probe_fingerprint": fp,
        "num_probed_ssids": num,
    }


def _poll(store, devices, n, gps_fix=None, start=T0):
    """Run record_poll n times at advancing timestamps."""
    for i in range(n):
        store.record_poll(devices, gps_fix=gps_fix, now=start + timedelta(minutes=i))


# ---------------------------------------------------------------------------
# probe_evidence — THE key upsert test
# ---------------------------------------------------------------------------


def test_probe_evidence_upsert_same_pair_over_5_polls_one_row():
    s = _store()
    dev = _device(probe_ssids=["HomeWiFi"])
    _poll(s, [dev], 5)
    assert s.count("probe_evidence") == 1            # ONE row, not five
    row = s.probe_evidence_row("aa:bb:cc:dd:ee:ff", "HomeWiFi")
    assert row["probe_count"] == 5                   # incremented each poll
    assert row["first_seen"] == T0.isoformat()       # first_seen pinned
    assert row["last_seen"] == (T0 + timedelta(minutes=4)).isoformat()  # advanced
    s.close()


def test_wildcard_and_blank_never_create_probe_evidence():
    s = _store()
    _poll(s, [_device(probe_ssids=["", "   ", "\t"])], 3)
    assert s.count("probe_evidence") == 0
    # mixed: only the named one persists
    s2 = _store()
    _poll(s2, [_device(probe_ssids=["", "RealNet"])], 3)
    assert s2.count("probe_evidence") == 1
    assert s2.probe_evidence_row("aa:bb:cc:dd:ee:ff", "RealNet")["probe_count"] == 3
    s.close(); s2.close()


def test_multiple_named_ssids_are_distinct_rows():
    s = _store()
    _poll(s, [_device(probe_ssids=["NetA", "NetB", "NetC"])], 2)
    assert s.count("probe_evidence") == 3
    for ssid in ("NetA", "NetB", "NetC"):
        assert s.probe_evidence_row("aa:bb:cc:dd:ee:ff", ssid)["probe_count"] == 2
    s.close()


# ---------------------------------------------------------------------------
# device_fingerprint — one row per mac, fields updated
# ---------------------------------------------------------------------------


def test_device_fingerprint_upsert_one_row_updates_fields():
    s = _store()
    s.record_poll([_device(fp=111, num=1)], now=T0)
    s.record_poll([_device(fp=222, num=3)], now=T0 + timedelta(minutes=5))
    assert s.count("device_fingerprint") == 1
    row = s.device_fingerprint_row("aa:bb:cc:dd:ee:ff")
    assert row["probe_fingerprint"] == 222           # latest value
    assert row["num_probed_ssids"] == 3
    assert row["first_seen"] == T0.isoformat()        # pinned
    assert row["last_seen"] == (T0 + timedelta(minutes=5)).isoformat()
    s.close()


def test_device_fingerprint_row_for_wildcard_only_device():
    # A device with no named SSIDs still gets a fingerprint row (catch-all).
    s = _store()
    _poll(s, [_device(probe_ssids=[""], fp=999, num=1)], 4)
    assert s.count("probe_evidence") == 0
    assert s.count("device_fingerprint") == 1
    assert s.device_fingerprint_row("aa:bb:cc:dd:ee:ff")["probe_fingerprint"] == 999
    s.close()


# ---------------------------------------------------------------------------
# entities — one row per (type, identifier), obs_count increments
# ---------------------------------------------------------------------------


def test_entities_upsert_one_row_obs_count_increments():
    s = _store()
    _poll(s, [_device()], 5)
    assert s.count("entities") == 1
    row = s.entity_row("aa:bb:cc:dd:ee:ff")
    assert row["entity_type"] == "wifi"
    assert row["obs_count"] == 5
    assert row["last_seen"] == (T0 + timedelta(minutes=4)).isoformat()
    s.close()


# ---------------------------------------------------------------------------
# observations — history grows by design (one row per device per poll)
# ---------------------------------------------------------------------------


def test_observations_grow_one_row_per_poll():
    s = _store()
    _poll(s, [_device()], 5)
    assert s.count("observations") == 5              # history, not deduped
    s.close()


def test_observation_position_null_without_fix():
    s = _store()
    s.record_poll([_device()], gps_fix=None, now=T0)
    row = s._conn.execute("SELECT lat, lon, pos_source, pos_confidence FROM observations").fetchone()
    assert row["lat"] is None and row["lon"] is None
    assert row["pos_source"] is None and row["pos_confidence"] is None
    s.close()


def test_observation_position_gps_node_with_fix():
    s = _store()
    s.record_poll([_device()], gps_fix={"lat": 21.4, "lon": -157.7}, now=T0)
    row = s._conn.execute(
        "SELECT lat, lon, pos_source, pos_confidence, signal FROM observations"
    ).fetchone()
    assert row["lat"] == 21.4 and row["lon"] == -157.7
    assert row["pos_source"] == "gps_node"
    assert row["pos_confidence"] == 1.0
    assert row["signal"] == -55
    s.close()


def test_blank_mac_skipped_entirely():
    s = _store()
    s.record_poll([{"macaddr": "", "probe_ssids": ["X"]}], now=T0)
    assert s.count("entities") == 0
    assert s.count("observations") == 0
    assert s.count("probe_evidence") == 0
    s.close()


# ---------------------------------------------------------------------------
# PersistenceEngine integration — additive, default-off
# ---------------------------------------------------------------------------


def test_persistence_engine_writes_when_store_injected():
    s = _store()
    eng = PersistenceEngine(entity_store=s)
    dev = _device(probe_ssids=["HomeWiFi"])
    for _ in range(3):
        eng.update([dev], gps_fix={"lat": 1.0, "lon": 2.0})
    assert s.count("entities") == 1
    assert s.entity_row("aa:bb:cc:dd:ee:ff")["obs_count"] == 3
    assert s.probe_evidence_row("aa:bb:cc:dd:ee:ff", "HomeWiFi")["probe_count"] == 3
    assert s.count("observations") == 3
    s.close()


def test_persistence_engine_default_has_no_entity_store():
    # No store injected -> the entity path is a no-op; scoring still works.
    eng = PersistenceEngine()
    assert eng._entity_store is None
    events = eng.update([_device(probe_ssids=["X"])], gps_fix=None)
    assert isinstance(events, list)   # update returns normally, no crash


def test_persistence_engine_store_failure_is_non_fatal():
    class _Boom:
        def record_poll(self, *a, **k):
            raise RuntimeError("disk gone")
    eng = PersistenceEngine(entity_store=_Boom())
    # update() must still return normally despite the store blowing up.
    events = eng.update([_device()], gps_fix=None)
    assert isinstance(events, list)
