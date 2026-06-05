"""Tests for FixedScoring novelty detection (Phase 1)."""

from datetime import datetime, timedelta, timezone

from modules.baseline_store import BaselineStore
from modules.fixed_scoring import FixedScoring
from modules.persistence import DetectionEvent

T0 = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)


def _clocked_engine(baseline_hours=1.0, start=T0):
    """Return (engine, clock_holder) with a controllable in-memory store."""
    holder = [start]
    store = BaselineStore(":memory:", baseline_hours=baseline_hours, now=start)
    engine = FixedScoring(store=store, clock=lambda: holder[0])
    return engine, holder


def _static_device(mac="d8:96:85:11:22:33", **extra):
    d = {"macaddr": mac, "manuf": "Acme", "type": "Wi-Fi AP"}
    d.update(extra)
    return d


def _random_device(mac, probe="HomeNet", **extra):
    d = {"macaddr": mac, "name": "", "probe_ssids": [probe]}
    d.update(extra)
    return d


# ---------------------------------------------------------------------------
# Learning window suppresses all flags
# ---------------------------------------------------------------------------


def test_no_flags_during_learning():
    engine, clock = _clocked_engine(baseline_hours=1.0)
    # Two observations during the learning window — still must not flag.
    assert engine.update([_static_device()]) == []
    assert engine.update([_static_device()]) == []


# ---------------------------------------------------------------------------
# In-baseline device is never novel
# ---------------------------------------------------------------------------


def test_in_baseline_device_no_alert_after_freeze():
    engine, clock = _clocked_engine(baseline_hours=1.0)
    dev = _static_device()
    engine.update([dev])               # seen during learning -> baseline
    clock[0] = T0 + timedelta(hours=2)  # baseline now frozen
    # Same device reappears repeatedly — it is part of the baseline, not novel.
    assert engine.update([dev]) == []
    assert engine.update([dev]) == []


# ---------------------------------------------------------------------------
# Novel-persistent device IS flagged
# ---------------------------------------------------------------------------


def test_novel_persistent_device_flagged():
    engine, clock = _clocked_engine(baseline_hours=1.0)
    engine.update([_static_device()])   # establish some baseline
    clock[0] = T0 + timedelta(hours=2)  # frozen

    newcomer = _static_device(mac="d8:96:85:99:99:99", manuf="Spy Inc", type="phone")
    # First post-freeze sighting: novel but not yet persistent (1 observation).
    assert engine.update([newcomer]) == []
    # Second sighting: now persists -> flagged.
    events = engine.update([newcomer])
    assert len(events) == 1
    ev = events[0]
    assert isinstance(ev, DetectionEvent)
    assert ev.mac == "d8:96:85:99:99:99"
    assert ev.alert_level == "high"
    assert ev.score == 1.0
    assert ev.score_breakdown == {"novelty": 1.0}
    assert ev.locations == []  # no location gate in fixed mode (the #50 bug)
    assert ev.observation_count == 2
    assert ev.manufacturer == "Spy Inc"


def test_single_post_freeze_sighting_not_flagged():
    engine, clock = _clocked_engine(baseline_hours=1.0)
    clock[0] = T0 + timedelta(hours=2)
    # A device seen exactly once after freeze does not "persist".
    assert engine.update([_static_device(mac="d8:96:85:ab:cd:ef")]) == []


# ---------------------------------------------------------------------------
# Keying: fingerprint for randomized MACs, MAC for stable
# ---------------------------------------------------------------------------


def test_randomized_macs_share_fingerprint_key():
    engine, clock = _clocked_engine(baseline_hours=1.0)
    # Two DIFFERENT randomized MACs sharing a probe SSID -> one logical device.
    engine.update([_random_device("a2:11:11:11:11:11", probe="HomeNet")])  # baseline
    clock[0] = T0 + timedelta(hours=2)  # frozen
    # A rotated MAC with the same probe fingerprint is the SAME device -> not novel.
    assert engine.update([_random_device("a6:22:22:22:22:22", probe="HomeNet")]) == []
    assert engine.update([_random_device("ae:33:33:33:33:33", probe="HomeNet")]) == []


def test_novel_fingerprint_flagged():
    engine, clock = _clocked_engine(baseline_hours=1.0)
    engine.update([_random_device("a2:11:11:11:11:11", probe="HomeNet")])  # baseline
    clock[0] = T0 + timedelta(hours=2)  # frozen
    # A different probe fingerprint never seen in baseline -> novel; persist it.
    new_fp = "a2:44:44:44:44:44"
    assert engine.update([_random_device(new_fp, probe="EvilProbe")]) == []
    events = engine.update([_random_device("a6:55:55:55:55:55", probe="EvilProbe")])
    assert len(events) == 1
    assert events[0].mac_type == "randomized"


def test_stable_mac_keyed_by_mac():
    # Distinct static MACs are distinct devices even with identical metadata.
    engine, clock = _clocked_engine(baseline_hours=1.0)
    engine.update([_static_device(mac="00:11:22:33:44:55")])  # baseline
    clock[0] = T0 + timedelta(hours=2)
    other = _static_device(mac="00:11:22:33:44:66")  # different static MAC -> novel
    assert engine.update([other]) == []
    events = engine.update([other])
    assert len(events) == 1
    assert events[0].mac == "00:11:22:33:44:66"
    assert events[0].mac_type == "static"


# ---------------------------------------------------------------------------
# Interface robustness
# ---------------------------------------------------------------------------


def test_update_accepts_none_gps_fix():
    engine, clock = _clocked_engine(baseline_hours=1.0)
    clock[0] = T0 + timedelta(hours=2)
    dev = _static_device(mac="d8:96:85:77:88:99")
    engine.update([dev], gps_fix=None)
    events = engine.update([dev], gps_fix=None)
    assert len(events) == 1
    assert events[0].locations == []


def test_update_ignores_blank_macaddr():
    engine, clock = _clocked_engine(baseline_hours=1.0)
    clock[0] = T0 + timedelta(hours=2)
    assert engine.update([{"macaddr": ""}]) == []


def test_status_reports_learning_then_frozen():
    engine, clock = _clocked_engine(baseline_hours=1.0)
    assert engine.status()["learning"] is True
    clock[0] = T0 + timedelta(hours=2)
    assert engine.status()["learning"] is False
