"""Tests for the durable SQLite baseline store (design 5.1).

The headline test is the crash-loop regression: a restart must RESUME the
existing learning window, never reset it.
"""

from datetime import datetime, timedelta, timezone

from modules.baseline_store import BaselineStore, DeviceProfile

T0 = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)


def test_schema_created_on_fresh_db(tmp_path):
    store = BaselineStore(str(tmp_path / "b.db"), baseline_hours=72, now=T0)
    assert store.profile_count() == 0
    assert store.learning_start == T0
    store.close()


def test_freeze_time_and_is_learning(tmp_path):
    store = BaselineStore(str(tmp_path / "b.db"), baseline_hours=10, now=T0)
    assert store.freeze_time == T0 + timedelta(hours=10)
    assert store.is_learning(T0 + timedelta(hours=5)) is True
    assert store.is_learning(T0 + timedelta(hours=11)) is False
    store.close()


def test_upsert_inserts_then_increments(tmp_path):
    store = BaselineStore(str(tmp_path / "b.db"), baseline_hours=72, now=T0)
    p1 = store.upsert("mac:aa", T0, manufacturer="Acme", device_type="AP")
    assert isinstance(p1, DeviceProfile)
    assert p1.observation_count == 1
    assert p1.first_seen == T0

    later = T0 + timedelta(minutes=30)
    p2 = store.upsert("mac:aa", later)
    assert p2.observation_count == 2
    assert p2.first_seen == T0          # first_seen never moves
    assert p2.last_seen == later
    assert p2.manufacturer == "Acme"    # retained when not re-supplied
    store.close()


def test_profiles_survive_reopen(tmp_path):
    db = str(tmp_path / "b.db")
    s1 = BaselineStore(db, baseline_hours=72, now=T0)
    s1.upsert("mac:aa", T0)
    s1.upsert("mac:aa", T0 + timedelta(minutes=1))
    s1.close()

    s2 = BaselineStore(db, baseline_hours=72, now=T0 + timedelta(hours=1))
    prof = s2.get_profile("mac:aa")
    assert prof is not None
    assert prof.observation_count == 2
    assert prof.first_seen == T0
    s2.close()


def test_crash_loop_does_not_reset_learning_window(tmp_path):
    """THE critical property: restarting NEVER resets learning_start.

    Simulates the 2026-06 crash loop — many restarts at different wall-clock
    times. The learning window must keep its original start, so the node
    eventually freezes the baseline and alerts instead of learning forever.
    """
    db = str(tmp_path / "b.db")

    s1 = BaselineStore(db, baseline_hours=72, now=T0)
    original_start = s1.learning_start
    assert original_start == T0
    s1.close()

    # Restart #1 — five hours later.
    s2 = BaselineStore(db, baseline_hours=72, now=T0 + timedelta(hours=5))
    assert s2.learning_start == original_start
    s2.close()

    # Restart #2 — three days later (would have been "still learning" if reset).
    s3 = BaselineStore(db, baseline_hours=72, now=T0 + timedelta(hours=72))
    assert s3.learning_start == original_start
    # The baseline is now frozen relative to the ORIGINAL start, not the restart.
    assert s3.is_learning(T0 + timedelta(hours=73)) is False
    s3.close()


def test_baseline_hours_retunable_but_start_immutable(tmp_path):
    db = str(tmp_path / "b.db")
    s1 = BaselineStore(db, baseline_hours=72, now=T0)
    s1.close()
    # Operator retunes the window; learning_start must stay put.
    s2 = BaselineStore(db, baseline_hours=24, now=T0 + timedelta(hours=1))
    assert s2.learning_start == T0
    assert s2.freeze_time == T0 + timedelta(hours=24)
    s2.close()


def test_baseline_count_counts_pre_freeze_profiles(tmp_path):
    store = BaselineStore(str(tmp_path / "b.db"), baseline_hours=10, now=T0)
    store.upsert("mac:in1", T0 + timedelta(hours=1))
    store.upsert("mac:in2", T0 + timedelta(hours=2))
    store.upsert("mac:out", T0 + timedelta(hours=20))  # first seen after freeze
    assert store.baseline_count() == 2
    assert store.profile_count() == 3
    store.close()
