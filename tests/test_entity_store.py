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


def _ap(bssid="ff:ee:dd:cc:bb:aa", ssid="HomeWiFi", signal=-40, channel=6, crypt=0):
    return {
        "macaddr": bssid,
        "type": "Wi-Fi AP",
        "name": ssid,
        "manuf": "Netgear",
        "last_signal": signal,
        "is_ap": True,
        "beaconed_ssid": ssid,
        "beacon_channel": channel,
        "beacon_crypt": crypt,
    }


def _poll(store, devices, n, gps_fix=None, start=T0):
    """Run record_poll n times at advancing timestamps."""
    for i in range(n):
        store.record_poll(devices, gps_fix=gps_fix, now=start + timedelta(minutes=i))


# ---------------------------------------------------------------------------
# Beacon capture + network affinity (PR A)
# ---------------------------------------------------------------------------


def test_beacon_evidence_records_ap_with_rssi_stats():
    s = _store()
    _poll(s, [_ap(ssid="HomeWiFi", signal=-40)], 3)
    assert s.count("beacon_evidence") == 1
    st = s.beacon_rssi("ff:ee:dd:cc:bb:aa", "HomeWiFi")
    assert st["count"] == 3 and abs(st["mean"] - (-40.0)) < 1e-9
    s.close()


def test_network_affinity_matches_only_locally_beaconed_ssid():
    s = _store()
    client = _device(probe_ssids=["HomeWiFi", "CoffeeShop"], fp=999)
    _poll(s, [client, _ap(ssid="HomeWiFi")], 4)
    prof = s.network_affinity_profile(999)
    assert prof == {"HomeWiFi": 4}          # beaconed here -> confirmed affinity
    assert "CoffeeShop" not in prof         # not beaconed here -> no affinity
    s.close()


def test_network_affinity_empty_without_local_beacon():
    s = _store()
    _poll(s, [_device(probe_ssids=["HomeWiFi"], fp=999)], 3)   # no AP beaconing
    assert s.count("network_affinity") == 0
    assert s.network_affinity_profile(999) == {}
    s.close()


def test_beacon_capture_disabled_skips_writes(monkeypatch):
    import modules.entity_store as es
    monkeypatch.setattr(es, "_BEACON_CAPTURE_ENABLED", False)
    s = _store()
    _poll(s, [_device(probe_ssids=["HomeWiFi"], fp=1), _ap(ssid="HomeWiFi")], 2)
    assert s.count("beacon_evidence") == 0
    assert s.count("network_affinity") == 0
    s.close()


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
    s.record_poll([_device()], gps_fix={"lat": 51.5, "lon": -0.1}, now=T0)
    row = s._conn.execute(
        "SELECT lat, lon, pos_source, pos_confidence, signal FROM observations"
    ).fetchone()
    assert row["lat"] == 51.5 and row["lon"] == -0.1
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
                      current_fix={"lat": 51.5, "lon": -0.1})
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


# ---------------------------------------------------------------------------
# Retention sweep is bounded — the 2026-06 crash-loop regression tests
# (one unbounded DELETE over a ~28M-row backlog held the asyncio loop past
# systemd's watchdog, and as one transaction rolled back on every kill)
# ---------------------------------------------------------------------------


def _seed_old_rows(store, n, mac_prefix="aa:bb:cc:dd:ee"):
    """n observations at T0 (old) + one recent row; retention 7 days."""
    for i in range(n):
        store.record_poll([_device(mac=f"{mac_prefix}:{i:02x}")], now=T0)
    store.record_poll([_device()], now=T0 + timedelta(days=30))


def test_prune_batches_clear_a_multi_batch_backlog():
    s = EntityStore(":memory:", retention_days=7, prune_interval_s=10 ** 9,
                    prune_batch_rows=2, prune_time_budget_s=60.0)
    _seed_old_rows(s, 7)
    assert s.count("observations") == 8
    removed = s.prune_observations(now=T0 + timedelta(days=30))
    assert removed == 7                      # whole backlog, across 4 batches
    assert s.count("observations") == 1      # the recent row survives
    s.close()


def test_prune_time_budget_bounds_one_sweep_and_resumes():
    # Budget 0 -> the sweep stops after its first batch; the rest of the
    # backlog must survive to be drained by the NEXT sweep, so a single sweep
    # can never hold the poll thread longer than its budget.
    s = EntityStore(":memory:", retention_days=7, prune_interval_s=10 ** 9,
                    prune_batch_rows=2, prune_time_budget_s=0.0)
    _seed_old_rows(s, 6)
    first = s.prune_observations(now=T0 + timedelta(days=30))
    assert first == 2                        # exactly one batch
    assert s.count("observations") == 5      # 4 old left + 1 recent
    second = s.prune_observations(now=T0 + timedelta(days=30))
    assert second == 2                       # the next sweep resumes the backlog
    s.close()


def test_prune_commits_per_batch_so_progress_survives_a_kill(tmp_path):
    # The old single-transaction DELETE rolled back when systemd killed the
    # node mid-sweep, so a crash-looping node made NO progress on each pass.
    # Per-batch commits must leave completed batches durable even if the sweep
    # stops early (budget 0 stands in for the kill).
    db = str(tmp_path / "entities.db")
    s = EntityStore(db, retention_days=7, prune_interval_s=10 ** 9,
                    prune_batch_rows=2, prune_time_budget_s=0.0)
    _seed_old_rows(s, 6)
    s.prune_observations(now=T0 + timedelta(days=30))   # one batch, then "killed"
    s.close()
    reopened = EntityStore(db, retention_days=7, prune_interval_s=10 ** 9)
    assert reopened.count("observations") == 5           # the batch stayed deleted
    reopened.close()


def test_first_auto_sweep_after_restart_is_deferred(tmp_path):
    # A restart over a database holding a backlog must NOT sweep on the first
    # poll — that put the big delete squarely in the startup window and drove
    # the restart crash-loop. The sweep runs one interval later instead.
    db = str(tmp_path / "entities.db")
    s = EntityStore(db, retention_days=7, prune_interval_s=3600)
    s.record_poll([_device()], now=T0)
    s.close()

    later = T0 + timedelta(days=30)
    restarted = EntityStore(db, retention_days=7, prune_interval_s=3600)
    restarted.record_poll([_device()], now=later)        # first poll: deferred
    assert restarted.count("observations") == 2          # old row still there
    restarted.record_poll([_device()], now=later + timedelta(hours=2))
    assert restarted.count("observations") == 2          # T0 row swept, +1 new row
    row_ts = {r["timestamp"] for r in
              restarted._conn.execute("SELECT timestamp FROM observations")}
    assert not any(ts.startswith("2026-01-01T00:00") for ts in row_ts)
    restarted.close()


def test_file_backed_store_runs_in_wal_mode(tmp_path):
    s = EntityStore(str(tmp_path / "entities.db"))
    mode = s._conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode == "wal"
    s.close()


def test_incremental_vacuum_actually_reclaims_pages(tmp_path):
    # PRAGMA incremental_vacuum does its work as the statement is STEPPED —
    # an un-drained cursor silently reclaims nothing (the fetchall gotcha).
    s = EntityStore(str(tmp_path / "vac.db"), retention_days=7)
    s._conn.execute(
        "INSERT INTO entities (entity_type, identifier, first_seen, last_seen, obs_count) "
        "VALUES ('wifi','x','t','t',1)")
    eid = s._conn.execute("SELECT entity_id FROM entities").fetchone()[0]
    s._conn.executemany(
        "INSERT INTO observations (entity_id, timestamp) VALUES (?, ?)",
        ((eid, "2026-01-01T00:00:00+00:00") for _ in range(20000)))
    s._conn.commit()
    s._conn.execute("DELETE FROM observations")
    s._conn.commit()
    before = s._conn.execute("PRAGMA freelist_count").fetchone()[0]
    assert before > 0
    s._reclaim_pages()
    after = s._conn.execute("PRAGMA freelist_count").fetchone()[0]
    assert after < before
    s.close()


# ---------------------------------------------------------------------------
# Cross-session contact registry (P4 phase B) — durable "returning entity" memory
# ---------------------------------------------------------------------------


def test_contact_registry_first_sighting_is_not_known():
    s = _store()
    prior = s.record_contact_sighting("wifi-fp:abc", T0, "sess-1")
    assert prior["known"] is False
    assert s.count("contact_registry") == 1
    assert s.contact_registry_row("wifi-fp:abc")["visits"] == 1
    s.close()


def test_contact_registry_same_session_does_not_add_visit():
    s = _store()
    s.record_contact_sighting("wifi-fp:abc", T0, "sess-1")
    prior = s.record_contact_sighting("wifi-fp:abc", T0 + timedelta(minutes=5), "sess-1")
    assert prior["known"] is True and prior["last_session"] == "sess-1"
    row = s.contact_registry_row("wifi-fp:abc")
    assert row["visits"] == 1                 # same session -> no new visit
    assert row["distinct_days"] == 1
    s.close()


def test_contact_registry_new_session_adds_visit_and_reports_prior():
    s = _store()
    s.record_contact_sighting("wifi-fp:abc", T0, "sess-1")
    prior = s.record_contact_sighting("wifi-fp:abc", T0 + timedelta(hours=26), "sess-2")
    assert prior["known"] is True
    assert prior["last_session"] == "sess-1"
    assert prior["prior_last_seen"] == T0
    row = s.contact_registry_row("wifi-fp:abc")
    assert row["visits"] == 2                 # new session -> visit++
    assert row["distinct_days"] == 2          # crossed a UTC day boundary
    s.close()


def test_contact_registry_survives_reopen(tmp_path):
    db = str(tmp_path / "e.db")
    s1 = EntityStore(db)
    s1.record_contact_sighting("wifi-fp:xyz", T0, "sess-1")
    s1.close()
    s2 = EntityStore(db)
    prior = s2.record_contact_sighting("wifi-fp:xyz", T0 + timedelta(days=1), "sess-2")
    assert prior["known"] is True and prior["last_session"] == "sess-1"
    assert s2.contact_registry_row("wifi-fp:xyz")["visits"] == 2
    s2.close()


# ---------------------------------------------------------------------------
# Cross-PHY contact links (P4 phase C) — durable person-link memory
# ---------------------------------------------------------------------------


def test_contact_link_records_order_independent():
    s = _store()
    s.record_contact_link("wifi-fp:a", "ble-fp:b", T0)
    s.record_contact_link("ble-fp:b", "wifi-fp:a", T0 + timedelta(minutes=1))  # reversed
    assert s.count("contact_links") == 1                 # one row, not two
    assert sorted(s.known_links()[0]) == ["ble-fp:b", "wifi-fp:a"]
    s.close()


def test_known_links_round_trips_across_reopen(tmp_path):
    db = str(tmp_path / "e.db")
    s1 = EntityStore(db)
    s1.record_contact_link("wifi-fp:a", "ble-fp:b", T0)
    s1.record_contact_link("wifi-fp:c", "ble-fp:d", T0)
    s1.close()
    s2 = EntityStore(db)
    links = {tuple(sorted(p)) for p in s2.known_links()}
    assert links == {("ble-fp:b", "wifi-fp:a"), ("ble-fp:d", "wifi-fp:c")}
    s2.close()
