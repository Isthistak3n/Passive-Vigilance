"""Tests for the SurveyStore — the recon-pair tasking/observation/finding store.

The headline behaviours: a tasking de-dupes on identity so re-flagging doesn't
flood the watchlist; observations cluster into bed-down findings with gap-tolerant
dwell (a blind gap must NOT inflate time-present), return counting, and an
overnight flag; and the store is safe to read from a second thread (the GUI).
"""

import threading
from datetime import datetime, timedelta, timezone

from modules.survey_store import SurveyStore

T0 = datetime(2026, 3, 1, 12, 0, 0, tzinfo=timezone.utc)
# A spot and one ~250 m away (outside the 100 m cluster threshold).
SPOT_A = (37.4000, -122.1000)
SPOT_B = (37.4030, -122.1000)  # ~333 m north of A


def _store(tmp_path):
    return SurveyStore(str(tmp_path / "survey.db"))


def test_schema_and_enqueue(tmp_path):
    s = _store(tmp_path)
    tid = s.enqueue_tasking("wifi-fp:abc", designator="CLI-HOME_5G-1",
                            reason="novelty", now=T0)
    assert tid
    tasks = s.open_taskings()
    assert len(tasks) == 1
    assert tasks[0]["identity_key"] == "wifi-fp:abc"
    assert tasks[0]["designator"] == "CLI-HOME_5G-1"


def test_enqueue_dedupes_on_open_identity(tmp_path):
    """Re-flagging the same contact returns the same task, not a second one."""
    s = _store(tmp_path)
    a = s.enqueue_tasking("wifi-fp:abc", now=T0)
    b = s.enqueue_tasking("wifi-fp:abc", now=T0 + timedelta(minutes=5))
    assert a == b
    assert len(s.open_taskings()) == 1


def test_completed_task_does_not_block_new_tasking(tmp_path):
    """Once a survey is complete, tasking the same identity again is a fresh task."""
    s = _store(tmp_path)
    a = s.enqueue_tasking("wifi-fp:abc", now=T0)
    s.set_status(a, "complete")
    b = s.enqueue_tasking("wifi-fp:abc", now=T0 + timedelta(hours=1))
    assert a != b
    assert len(s.open_taskings()) == 1  # only the new one is open


def test_open_identity_keys_lookup(tmp_path):
    s = _store(tmp_path)
    t1 = s.enqueue_tasking("wifi-fp:abc", now=T0)
    s.enqueue_tasking("ble-fp:def", now=T0)
    keys = s.open_identity_keys()
    assert keys["wifi-fp:abc"] == t1
    assert "ble-fp:def" in keys


def test_dwell_is_gap_tolerant(tmp_path):
    """A blind gap between two sighting runs must NOT be counted as dwell, and it
    should split the stay into two visits."""
    s = _store(tmp_path)
    tid = s.enqueue_tasking("wifi-fp:abc", now=T0)
    # Run 1: five sightings 60 s apart at SPOT_A (4 min of dwell).
    for i in range(5):
        s.record_survey_observation(
            tid, timestamp=T0 + timedelta(seconds=60 * i),
            lat=SPOT_A[0], lon=SPOT_A[1], rssi=-50)
    # 30-minute blind gap (> visit_gap 600 s), then run 2: three more sightings.
    base2 = T0 + timedelta(minutes=34)
    for i in range(3):
        s.record_survey_observation(
            tid, timestamp=base2 + timedelta(seconds=60 * i),
            lat=SPOT_A[0], lon=SPOT_A[1], rssi=-48)

    result = s.compute_findings(tid, tz=timezone.utc)
    clusters = result["clusters"]
    assert len(clusters) == 1
    f = clusters[0]
    # Dwell = 4 min (run 1) + 2 min (run 2) = 360 s; the 30-min gap is excluded.
    assert abs(f["dwell_seconds"] - 360.0) < 1e-6
    assert f["visit_count"] == 2
    assert f["obs_count"] == 8
    # Device seen but no local home AP found -> a WiGLE candidate.
    assert result["outcome"] == "seen"
    assert result["wigle_candidate"] is True
    assert result["home_ap"] is None


def test_two_locations_ranked_by_dwell_and_return(tmp_path):
    """The longer-dwell / more-recurring spot ranks first (the bed-down headline)."""
    s = _store(tmp_path)
    tid = s.enqueue_tasking("wifi-fp:abc", now=T0)
    # SPOT_B: a brief single pass (2 sightings).
    for i in range(2):
        s.record_survey_observation(
            tid, timestamp=T0 + timedelta(seconds=30 * i),
            lat=SPOT_B[0], lon=SPOT_B[1], rssi=-70)
    # SPOT_A: a long stay much later (10 min continuous).
    base = T0 + timedelta(hours=2)
    for i in range(11):
        s.record_survey_observation(
            tid, timestamp=base + timedelta(seconds=60 * i),
            lat=SPOT_A[0], lon=SPOT_A[1], rssi=-45)

    clusters = s.compute_findings(tid, tz=timezone.utc)["clusters"]
    assert len(clusters) == 2
    assert clusters[0]["rank"] == 0
    # Rank 0 is SPOT_A (the long stay).
    assert abs(clusters[0]["cluster_lat"] - SPOT_A[0]) < 1e-4
    assert clusters[0]["dwell_seconds"] > clusters[1]["dwell_seconds"]


def test_overnight_and_distinct_nights(tmp_path):
    """Sightings across the night window on two calendar nights flag overnight and
    count two distinct nights (a pre-dawn hit belongs to the prior evening)."""
    s = _store(tmp_path)
    tid = s.enqueue_tasking("wifi-fp:abc", now=T0)
    # Night 1: 23:30 UTC on Mar 1 and 02:00 UTC on Mar 2 (same overnight stay).
    n1_eve = datetime(2026, 3, 1, 23, 30, tzinfo=timezone.utc)
    n1_pre = datetime(2026, 3, 2, 2, 0, tzinfo=timezone.utc)
    # Night 2: 23:00 UTC on Mar 3.
    n2_eve = datetime(2026, 3, 3, 23, 0, tzinfo=timezone.utc)
    for t in (n1_eve, n1_pre, n2_eve):
        s.record_survey_observation(tid, timestamp=t,
                                    lat=SPOT_A[0], lon=SPOT_A[1], rssi=-50)

    f = s.compute_findings(tid, tz=timezone.utc, night_hours="22-06")["clusters"][0]
    assert f["is_overnight"] is True
    assert f["distinct_nights"] == 2


def test_max_rssi_skips_zero_placeholder(tmp_path):
    """Zero RSSI is a placeholder, not a measurement; it must not win 'strongest'."""
    s = _store(tmp_path)
    tid = s.enqueue_tasking("wifi-fp:abc", now=T0)
    for rssi in (-60, 0, -48, None):
        s.record_survey_observation(tid, timestamp=T0, lat=SPOT_A[0],
                                    lon=SPOT_A[1], rssi=rssi)
    f = s.compute_findings(tid, tz=timezone.utc)["clusters"][0]
    assert f["max_rssi"] == -48


def test_home_ap_association_is_the_bed_down(tmp_path):
    """A local AP beaconing the device's home network locates the bed-down in one
    patrol — no dwell accumulation needed — and is NOT a WiGLE candidate."""
    s = _store(tmp_path)
    tid = s.enqueue_tasking("wifi-fp:abc", now=T0)
    for i in range(4):
        s.record_survey_observation(
            tid, timestamp=T0 + timedelta(seconds=30 * i),
            lat=SPOT_A[0], lon=SPOT_A[1], rssi=-55,
            kind="ap", bssid="00:11:22:33:44:55", ssid="HOME_NET_5G")
    result = s.compute_findings(tid, tz=timezone.utc)
    assert result["outcome"] == "resident"
    assert result["wigle_candidate"] is False
    ap = result["home_ap"]
    assert ap["bssid"] == "00:11:22:33:44:55"
    assert ap["ssid"] == "HOME_NET_5G"
    assert abs(ap["lat"] - SPOT_A[0]) < 1e-4
    # And it round-trips through the store read.
    assert s.findings_for(tid)["home_ap"]["bssid"] == "00:11:22:33:44:55"


def test_not_located_outcome(tmp_path):
    """A task with zero observations resolves to not_located / WiGLE candidate."""
    s = _store(tmp_path)
    tid = s.enqueue_tasking("wifi-fp:abc", now=T0)
    result = s.compute_findings(tid, tz=timezone.utc)
    assert result["located"] is False
    assert result["outcome"] == "not_located"
    assert result["wigle_candidate"] is True
    assert result["home_ap"] is None


def test_ingest_result_marks_complete_and_stores_home_ap(tmp_path):
    """Fixed-side ingest stores a pushed result (home AP + clusters) and completes it."""
    s = _store(tmp_path)
    tid = s.enqueue_tasking("wifi-fp:abc", now=T0)
    pushed = {
        "located": True, "outcome": "resident", "wigle_candidate": False,
        "home_ap": {"bssid": "aa:bb:cc:dd:ee:ff", "ssid": "HOME_NET_5G",
                    "lat": SPOT_A[0], "lon": SPOT_A[1], "max_rssi": -50, "obs_count": 6},
        "clusters": [{"rank": 0, "cluster_lat": SPOT_A[0], "cluster_lon": SPOT_A[1],
                      "dwell_seconds": 300, "visit_count": 1, "distinct_days": 1,
                      "distinct_nights": 0, "max_rssi": -48, "is_overnight": False,
                      "obs_count": 5}],
    }
    s.ingest_result(tid, pushed, survey_node="mobile-1")
    got = s.findings_for(tid)
    assert got["home_ap"]["ssid"] == "HOME_NET_5G"
    assert len(got["clusters"]) == 1
    t = s.get_tasking(tid)
    assert t["status"] == "complete"
    tf = [x for x in s.taskings_with_findings() if x["task_id"] == tid][0]
    assert tf["outcome"] == "resident"
    assert tf["wigle_candidate"] is False


def test_upsert_tasking_preserves_local_completed_status(tmp_path):
    """A pulled tasking must not resurrect a survey the local node already completed."""
    s = _store(tmp_path)
    tid = s.enqueue_tasking("wifi-fp:abc", now=T0)
    s.ingest_result(tid, {"located": False, "outcome": "not_located",
                          "wigle_candidate": True, "home_ap": None, "clusters": []},
                    complete=True)
    s.upsert_tasking({"task_id": tid, "identity_key": "wifi-fp:abc",
                      "status": "open", "reason": "operator"})
    assert s.get_tasking(tid)["status"] == "complete"


def test_prune_observations(tmp_path):
    s = SurveyStore(str(tmp_path / "survey.db"), retention_days=7)
    tid = s.enqueue_tasking("wifi-fp:abc", now=T0)
    old = T0 - timedelta(days=30)
    s.record_survey_observation(tid, timestamp=old, lat=SPOT_A[0], lon=SPOT_A[1])
    s.record_survey_observation(tid, timestamp=T0, lat=SPOT_A[0], lon=SPOT_A[1])
    removed = s.prune_observations(now=T0)
    assert removed == 1
    assert s.observation_count(tid) == 1


def test_cross_thread_read_is_safe(tmp_path):
    """The GUI (Flask thread) reads taskings off the asyncio-thread connection."""
    s = _store(tmp_path)
    s.enqueue_tasking("wifi-fp:abc", now=T0)
    results = {}

    def reader():
        try:
            results["tasks"] = s.open_taskings()
        except Exception as exc:
            results["error"] = exc

    t = threading.Thread(target=reader)
    t.start()
    t.join()
    assert "error" not in results, results.get("error")
    assert len(results["tasks"]) == 1


def _iso(dt):
    return dt.astimezone(timezone.utc).isoformat()
