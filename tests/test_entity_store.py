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
# pnl_evidence — PNL accumulates per IE hash, ACROSS MAC rotation
# ---------------------------------------------------------------------------

def test_pnl_accumulates_across_rotated_macs_sharing_ie_hash():
    s = _store()
    # Same device (probe_fingerprint 777), two different (rotated) MACs, each probing
    # a different slice of its preferred-network list.
    s.record_poll([_device(mac="aa:11:11:11:11:11", probe_ssids=["Home", "Work"], fp=777)], now=T0)
    s.record_poll([_device(mac="bb:22:22:22:22:22", probe_ssids=["Work", "Cafe"], fp=777)],
                  now=T0 + timedelta(minutes=5))
    # Union of the PNL is recovered under the single stable anchor.
    assert set(s.accumulated_pnl(777)) == {"Home", "Work", "Cafe"}
    assert s.count("pnl_evidence") == 3   # one row per distinct ssid, not per mac
    s.close()


def test_pnl_empty_without_ie_hash():
    s = _store()
    _poll(s, [_device(probe_ssids=["Home"], fp=0)], 3)   # no IE fingerprint
    assert s.count("pnl_evidence") == 0
    assert s.accumulated_pnl(0) == []
    s.close()


def test_distinctive_anchors_picks_rarest_omits_common_only():
    s = _store()
    # 'attwifi' is probed by 3 distinct IE hashes (common, df=3); 'Home'/'Work' df=1.
    s.record_poll([_device(mac="aa:11:11:11:11:11", probe_ssids=["Home", "attwifi"], fp=100)], now=T0)
    s.record_poll([_device(mac="bb:22:22:22:22:22", probe_ssids=["Work", "attwifi"], fp=200)], now=T0)
    s.record_poll([_device(mac="cc:33:33:33:33:33", probe_ssids=["attwifi"], fp=300)], now=T0)
    anchors = s.distinctive_anchors(max_df=2)
    # 100 -> Home, 200 -> Work; 300 probes only the common SSID -> NO anchor (omitted).
    assert anchors == {100: "Home", 200: "Work"}
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


# ---------------------------------------------------------------------------
# Observation history retention — bound the only table that grows by design
# ---------------------------------------------------------------------------


def test_prune_removes_observations_older_than_retention():
    # Large interval so the auto-sweep inside record_poll doesn't fire early.
    s = EntityStore(":memory:", retention_days=7, prune_interval_s=10 ** 9)
    s.record_poll([_device()], now=T0)
    recent = T0 + timedelta(days=10)
    s.record_poll([_device()], now=recent)
    assert s.count("observations") == 2
    removed = s.prune_observations(now=recent)
    assert removed == 1                              # the T0 row aged out
    assert s.count("observations") == 1
    s.close()


def test_prune_disabled_keeps_history_forever():
    s = EntityStore(":memory:", retention_days=0)
    s.record_poll([_device()], now=T0)
    s.record_poll([_device()], now=T0 + timedelta(days=365))
    assert s.prune_observations(now=T0 + timedelta(days=365)) == 0
    assert s.count("observations") == 2
    s.close()


def test_record_poll_auto_prunes_old_history():
    # prune_interval_s=0 makes every poll eligible for the sweep.
    s = EntityStore(":memory:", retention_days=7, prune_interval_s=0)
    s.record_poll([_device()], now=T0)
    s.record_poll([_device()], now=T0 + timedelta(days=30))
    assert s.count("observations") == 1              # T0 row swept on the second poll
    s.close()


def test_observation_timestamp_index_created():
    s = _store()
    names = {r["name"] for r in s._conn.execute("PRAGMA index_list('observations')")}
    assert "idx_obs_timestamp" in names
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
# Orchestrator integration — recording fires at the poll site for EVERY mode
# (the gap this phase closes: a fixed node runs FixedScoring, not
# PersistenceEngine, so the recording must not live inside either scorer).
# ---------------------------------------------------------------------------

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock


def _make_orch(persistence, entity_store, devices, session_dir, current_fix=None):
    from modules.orchestrator import SensorOrchestrator
    km = MagicMock()
    km.poll_devices = AsyncMock(return_value=devices)
    pa = MagicMock(); pa.analyze = MagicMock(return_value=[])
    orch = SensorOrchestrator(
        gps=MagicMock(), kismet=km, adsb=MagicMock(), drone_rf=MagicMock(),
        sdr_coordinator=MagicMock(), alert_backend=MagicMock(), rate_limiter=MagicMock(),
        persistence=persistence, probe_analyzer=pa, gui_server=None,
        entity_store=entity_store, remote_id=None,
        session_id="test", session_start=T0, session_dir=Path(session_dir),
        sdr_mode=MagicMock(), stop_event=asyncio.Event(),
        gps_poll_interval=1, adsb_poll_interval=5, kismet_poll_interval=30,
        drone_poll_interval=5, health_banner_interval=300,
        max_reconnect_attempts=3, reconnect_interval=5,
        modules_active={"kismet": True},
    )
    orch._current_fix = current_fix
    return orch


def test_records_in_fixed_mode(tmp_path):
    # THE gap this phase closes: a fixed node (FixedScoring) now records.
    from modules.fixed_scoring import FixedScoring
    s = _store()
    eng = FixedScoring(db_path=":memory:", baseline_hours=72)   # learning -> [] events
    orch = _make_orch(eng, s, [_device(probe_ssids=["HomeWiFi"])], tmp_path,
                      current_fix={"lat": 21.4, "lon": -157.7})
    asyncio.run(orch._poll_kismet())
    assert s.count("entities") == 1
    assert s.probe_evidence_row("aa:bb:cc:dd:ee:ff", "HomeWiFi")["probe_count"] == 1
    assert s.count("observations") == 1
    s.close()


def test_records_in_mobile_mode(tmp_path):
    s = _store()
    eng = PersistenceEngine()                                   # below threshold -> []
    orch = _make_orch(eng, s, [_device(probe_ssids=["HomeWiFi"])], tmp_path)
    asyncio.run(orch._poll_kismet())
    assert s.count("entities") == 1
    assert s.probe_evidence_row("aa:bb:cc:dd:ee:ff", "HomeWiFi")["probe_count"] == 1
    s.close()


def test_flat_line_property_at_poll_site_fixed_mode(tmp_path):
    # 5 polls under fixed mode: upsert tables flat, observations grows.
    from modules.fixed_scoring import FixedScoring
    s = _store()
    eng = FixedScoring(db_path=":memory:", baseline_hours=72)
    orch = _make_orch(eng, s, [_device(probe_ssids=["HomeWiFi"])], tmp_path)
    for _ in range(5):
        asyncio.run(orch._poll_kismet())
    assert s.count("probe_evidence") == 1
    assert s.probe_evidence_row("aa:bb:cc:dd:ee:ff", "HomeWiFi")["probe_count"] == 5
    assert s.count("device_fingerprint") == 1
    assert s.count("entities") == 1
    assert s.count("observations") == 5
    s.close()


def test_store_failure_at_poll_site_is_non_fatal(tmp_path):
    class _Boom:
        def record_poll(self, *a, **k):
            raise RuntimeError("disk gone")
    eng = PersistenceEngine()
    orch = _make_orch(eng, _Boom(), [_device()], tmp_path)
    asyncio.run(orch._poll_kismet())   # must not raise


def test_no_store_is_a_clean_noop(tmp_path):
    eng = PersistenceEngine()
    orch = _make_orch(eng, None, [_device()], tmp_path)
    asyncio.run(orch._poll_kismet())   # entity_store None -> skipped, no crash


# ---------------------------------------------------------------------------
# Contact designator — persisted, stable instance numbers
# ---------------------------------------------------------------------------

def test_contact_number_sequential_within_group():
    s = _store()
    assert s.assign_contact_number("wifi-fp:a", "AP-NETGEAR") == 1
    assert s.assign_contact_number("wifi-fp:b", "AP-NETGEAR") == 2
    assert s.assign_contact_number("wifi-fp:c", "AP-NETGEAR") == 3


def test_contact_number_stable_for_same_identity():
    s = _store()
    first = s.assign_contact_number("wifi-fp:a", "AP-NETGEAR")
    # re-assigning the same identity returns the same number, even after others join
    s.assign_contact_number("wifi-fp:b", "AP-NETGEAR")
    assert s.assign_contact_number("wifi-fp:a", "AP-NETGEAR") == first


def test_contact_number_independent_per_group():
    s = _store()
    assert s.assign_contact_number("wifi-fp:a", "AP-NETGEAR") == 1
    assert s.assign_contact_number("ble-fp:x", "BLE-Apple") == 1   # separate group resets


def test_contact_number_survives_reopen():
    import tempfile, os
    d = tempfile.mkdtemp()
    path = os.path.join(d, "e.db")
    s1 = EntityStore(path)
    n = s1.assign_contact_number("wifi-fp:a", "AP-NETGEAR")
    s1.close()
    s2 = EntityStore(path)
    assert s2.assign_contact_number("wifi-fp:a", "AP-NETGEAR") == n   # stable across restart
    s2.close()
