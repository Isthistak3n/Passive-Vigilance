"""Tests for FixedScoring — novelty (Phase 1) + off-schedule / severity (Phase 2).

Fixture timing note: the learning window is 1h starting at T0 (hour 0), so every
baseline sighting lands in hour 0 and the baseline hour-mask is {0}. "Known
device stays silent" tests therefore advance the clock by whole days (+24h) to
stay in a baselined hour; off-schedule tests advance to a *different* hour.
"""

import os
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from modules.baseline_store import BaselineStore
from modules.fixed_scoring import FixedScoring, _coerce_signal
from modules.persistence import DetectionEvent

T0 = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)


def _clocked_engine(baseline_hours=1.0, start=T0):
    """Return (engine, clock_holder) with a controllable in-memory store."""
    holder = [start]
    store = BaselineStore(":memory:", baseline_hours=baseline_hours, now=start)
    engine = FixedScoring(store=store, clock=lambda: holder[0])
    return engine, holder


def _seed_distinct_hours(engine, clock, dev, n_hours):
    """Observe `dev` once in each of hours 0..n_hours-1 during learning, so its
    baseline hour-mask ends up with n_hours distinct bits set."""
    for h in range(n_hours):
        clock[0] = T0 + timedelta(hours=h)
        engine.update([dev])


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
    engine.update([dev])                # seen during learning (hour 0) -> baseline
    clock[0] = T0 + timedelta(hours=24)  # frozen, and back at a baselined hour (0)
    # Same device, same hour-of-day — part of the baseline, not novel, on-schedule.
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
    # Phase 2: novelty alone is now a LOW (suspicious) flag, not hardcoded high.
    assert ev.alert_level == "suspicious"
    assert ev.score == 0.5
    assert ev.score_breakdown == {
        "novelty": 1.0, "off_schedule": 0.0, "abnormal_dwell": 0.0, "approaching": 0.0,
    }
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
    clock[0] = T0 + timedelta(hours=24)  # frozen, back at a baselined hour (0)
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


# ===========================================================================
# Phase 2 — off-schedule, graduated severity, baseline signal stats
# ===========================================================================


def test_coerce_signal():
    assert _coerce_signal(None) is None
    assert _coerce_signal(-55) == -55.0
    assert _coerce_signal("-55") == -55.0
    assert _coerce_signal("nope") is None


def test_novelty_alone_flags_suspicious():
    """No-regression invariant: a Phase-1 novel-persistent device STILL emits an
    event — now at the LOW (suspicious) level, not silent and not high."""
    engine, clock = _clocked_engine(baseline_hours=1.0)
    clock[0] = T0 + timedelta(hours=2)   # frozen immediately; nothing baselined
    dev = _static_device(mac="d8:96:85:ca:fe:01")
    assert engine.update([dev]) == []    # 1 sighting — not persistent yet
    events = engine.update([dev])        # 2 sightings — novel-persistent
    assert len(events) == 1
    assert events[0].alert_level == "suspicious"
    assert events[0].alert_level != "high"
    assert events[0].score == 0.5


def test_off_schedule_new_hour_flags():
    # Rich baseline (>= default 12 distinct hours) seen in a never-baselined hour.
    engine, clock = _clocked_engine(baseline_hours=24.0)
    dev = _static_device(mac="d8:96:85:aa:bb:cc")
    _seed_distinct_hours(engine, clock, dev, 12)   # baseline spans hours 0..11
    clock[0] = T0 + timedelta(hours=37)            # frozen; hour 13 never baselined
    events = engine.update([dev])
    assert len(events) == 1
    ev = events[0]
    assert ev.alert_level == "suspicious"
    assert ev.score == 0.5
    assert ev.score_breakdown["off_schedule"] == 1.0
    assert ev.score_breakdown["novelty"] == 0.0


def test_off_schedule_same_hour_no_flag():
    # Rich baseline (guard active), device re-seen in a baselined hour -> silent.
    engine, clock = _clocked_engine(baseline_hours=24.0)
    dev = _static_device(mac="d8:96:85:aa:bb:cc")
    _seed_distinct_hours(engine, clock, dev, 12)   # baseline hours 0..11
    clock[0] = T0 + timedelta(hours=24 + 5)        # frozen, hour 5 (baselined)
    assert engine.update([dev]) == []


# ---------------------------------------------------------------------------
# Off-schedule activation guard (>= N distinct baseline hours)
# ---------------------------------------------------------------------------


def test_guard_suppresses_off_schedule_for_thin_baseline():
    # Default guard is 12. A baseline of only a few distinct hours must NOT flag
    # off-schedule even in a never-baselined hour — "insufficient baseline".
    engine, clock = _clocked_engine(baseline_hours=24.0)
    dev = _static_device(mac="d8:96:85:11:00:11")
    _seed_distinct_hours(engine, clock, dev, 5)    # only 5 distinct hours < 12
    clock[0] = T0 + timedelta(hours=37)            # frozen, hour 13 (unbaselined)
    assert engine.update([dev]) == []              # guard suppresses off-schedule


def test_guard_boundary_11_suppresses_12_activates():
    # Exactly at the default threshold: 11 distinct hours -> suppressed,
    # 12 distinct hours -> activates.
    eng11, c11 = _clocked_engine(baseline_hours=24.0)
    d11 = _static_device(mac="d8:96:85:00:00:11")
    _seed_distinct_hours(eng11, c11, d11, 11)
    c11[0] = T0 + timedelta(hours=37)              # hour 13, unbaselined
    assert eng11.update([d11]) == []               # 11 < 12 -> no flag

    eng12, c12 = _clocked_engine(baseline_hours=24.0)
    d12 = _static_device(mac="d8:96:85:00:00:12")
    _seed_distinct_hours(eng12, c12, d12, 12)
    c12[0] = T0 + timedelta(hours=37)              # hour 13, unbaselined
    events = eng12.update([d12])                   # 12 >= 12 -> flags
    assert len(events) == 1
    assert events[0].score_breakdown["off_schedule"] == 1.0


def test_guard_threshold_env_overridable():
    # OFF_SCHEDULE_MIN_BASELINE_HOURS=2 -> activation at 2 distinct hours.
    holder = [T0]
    store = BaselineStore(":memory:", baseline_hours=24.0, now=T0)
    with patch.dict(os.environ, {"OFF_SCHEDULE_MIN_BASELINE_HOURS": "2"}):
        engine = FixedScoring(store=store, clock=lambda: holder[0])
    dev = _static_device(mac="d8:96:85:02:00:02")
    _seed_distinct_hours(engine, holder, dev, 2)   # 2 distinct hours
    holder[0] = T0 + timedelta(hours=37)           # frozen, hour 13 (unbaselined)
    events = engine.update([dev])
    assert len(events) == 1
    assert events[0].score_breakdown["off_schedule"] == 1.0


def test_novelty_unaffected_by_guard():
    # No-regression: a novel device flags on novelty regardless of the guard
    # (the guard only suppresses the off-schedule signal for known devices).
    engine, clock = _clocked_engine(baseline_hours=1.0)
    clock[0] = T0 + timedelta(hours=2)             # frozen immediately
    dev = _static_device(mac="d8:96:85:ca:fe:02")
    assert engine.update([dev]) == []              # 1 sighting
    events = engine.update([dev])                  # novel-persistent
    assert len(events) == 1
    assert events[0].alert_level == "suspicious"
    assert events[0].score_breakdown["novelty"] == 1.0


def test_off_schedule_not_applied_to_novel():
    # A novel device has no baseline schedule — off-schedule must not apply even
    # when it appears in an hour never seen during baseline.
    engine, clock = _clocked_engine(baseline_hours=1.0)
    engine.update([_static_device(mac="00:11:22:33:44:55")])  # some baseline
    clock[0] = T0 + timedelta(hours=2)     # frozen, hour 2
    novel = _static_device(mac="d8:96:85:de:ad:00")
    engine.update([novel])                 # 1st post-freeze (novel)
    events = engine.update([novel])        # persists -> flags
    assert len(events) == 1
    bd = events[0].score_breakdown
    assert bd["novelty"] == 1.0
    assert bd["off_schedule"] == 0.0


def test_combine_severity_mapping():
    combine = FixedScoring._combine
    base = {"novelty": 0.0, "off_schedule": 0.0, "abnormal_dwell": 0.0, "approaching": 0.0}
    # one active signal -> suspicious
    assert combine({**base, "novelty": 1.0}) == (0.5, "suspicious")
    assert combine({**base, "off_schedule": 1.0}) == (0.5, "suspicious")
    # two active signals -> likely
    assert combine({**base, "novelty": 1.0, "off_schedule": 1.0}) == (0.7, "likely")
    # three -> high (reachable in Phase 2.5 when more signals activate)
    assert combine({**base, "novelty": 1.0, "off_schedule": 1.0, "abnormal_dwell": 1.0}) == (0.9, "high")
    # none -> no flag
    assert combine(base) == (0.0, None)


def test_baseline_signal_stats_populated_and_none_skipped():
    engine, clock = _clocked_engine(baseline_hours=1.0)

    def dev(sig):
        return {"macaddr": "d8:96:85:11:22:33", "manuf": "Acme", "type": "AP", "last_signal": sig}

    engine.update([dev(-50)])
    engine.update([dev(-60)])
    engine.update([dev(None)])                       # None -> skipped, not counted
    engine.update([{"macaddr": "d8:96:85:11:22:33"}])  # missing last_signal -> None
    p = engine._store.get_profile("mac:d8:96:85:11:22:33")
    assert p.signal_count == 2
    assert p.signal_mean == -55.0
    assert p.signal_var == 25.0          # population variance of [-50, -60]
    assert p.hour_mask == (1 << 0)        # all sightings in hour 0


def test_baseline_signal_stats_not_accumulated_after_freeze():
    engine, clock = _clocked_engine(baseline_hours=1.0)
    dev = lambda sig: {"macaddr": "d8:96:85:11:22:33", "last_signal": sig}
    engine.update([dev(-50)])                         # learning sample
    clock[0] = T0 + timedelta(hours=24)               # frozen (same hour)
    engine.update([dev(-90)])                         # post-freeze — must NOT count
    p = engine._store.get_profile("mac:d8:96:85:11:22:33")
    assert p.signal_count == 1
    assert p.signal_mean == -50.0


# ===========================================================================
# Phase 2.5 — zero-signal filter + approaching trigger
# ===========================================================================

_AMAC = "d8:96:85:ab:cd:ef"


def _seed_baseline_signal(engine, clock, n, dbm, mac=_AMAC, jitter=None, dtype="Wi-Fi Client"):
    """Fold n baseline RSSI samples (all in hour 0, during learning).

    Default device type is a mobile client (approaching-eligible). Pass
    dtype="Wi-Fi AP" to exercise the access-point exclusion.
    """
    for i in range(n):
        clock[0] = T0 + timedelta(minutes=i)
        s = dbm if jitter is None else dbm + jitter[i % len(jitter)]
        engine.update([{"macaddr": mac, "manuf": "Acme", "type": dtype, "last_signal": s}])


def _feed_recent(engine, clock, n, dbm, mac=_AMAC, hour=1):
    """Feed n post-freeze readings at a fixed (post-freeze) hour; return events."""
    events = []
    for i in range(n):
        clock[0] = T0 + timedelta(hours=24 + hour, minutes=i)
        events += engine.update([{"macaddr": mac, "last_signal": dbm}])
    return events


def test_coerce_signal_skips_zero():
    assert _coerce_signal(0) is None
    assert _coerce_signal(0.0) is None
    assert _coerce_signal(-55) == -55.0
    assert _coerce_signal(None) is None


def test_approaching_fires_when_recent_meaningfully_stronger():
    engine, clock = _clocked_engine(baseline_hours=1.0)
    _seed_baseline_signal(engine, clock, 12, -70, jitter=[-1, 0, 1])   # ~-70, small std
    events = _feed_recent(engine, clock, 6, -50)                       # +20 dB stronger
    assert events, "expected an approaching flag"
    ev = events[-1]
    assert ev.score_breakdown["approaching"] == 1.0
    assert ev.score_breakdown["novelty"] == 0.0
    assert ev.alert_level == "suspicious"
    assert ev.score == 0.5


def test_approaching_quiet_when_recent_not_stronger():
    engine, clock = _clocked_engine(baseline_hours=1.0)
    _seed_baseline_signal(engine, clock, 12, -70, jitter=[-1, 0, 1])
    assert _feed_recent(engine, clock, 6, -71) == []   # same/weaker -> no approach


def test_approaching_respects_db_floor_for_steady_baseline():
    # Steady baseline (std ~0): a small rise below the absolute dB floor (6) must
    # not trip approaching, even though it exceeds 2*std.
    engine, clock = _clocked_engine(baseline_hours=1.0)
    _seed_baseline_signal(engine, clock, 12, -70)      # zero variance
    assert _feed_recent(engine, clock, 6, -66) == []   # +4 dB < 6 dB floor -> quiet


def test_approaching_quiet_below_min_recent_samples():
    engine, clock = _clocked_engine(baseline_hours=1.0)
    _seed_baseline_signal(engine, clock, 12, -70, jitter=[-1, 0, 1])
    assert _feed_recent(engine, clock, 4, -50) == []   # only 4 recent (<5) -> quiet


def test_approaching_quiet_below_min_baseline_samples():
    engine, clock = _clocked_engine(baseline_hours=1.0)
    _seed_baseline_signal(engine, clock, 6, -70, jitter=[-1, 0, 1])   # 6 baseline (<10)
    assert _feed_recent(engine, clock, 6, -50) == []   # thin baseline -> quiet


def test_approaching_not_for_novel_device():
    # A novel device (no baseline) never gets approaching even with a strong,
    # rising recent signal; it still flags on novelty alone.
    engine, clock = _clocked_engine(baseline_hours=1.0)
    clock[0] = T0 + timedelta(hours=2)                 # frozen; device is novel
    dev = lambda s: {"macaddr": "d8:96:85:00:00:99", "last_signal": s}
    events = []
    for i in range(6):
        clock[0] = T0 + timedelta(hours=2, minutes=i)
        events += engine.update([dev(-40)])            # strong, but no baseline
    assert events
    bd = events[-1].score_breakdown
    assert bd["approaching"] == 0.0                    # known-device-only
    assert bd["novelty"] == 1.0


def test_off_schedule_plus_approaching_escalates_to_likely():
    # A known device that is BOTH off-schedule and approaching -> two signals -> likely.
    engine, clock = _clocked_engine(baseline_hours=12.0)
    dev = lambda s, : {"macaddr": _AMAC, "manuf": "Acme", "type": "Wi-Fi Client", "last_signal": s}
    # Baseline across 12 distinct hours (satisfies off-schedule guard) with a
    # weak, slightly-jittery signal.
    for h in range(12):
        clock[0] = T0 + timedelta(hours=h)
        engine.update([dev(-70 + (h % 3 - 1))])
    # Post-freeze in a never-baselined hour (13), with a much stronger signal.
    events = []
    for i in range(6):
        clock[0] = T0 + timedelta(hours=37, minutes=i)
        events += engine.update([dev(-50)])
    ev = events[-1]
    assert ev.score_breakdown["off_schedule"] == 1.0
    assert ev.score_breakdown["approaching"] == 1.0
    assert ev.alert_level == "likely"
    assert ev.score == 0.7


def test_approaching_threshold_env_overridable():
    # Tighten the dB floor via env so a +4 dB rise (normally below the 6 dB
    # default) now trips approaching.
    holder = [T0]
    store = BaselineStore(":memory:", baseline_hours=1.0, now=T0)
    with patch.dict(os.environ, {"APPROACHING_MIN_DB_MARGIN": "3"}):
        engine = FixedScoring(store=store, clock=lambda: holder[0])
    _seed_baseline_signal(engine, holder, 12, -70)     # steady baseline
    events = _feed_recent(engine, holder, 6, -66)      # +4 dB >= 3 dB floor now
    assert events
    assert events[-1].score_breakdown["approaching"] == 1.0


# ---------------------------------------------------------------------------
# AP-exclusion from approaching (Wi-Fi APs don't physically move)
# ---------------------------------------------------------------------------


def test_approaching_excluded_for_wifi_ap():
    # Same strong rise as the client case, but a Wi-Fi AP must NOT flag
    # approaching — and the suppression is recorded for observability.
    engine, clock = _clocked_engine(baseline_hours=1.0)
    _seed_baseline_signal(engine, clock, 12, -70, jitter=[-1, 0, 1], dtype="Wi-Fi AP")
    assert _feed_recent(engine, clock, 6, -50) == []          # suppressed
    assert engine._store.get_profile("mac:" + _AMAC).device_type == "Wi-Fi AP"
    assert ("mac:" + _AMAC) in engine._approaching_excluded_aps
    assert engine.status()["approaching_excluded_aps"] == 1


def test_approaching_client_fires_where_identical_ap_would_not():
    # Direct contrast: identical baseline + rise; client fires, AP does not.
    eng_c, c_c = _clocked_engine(baseline_hours=1.0)
    _seed_baseline_signal(eng_c, c_c, 12, -70, jitter=[-1, 0, 1],
                          mac="d8:96:85:cc:cc:cc", dtype="Wi-Fi Client")
    fired_client = _feed_recent(eng_c, c_c, 6, -50, mac="d8:96:85:cc:cc:cc")

    eng_a, c_a = _clocked_engine(baseline_hours=1.0)
    _seed_baseline_signal(eng_a, c_a, 12, -70, jitter=[-1, 0, 1],
                          mac="d8:96:85:aa:aa:aa", dtype="Wi-Fi AP")
    fired_ap = _feed_recent(eng_a, c_a, 6, -50, mac="d8:96:85:aa:aa:aa")

    assert fired_client and fired_client[-1].score_breakdown["approaching"] == 1.0
    assert fired_ap == []


def test_wds_ap_excluded_but_plain_wds_and_bridged_eligible():
    # 'Wi-Fi WDS AP' is infrastructure (excluded); 'Wi-Fi WDS' and 'Wi-Fi
    # Bridged' stay eligible under the narrow filter.
    from modules.fixed_scoring import _is_access_point
    assert _is_access_point("Wi-Fi AP") is True
    assert _is_access_point("Wi-Fi WDS AP") is True
    assert _is_access_point("Wi-Fi WDS") is False
    assert _is_access_point("Wi-Fi Bridged") is False
    assert _is_access_point("Wi-Fi Client") is False
    assert _is_access_point("Wi-Fi Ad-Hoc") is False
    assert _is_access_point("") is False
    assert _is_access_point(None) is False


def test_ap_filter_does_not_affect_novelty():
    # A novel Wi-Fi AP still flags on novelty exactly as before — the AP filter
    # only touches the approaching signal.
    engine, clock = _clocked_engine(baseline_hours=1.0)
    clock[0] = T0 + timedelta(hours=2)                 # frozen; device is novel
    dev = {"macaddr": "d8:96:85:ap:00:01".replace("ap", "a0"),
           "manuf": "Acme", "type": "Wi-Fi AP"}
    assert engine.update([dev]) == []                  # 1 sighting
    events = engine.update([dev])                      # novel-persistent AP
    assert len(events) == 1
    assert events[0].score_breakdown["novelty"] == 1.0


def test_ap_filter_does_not_affect_off_schedule():
    # A known Wi-Fi AP seen off-schedule still flags off-schedule (AP filter
    # only touches approaching).
    engine, clock = _clocked_engine(baseline_hours=24.0)
    ap = _static_device(mac="d8:96:85:0a:0b:0c", type="Wi-Fi AP")
    _seed_distinct_hours(engine, clock, ap, 12)        # 12 distinct baseline hours
    clock[0] = T0 + timedelta(hours=37)                # frozen; hour 13 unbaselined
    events = engine.update([ap])
    assert len(events) == 1
    assert events[0].score_breakdown["off_schedule"] == 1.0
