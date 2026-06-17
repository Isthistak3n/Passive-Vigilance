"""Tests for P3 rolling-baseline adaptation — off-hardware, synthetic presence.

Covers the roadmap P3 exit gate: a consistently-present device is promoted and
stops flagging novel; an intermittent returner is not absorbed; a promoted device
absent past the window demotes and reads novel again; ``off`` promotes nothing;
demotion never touches the original frozen baseline; the slow-in/fast-out and
posture guards fail correctly; and the promotion policy is a swappable seam.
"""

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from modules.baseline_store import BaselineStore
from modules.fixed_scoring import FixedScoring
from modules.promotion_policy import (
    AdaptationParams,
    PresenceRecord,
    PromotionPolicy,
    SustainedPresencePolicy,
    resolve_adaptation,
)

UTC = timezone.utc
_T0 = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
_MAC = "a4:bb:cc:dd:ee:f0"   # static MAC (second hex digit 4) -> keyed "mac:<mac>"
_KEY = "mac:" + _MAC


def _dev(mac=_MAC, signal=-60):
    return {"macaddr": mac, "manuf": "Acme", "type": "Wi-Fi Client",
            "last_signal": signal}


def _frozen_engine(clock):
    """A FixedScoring whose baseline is already frozen (baseline_hours=0), so every
    sighting is post-freeze. ``clock`` is a one-key dict used as the time source."""
    store = BaselineStore(":memory:", baseline_hours=0.0, now=_T0)
    return FixedScoring(store=store, clock=lambda: clock["t"]), store


def _novelty_fired(events):
    return any(e.score_breakdown.get("novelty") for e in events)


# ---------------------------------------------------------------------------
# Promotion policy / posture resolution (invariant + seam guards)
# ---------------------------------------------------------------------------


def test_absent_posture_resolves_off():
    posture, params = resolve_adaptation({})
    assert posture == "off" and params is None


def test_garbage_posture_resolves_off():
    posture, params = resolve_adaptation({"ADAPTATION_POSTURE": "banana"})
    assert posture == "off" and params is None


def test_recognized_posture_returns_params():
    posture, params = resolve_adaptation({"ADAPTATION_POSTURE": "Permissive"})
    assert posture == "permissive"
    assert isinstance(params, AdaptationParams)


def test_malformed_override_fails_loud():
    with pytest.raises(ValueError):
        resolve_adaptation({"ADAPTATION_POSTURE": "permissive",
                            "ADAPT_PROMO_MIN_DAYS": "abc"})


def test_slow_in_fast_out_invariant_fails_loud():
    # demote_after (1000h) >= promo_min_span (permissive 72h) violates the invariant.
    with pytest.raises(ValueError):
        resolve_adaptation({"ADAPTATION_POSTURE": "permissive",
                            "ADAPT_DEMOTE_AFTER_HOURS": "1000"})


def test_presets_obey_slow_in_fast_out():
    for name in ("conservative", "permissive"):
        _, params = resolve_adaptation({"ADAPTATION_POSTURE": name})
        assert params.demote_after < params.promo_min_span


def test_sustained_presence_policy_thresholds():
    params = AdaptationParams(promo_min_days=3, promo_min_span=timedelta(days=3),
                              promo_min_distinct_hours=4, demote_after=timedelta(hours=24))
    pol = SustainedPresencePolicy()
    ok = PresenceRecord(key=_KEY, mac_type="static", pf_first=_T0,
                        pf_last=_T0 + timedelta(days=3), distinct_days=3,
                        adapt_hour_mask=0b1111, observation_count=20, now=_T0)
    assert pol.should_promote(ok, params) is True
    thin = PresenceRecord(key=_KEY, mac_type="static", pf_first=_T0,
                          pf_last=_T0 + timedelta(days=1), distinct_days=1,
                          adapt_hour_mask=0b1, observation_count=99, now=_T0)
    assert pol.should_promote(thin, params) is False   # high count, one day -> no


# ---------------------------------------------------------------------------
# Promotion / demotion through the engine + store (the exit-gate cases)
# ---------------------------------------------------------------------------


def _bank_presence(engine, clock, days, hours=(8, 12, 16, 20), start=None):
    """Drive post-freeze sightings across ``days`` distinct days at ``hours``."""
    base = (start or _T0) + timedelta(seconds=1)
    last = base
    for day in range(days):
        for hour in hours:
            last = base + timedelta(days=day, hours=hour)
            clock["t"] = last
            engine.update([_dev()])
    return last


def test_sustained_presence_promotes_and_stops_novelty(monkeypatch):
    monkeypatch.setenv("ADAPTATION_POSTURE", "permissive")   # 3 days / 3d / 4 hours
    clock = {"t": _T0}
    engine, store = _frozen_engine(clock)

    last = _bank_presence(engine, clock, days=4)
    clock["t"] = last + timedelta(minutes=1)
    # Pre-sweep: still novel (post-freeze, not yet promoted).
    assert _novelty_fired(engine.update([_dev()]))

    engine.run_adaptation_sweep(clock["t"])
    assert store.get_profile(_KEY).promoted is True

    # Post-promotion: novelty no longer fires for it.
    assert not _novelty_fired(engine.update([_dev()]))


def test_intermittent_returner_not_promoted(monkeypatch):
    monkeypatch.setenv("ADAPTATION_POSTURE", "conservative")  # needs 5 days
    clock = {"t": _T0}
    engine, store = _frozen_engine(clock)

    _bank_presence(engine, clock, days=2)   # only 2 distinct days -> below threshold
    engine.run_adaptation_sweep(clock["t"])
    assert store.get_profile(_KEY).promoted is False


def test_promoted_then_absent_demotes_and_reads_novel(monkeypatch):
    monkeypatch.setenv("ADAPTATION_POSTURE", "permissive")    # demote_after 48h
    clock = {"t": _T0}
    engine, store = _frozen_engine(clock)

    last = _bank_presence(engine, clock, days=4)
    clock["t"] = last + timedelta(minutes=1)
    engine.run_adaptation_sweep(clock["t"])
    assert store.get_profile(_KEY).promoted is True

    # Advance past the demotion window WITHOUT a sighting (last_seen stays put).
    clock["t"] = last + timedelta(hours=49)
    events = engine.run_adaptation_sweep(clock["t"])
    assert store.get_profile(_KEY).promoted is False
    assert events and events[0]["event_type"] == "baseline_demotion"
    assert events[0]["fingerprint"] == _KEY
    assert events[0]["absence_seconds"] >= 48 * 3600

    # A later sighting reads novel again (lost its earned baseline status).
    assert _novelty_fired(engine.update([_dev()]))


def test_off_posture_promotes_nothing(monkeypatch):
    monkeypatch.delenv("ADAPTATION_POSTURE", raising=False)   # default -> off
    clock = {"t": _T0}
    engine, store = _frozen_engine(clock)
    assert engine._adaptation_posture == "off"

    _bank_presence(engine, clock, days=6)   # plenty of presence
    assert engine.run_adaptation_sweep(clock["t"]) == []
    assert store.get_profile(_KEY).promoted is False
    # Still flags novel — exactly today's frozen behaviour.
    assert _novelty_fired(engine.update([_dev()]))


def test_demotion_never_touches_original_baseline(monkeypatch):
    monkeypatch.setenv("ADAPTATION_POSTURE", "permissive")
    clock = {"t": _T0}
    # Learning window of 1h so the first sighting lands in the baseline.
    store = BaselineStore(":memory:", baseline_hours=1.0, now=_T0)
    engine = FixedScoring(store=store, clock=lambda: clock["t"])

    clock["t"] = _T0 + timedelta(minutes=5)         # within learning window
    engine.update([_dev()])                          # first_seen <= freeze -> baseline
    assert store.get_profile(_KEY).first_seen <= store.freeze_time

    # Long absence, well past any demotion window.
    clock["t"] = _T0 + timedelta(days=20)
    events = engine.run_adaptation_sweep(clock["t"])
    assert all(e["fingerprint"] != _KEY for e in events)
    assert store.get_profile(_KEY).promoted is False
    # An original-baseline device is not novel and is never demoted.
    assert not _novelty_fired(engine.update([_dev()]))


def test_policy_seam_swappable_without_touching_sweep(monkeypatch):
    monkeypatch.setenv("ADAPTATION_POSTURE", "permissive")
    clock = {"t": _T0}
    engine, store = _frozen_engine(clock)

    class _AlwaysPromote(PromotionPolicy):
        def should_promote(self, rec, params):
            return True

    engine._promotion_policy = _AlwaysPromote()   # one-line swap, nothing else changes
    clock["t"] = _T0 + timedelta(hours=1)
    engine.update([_dev()])                        # one post-freeze sighting -> candidate
    engine.run_adaptation_sweep(clock["t"])
    assert store.get_profile(_KEY).promoted is True


# ---------------------------------------------------------------------------
# Schema migration (existing baselines on disk gain the new columns)
# ---------------------------------------------------------------------------


def test_migration_adds_promoted_columns(tmp_path):
    path = str(tmp_path / "old.db")
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE device_profiles (key TEXT PRIMARY KEY, first_seen TEXT, "
        "last_seen TEXT, observation_count INTEGER, manufacturer TEXT, "
        "device_type TEXT, mac_type TEXT, dwell_seconds REAL, time_histogram TEXT, "
        "signal_mean REAL, signal_var REAL)"
    )
    conn.execute(
        "INSERT INTO device_profiles (key, first_seen, last_seen, observation_count) "
        "VALUES (?, ?, ?, 1)", (_KEY, _T0.isoformat(), _T0.isoformat()),
    )
    conn.commit()
    conn.close()

    store = BaselineStore(path, baseline_hours=1.0)   # opens + migrates in place
    prof = store.get_profile(_KEY)
    assert prof is not None
    assert prof.promoted is False
    assert prof.promotion_ts is None
    store.close()
