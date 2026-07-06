"""Tests for the recon-pair survey coordinator (design §5.5 / §10).

These exercise the matcher, fixed-node evidence/auto-task, and the patrol-aware
finding preparation directly against a :class:`SurveyCoordinator` built on an
in-memory store — no full orchestrator needed. The orchestrator's poll-path seams
into this coordinator are covered by the wiring tests in ``test_orchestrator.py``.
"""
import asyncio
from datetime import datetime, timezone

from modules.survey_coordinator import SurveyCoordinator
from modules.survey_store import SurveyStore
from modules.wifi_fingerprint import anchored_identity_key

FIX = {"lat": 37.8, "lon": -122.42}


def _make_event(**overrides):
    """A minimal DetectionEvent for the evidence-building tests."""
    from modules.persistence import DetectionEvent
    defaults = dict(
        mac="aa:bb:cc:dd:ee:ff", score=0.85,
        score_breakdown={"novelty": 1.0},
        first_seen=datetime(2026, 1, 1, tzinfo=timezone.utc),
        last_seen=datetime(2026, 1, 1, tzinfo=timezone.utc),
        locations=[], observation_count=5, manufacturer="Acme",
        device_type="phone", alert_level="likely",
    )
    defaults.update(overrides)
    return DetectionEvent(**defaults)


def _coord(node_mode="mobile", store=None):
    """A survey coordinator on an in-memory store (or the given one)."""
    if store is None:
        store = SurveyStore(":memory:")
    return SurveyCoordinator(store, node_mode, asyncio.Event())


def _coord_with_task(min_polls=3):
    """A mobile coordinator with an in-memory store and one open WiFi tasking."""
    co = _coord()
    co._survey_min_patrol_polls = min_polls
    key = anchored_identity_key(1, "RARE_SSID")
    tid = co.survey_store.enqueue_tasking(
        key, evidence={"anchor": "RARE_SSID", "identity_key": key})
    return co, tid


# ---------------------------------------------------------------------------
# Mobile matcher
# ---------------------------------------------------------------------------

def test_ap_association_locates_bed_down():
    """A local AP beaconing the tasked device's distinctive home network resolves the
    bed-down in a single patrol — even if the device itself is never seen."""
    co = _coord()
    key = anchored_identity_key(777, "CASA_DEL_MAR")
    tid = co.survey_store.enqueue_tasking(
        key, evidence={"anchor": "CASA_DEL_MAR", "identity_key": key})

    ap = {"macaddr": "de:ad:be:ef:00:01", "type": "Wi-Fi AP",
          "beaconed_ssid": "CASA_DEL_MAR", "last_signal": -58}
    for _ in range(3):
        co.record_hits([ap, {"macaddr": "x", "beaconed_ssid": "xfinitywifi"}], FIX)

    res = co.survey_store.compute_findings(tid)
    assert res["outcome"] == "resident"
    assert res["wigle_candidate"] is False
    assert res["home_ap"]["bssid"] == "de:ad:be:ef:00:01"
    assert res["home_ap"]["ssid"] == "CASA_DEL_MAR"


def test_ap_association_falls_back_to_label():
    """A tasking with no explicit anchor but a populated label still resolves the
    bed-down — recovers tasks dispatched before the anchor was wired through."""
    co = _coord()
    key = anchored_identity_key(777, "CASA_DEL_MAR")
    tid = co.survey_store.enqueue_tasking(
        key, evidence={"anchor": None, "label": "CASA_DEL_MAR",
                       "modality": "wifi", "identity_key": key})

    ap = {"macaddr": "de:ad:be:ef:00:01", "type": "Wi-Fi AP",
          "beaconed_ssid": "CASA_DEL_MAR", "last_signal": -58}
    for _ in range(3):
        co.record_hits([ap], FIX)

    res = co.survey_store.compute_findings(tid)
    assert res["outcome"] == "resident"
    assert res["home_ap"]["ssid"] == "CASA_DEL_MAR"


def test_not_located_after_patrol():
    """A tasking patrolled past the threshold with no match reports not_located."""
    co, tid = _coord_with_task(min_polls=3)
    for _ in range(3):
        co.record_hits([{"macaddr": "z", "beaconed_ssid": "attwifi"}], FIX)
    prepared = co._prepare_findings()
    assert prepared and prepared[0][0] == tid
    assert prepared[0][1]["outcome"] == "not_located"
    assert prepared[0][1]["wigle_candidate"] is True


def test_matcher_noop_on_fixed_node():
    """The matcher only records on a mobile node."""
    co = _coord(node_mode="fixed")
    tid = co.survey_store.enqueue_tasking("wifi-fp:abc", evidence={"anchor": "N"})
    co.record_hits([{"macaddr": "x", "beaconed_ssid": "N"}], FIX)
    assert co.survey_store.observation_count(tid) == 0


# ---------------------------------------------------------------------------
# Fixed-node evidence + auto-task
# ---------------------------------------------------------------------------

def test_evidence_anchor_from_label_when_no_live_fp_anchor():
    """Regression: a surveyable WiFi contact whose live device lacks ``fp_anchor``
    (the usual case — it only appears on a poll where the device freshly probes its
    SSID) must still carry the anchor, sourced from the rotation-stable fingerprint
    label. Without this the AP-association bed-down can never match (all #195 field
    tasks shipped with anchor=None)."""
    co = _coord()
    key = anchored_identity_key(777, "Battoman2021_nomap")
    event = _make_event(fingerprint=key, fingerprint_label="Battoman2021_nomap")
    device = {"macaddr": "aa:bb:cc:dd:ee:ff", "probe_fingerprint": 777}  # no fp_anchor
    ev = co.note_flagged_contact(event, device, "Battoman2021_nomap", key)
    assert ev is not None
    assert ev["modality"] == "wifi"
    assert ev["anchor"] == "Battoman2021_nomap"
    assert ev["label"] == "Battoman2021_nomap"


def test_evidence_ble_anchor_stays_none():
    """BLE has no beaconed home network, so its anchor must stay None even though it
    has a label — otherwise the matcher would hunt an AP beaconing a vendor string."""
    co = _coord()
    event = _make_event(fingerprint="ble-fp:8c5300c89623",
                        fingerprint_label="BLE-vendor_0x02b2-14",
                        device_type="BTLE")
    device = {"macaddr": "8c:53:00:c8:96:23", "type": "BTLE"}
    ev = co.note_flagged_contact(event, device, "BLE-vendor_0x02b2-14",
                                 "ble-fp:8c5300c89623")
    assert ev is not None
    assert ev["modality"] == "ble"
    assert ev["anchor"] is None


def test_evidence_none_for_mac_only_contact():
    """A bare mac: contact has no portable key and cannot be surveyed — no evidence."""
    co = _coord()
    event = _make_event(fingerprint="mac:aa:bb:cc:dd:ee:ff")
    ev = co.note_flagged_contact(event, {"macaddr": "aa:bb:cc:dd:ee:ff"},
                                 "mac:aa:bb:cc:dd:ee:ff", "mac:aa:bb:cc:dd:ee:ff")
    assert ev is None


# ---------------------------------------------------------------------------
# Patrol-aware finding preparation (design §10)
# ---------------------------------------------------------------------------

def test_active_patrol_defers_all_task_closure():
    """During a patrol the poll quota is suspended: a task patrolled well past the
    threshold with no match must stay open, not close as not_located (the walk-2 miss)."""
    co, tid = _coord_with_task(min_polls=2)
    co.survey_store.start_patrol()
    for _ in range(6):  # far past the quota, still nothing matched
        co.record_hits([{"macaddr": "z", "beaconed_ssid": "attwifi"}], FIX)
    assert co._prepare_findings() == []
    assert co.survey_store.get_tasking(tid)["status"] in ("open", "surveying")


def test_end_patrol_finalizes_open_tasks():
    """Ending a patrol closes out every still-open task as a unit, and finalizes once."""
    co, tid = _coord_with_task(min_polls=99)  # quota unreachable
    co.survey_store.start_patrol()
    for _ in range(3):
        co.record_hits([{"macaddr": "z", "beaconed_ssid": "attwifi"}], FIX)
    assert co._prepare_findings() == []  # active: nothing closes

    co.survey_store.end_patrol()
    prepared = co._prepare_findings()
    assert prepared and prepared[0][0] == tid
    assert prepared[0][1]["outcome"] == "not_located"
    assert co.survey_store.patrol_pending_finalize() is None  # finalized once


def test_patrol_backstop_auto_ends_runaway_patrol():
    """A patrol left running past the backstop auto-ends and finalizes, so a forgotten
    'end patrol' can't hold tasks open forever."""
    co, tid = _coord_with_task(min_polls=99)
    co._patrol_max_seconds = 0.0  # any elapsed time trips the backstop
    co.survey_store.start_patrol()
    for _ in range(2):
        co.record_hits([{"macaddr": "z", "beaconed_ssid": "attwifi"}], FIX)
    prepared = co._prepare_findings()
    assert prepared and prepared[0][0] == tid
    assert co.survey_store.active_patrol() is None


def test_no_patrol_keeps_legacy_quota_closure():
    """With no patrol started, the legacy poll-quota closure still applies."""
    co, tid = _coord_with_task(min_polls=3)
    for _ in range(3):
        co.record_hits([{"macaddr": "z", "beaconed_ssid": "attwifi"}], FIX)
    prepared = co._prepare_findings()
    assert prepared and prepared[0][0] == tid
    assert prepared[0][1]["outcome"] == "not_located"


# ---------------------------------------------------------------------------
# sync_configured gate
# ---------------------------------------------------------------------------

def test_sync_not_configured_without_store_or_url(monkeypatch):
    """No fixed-node URL -> not a syncing node; None store -> inert."""
    monkeypatch.delenv("SURVEY_FIXED_URL", raising=False)
    assert _coord().sync_configured is False
    assert SurveyCoordinator(None, "mobile", asyncio.Event()).sync_configured is False


# ---------------------------------------------------------------------------
# Wardrive index (design §11)
# ---------------------------------------------------------------------------

def test_wardrive_banks_aps_only_during_a_patrol():
    """APs are banked while a patrol runs, and not otherwise (nothing is collected
    outside a patrol)."""
    co = _coord()
    ap = {"macaddr": "aa:bb", "beaconed_ssid": "NetA", "last_signal": -60}
    co.record_hits([ap], FIX)                       # no patrol -> nothing banked
    assert co.survey_store.wardrive_count() == 0
    co.survey_store.start_patrol()
    co.record_hits([ap], FIX)                       # patrol active -> banked
    assert co.survey_store.wardrive_count() == 1


def test_wardrive_needs_a_gps_fix():
    co = _coord(); co.survey_store.start_patrol()
    co.record_hits([{"macaddr": "aa", "beaconed_ssid": "N", "last_signal": -60}], None)
    assert co.survey_store.wardrive_count() == 0


def test_wardrive_banks_aps_not_clients():
    """APs only (v1): a record with no beaconed SSID is a client, not banked."""
    co = _coord(); co.survey_store.start_patrol()
    co.record_hits([{"macaddr": "cc", "last_signal": -60}], FIX)
    assert co.survey_store.wardrive_count() == 0


def test_wardrive_resolves_bed_down_for_a_device_tasked_after_the_walk():
    """The headline §11 payoff: walk past an AP with an EMPTY watchlist, task the device
    whose home network it beacons only afterwards, and the bed-down still resolves from
    the banked index — timing no longer decides whether a residence is found."""
    co = _coord()
    co.survey_store.start_patrol()
    ap = {"macaddr": "de:ad:be:ef", "beaconed_ssid": "CASA_DEL_MAR", "last_signal": -55}
    co.record_hits([ap], FIX)                        # banked with nothing tasked
    assert co.survey_store.wardrive_count() == 1
    key = anchored_identity_key(777, "CASA_DEL_MAR")  # tasked AFTER the encounter
    tid = co.survey_store.enqueue_tasking(
        key, evidence={"anchor": "CASA_DEL_MAR", "identity_key": key})
    co.survey_store.end_patrol()
    prepared = co._prepare_findings()
    assert prepared and prepared[0][0] == tid
    res = prepared[0][1]
    assert res["outcome"] == "resident"
    assert res["home_ap"]["ssid"] == "CASA_DEL_MAR"
    assert res["home_ap"]["bssid"] == "de:ad:be:ef"


def test_wardrive_retroactive_resolution_dedups_per_ap():
    """Folding a banked AP into a task's observations happens once per (task, BSSID),
    so repeated cycles don't inflate the finding."""
    co = _coord(); co.survey_store.start_patrol()
    co.record_hits([{"macaddr": "de:ad", "beaconed_ssid": "CASA", "last_signal": -55}], FIX)
    key = anchored_identity_key(1, "CASA")
    tid = co.survey_store.enqueue_tasking(
        key, evidence={"anchor": "CASA", "identity_key": key})
    co._resolve_from_wardrive(co.survey_store.open_taskings())
    co._resolve_from_wardrive(co.survey_store.open_taskings())
    assert co.survey_store.observation_count(tid) == 1
