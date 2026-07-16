"""Tests for the durable SQLite baseline store (design 5.1).

The headline test is the crash-loop regression: a restart must RESUME the
existing learning window, never reset it.
"""

import threading
from datetime import datetime, timedelta, timezone

from modules.baseline_store import BaselineStore, DeviceProfile

T0 = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)


def test_reads_from_another_thread_do_not_raise(tmp_path):
    """The GUI (Flask thread) reads status counts off the connection the asyncio
    thread opened. With check_same_thread=True this raised a ProgrammingError and
    the GUI showed 'scoring not active'; the lock + check_same_thread=False fix it."""
    store = BaselineStore(str(tmp_path / "b.db"), baseline_hours=72, now=T0)
    store.upsert("mac:aa:bb:cc:dd:ee:ff", T0)

    results = {}

    def reader():
        try:
            results["profiles"] = store.profile_count()
            results["baseline"] = store.baseline_count()
            results["start"] = store.learning_start
        except Exception as exc:  # would fire on the cross-thread SQLite error
            results["error"] = exc

    t = threading.Thread(target=reader)
    t.start()
    t.join()

    assert "error" not in results, f"cross-thread read raised: {results.get('error')}"
    assert results["profiles"] == 1
    assert results["baseline"] == 1


def test_concurrent_upsert_and_reads_are_safe(tmp_path):
    """Concurrent poll-thread writes + GUI-thread reads must not raise or corrupt."""
    store = BaselineStore(str(tmp_path / "b.db"), baseline_hours=72, now=T0)
    errors = []

    def writer():
        try:
            for i in range(50):
                store.upsert(f"mac:00:00:00:00:00:{i:02x}", T0)
        except Exception as exc:
            errors.append(exc)

    def reader():
        try:
            for _ in range(50):
                store.profile_count()
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=writer), threading.Thread(target=reader)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert store.profile_count() == 50


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


# ---------------------------------------------------------------------------
# Phase 2 — reserved columns now populated (hour mask + signal stats)
# ---------------------------------------------------------------------------


def test_upsert_accumulates_hour_mask(tmp_path):
    store = BaselineStore(str(tmp_path / "b.db"), baseline_hours=72, now=T0)
    store.upsert("mac:a", T0)                       # hour 0
    store.upsert("mac:a", T0 + timedelta(hours=5))  # hour 5
    p = store.get_profile("mac:a")
    assert p.hour_mask == (1 << 0) | (1 << 5)
    store.close()


def test_upsert_signal_stats_welford_and_none_skipped(tmp_path):
    store = BaselineStore(str(tmp_path / "b.db"), baseline_hours=72, now=T0)
    store.upsert("mac:a", T0, last_signal=-50)
    store.upsert("mac:a", T0, last_signal=-60)
    store.upsert("mac:a", T0, last_signal=None)   # skipped, not counted
    p = store.get_profile("mac:a")
    assert p.signal_count == 2
    assert p.signal_mean == -55.0
    assert p.signal_var == 25.0                    # population variance of [-50, -60]
    store.close()


def test_accumulate_baseline_false_freezes_stats_but_advances_recency(tmp_path):
    store = BaselineStore(str(tmp_path / "b.db"), baseline_hours=72, now=T0)
    store.upsert("mac:a", T0, last_signal=-50)                       # baseline
    store.upsert("mac:a", T0 + timedelta(hours=9), last_signal=-90,
                 accumulate_baseline=False)                          # post-freeze
    p = store.get_profile("mac:a")
    assert p.hour_mask == (1 << 0)        # hour 9 NOT added to baseline
    assert p.signal_count == 1            # -90 NOT counted
    assert p.signal_mean == -50.0
    assert p.observation_count == 2       # recency still advanced
    store.close()


def test_phase2_stats_survive_reopen_and_keep_accumulating(tmp_path):
    db = str(tmp_path / "b.db")
    s1 = BaselineStore(db, baseline_hours=72, now=T0)
    s1.upsert("mac:a", T0, last_signal=-50)
    s1.upsert("mac:a", T0 + timedelta(hours=3), last_signal=-54)
    s1.close()

    s2 = BaselineStore(db, baseline_hours=72, now=T0 + timedelta(hours=1))
    p = s2.get_profile("mac:a")
    assert p.hour_mask == (1 << 0) | (1 << 3)
    assert p.signal_count == 2
    assert p.signal_mean == -52.0
    # Welford accumulator resumes correctly across the restart.
    s2.upsert("mac:a", T0 + timedelta(hours=7), last_signal=-52)
    p2 = s2.get_profile("mac:a")
    assert p2.hour_mask == (1 << 0) | (1 << 3) | (1 << 7)
    assert p2.signal_count == 3
    s2.close()


# ---------------------------------------------------------------------------
# Phase 2.5 — zero-signal skip + post-freeze recent-signal EMA
# ---------------------------------------------------------------------------


def test_zero_signal_skipped_in_baseline_like_none(tmp_path):
    store = BaselineStore(str(tmp_path / "b.db"), baseline_hours=72, now=T0)
    store.upsert("mac:a", T0, last_signal=-50)
    store.upsert("mac:a", T0, last_signal=0)      # placeholder -> skipped
    store.upsert("mac:a", T0, last_signal=None)   # missing -> skipped
    p = store.get_profile("mac:a")
    assert p.signal_count == 1                    # only the -50 counted
    assert p.signal_mean == -50.0
    store.close()


def test_recent_ema_accumulates_only_post_freeze(tmp_path):
    store = BaselineStore(str(tmp_path / "b.db"), baseline_hours=10, now=T0)
    store.upsert("mac:a", T0, last_signal=-70)    # learning -> baseline, not recent
    p = store.get_profile("mac:a")
    assert p.recent_signal_count == 0
    assert p.recent_signal_mean is None
    # Post-freeze: feeds the recent EMA; zeros/None skipped there too.
    store.upsert("mac:a", T0 + timedelta(hours=20), last_signal=-50, accumulate_baseline=False)
    store.upsert("mac:a", T0 + timedelta(hours=20), last_signal=0, accumulate_baseline=False)
    store.upsert("mac:a", T0 + timedelta(hours=20), last_signal=-50, accumulate_baseline=False)
    p = store.get_profile("mac:a")
    assert p.recent_signal_count == 2             # two -50s; the 0 skipped
    assert p.recent_signal_mean == -50.0          # EMA of identical values
    assert p.signal_mean == -70.0                 # baseline untouched post-freeze
    store.close()


def test_recent_ema_survives_reopen(tmp_path):
    db = str(tmp_path / "b.db")
    s1 = BaselineStore(db, baseline_hours=10, now=T0)
    s1.upsert("mac:a", T0, last_signal=-70)                                   # baseline
    s1.upsert("mac:a", T0 + timedelta(hours=20), last_signal=-55, accumulate_baseline=False)
    s1.close()
    s2 = BaselineStore(db, baseline_hours=10, now=T0 + timedelta(hours=21))
    p = s2.get_profile("mac:a")
    assert p.recent_signal_count == 1
    assert p.recent_signal_mean == -55.0
    s2.upsert("mac:a", T0 + timedelta(hours=22), last_signal=-55, accumulate_baseline=False)
    assert s2.get_profile("mac:a").recent_signal_count == 2
    s2.close()


# ---------------------------------------------------------------------------
# Durability pragmas — WAL cuts the per-commit fsync stall of the per-device,
# per-poll upsert on the asyncio thread (matches the entity_store hardening).
# The correctness invariant (never reset learning_start) is unaffected.
# ---------------------------------------------------------------------------


def test_file_backed_store_runs_in_wal_mode(tmp_path):
    store = BaselineStore(str(tmp_path / "b.db"), baseline_hours=72, now=T0)
    mode = store._conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode == "wal"
    store.close()


def test_bulk_upserts_durable_across_reopen_under_wal(tmp_path):
    # A learning-phase burst (many devices, each its own commit) must all survive
    # a clean close + reopen — WAL checkpoints on the final connection closing.
    db = str(tmp_path / "b.db")
    s1 = BaselineStore(db, baseline_hours=72, now=T0)
    for i in range(200):
        s1.upsert(f"mac:00:00:00:00:00:{i:02x}", T0, last_signal=-50 - (i % 10))
    assert s1.profile_count() == 200
    s1.close()

    s2 = BaselineStore(db, baseline_hours=72, now=T0 + timedelta(hours=1))
    assert s2.profile_count() == 200
    assert s2.learning_start == T0            # invariant intact under WAL
    p = s2.get_profile("mac:00:00:00:00:00:05")
    assert p is not None and p.observation_count == 1
    s2.close()


# ---------------------------------------------------------------------------
# batch() — one commit per poll pass (2026-07-14 watchdog-stall regression)
# ---------------------------------------------------------------------------

def _committed_profiles(db_path):
    """Count device_profiles rows a FRESH connection can see — i.e. committed
    state only. Uncommitted work on the store's own connection is invisible here."""
    import sqlite3
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute("SELECT COUNT(*) FROM device_profiles").fetchone()[0]
    finally:
        conn.close()


def test_batch_defers_commit_until_exit(tmp_path):
    """Inside batch() nothing is committed; the outermost exit commits it all.

    Per-device commits inside FixedScoring.update() overran the 2-minute systemd
    watchdog at ~12.5k devices/poll (2026-07-14 outage) — one poll, one commit."""
    db = str(tmp_path / "b.db")
    store = BaselineStore(db, baseline_hours=72, now=T0)

    with store.batch():
        for i in range(50):
            store.upsert(f"mac:00:00:00:00:01:{i:02x}", T0)
        assert _committed_profiles(db) == 0     # still one open transaction

    assert _committed_profiles(db) == 50        # single commit on exit
    store.close()


def test_batch_commits_prior_work_on_exception(tmp_path):
    """An exception mid-batch must not roll back sightings already folded in —
    those observations happened; losing them would silently thin the baseline."""
    db = str(tmp_path / "b.db")
    store = BaselineStore(db, baseline_hours=72, now=T0)

    class Boom(Exception):
        pass

    try:
        with store.batch():
            store.upsert("mac:00:00:00:00:02:01", T0)
            store.upsert("mac:00:00:00:00:02:02", T0)
            raise Boom()
    except Boom:
        pass

    assert _committed_profiles(db) == 2
    # The store must remain usable (batch depth unwound, lock released).
    store.upsert("mac:00:00:00:00:02:03", T0)
    assert _committed_profiles(db) == 3
    store.close()


def test_nested_batches_commit_once_at_outermost_exit(tmp_path):
    db = str(tmp_path / "b.db")
    store = BaselineStore(db, baseline_hours=72, now=T0)

    with store.batch():
        store.upsert("mac:00:00:00:00:03:01", T0)
        with store.batch():
            store.upsert("mac:00:00:00:00:03:02", T0)
        # Inner exit must NOT commit — only the outermost does.
        assert _committed_profiles(db) == 0
    assert _committed_profiles(db) == 2
    store.close()


def test_upsert_outside_batch_still_commits_immediately(tmp_path):
    db = str(tmp_path / "b.db")
    store = BaselineStore(db, baseline_hours=72, now=T0)
    store.upsert("mac:00:00:00:00:04:01", T0)
    assert _committed_profiles(db) == 1
    store.close()


def test_gui_reads_interleave_during_open_batch(tmp_path):
    """batch() deliberately does not hold the store lock for the whole block, so
    the GUI thread's status() reads keep interleaving between upserts instead of
    blocking for a full poll pass."""
    db = str(tmp_path / "b.db")
    store = BaselineStore(db, baseline_hours=72, now=T0)

    results = {}
    with store.batch():
        store.upsert("mac:00:00:00:00:05:01", T0)

        def reader():
            try:
                # Same connection => sees the store's own uncommitted work; the
                # point is it neither raises nor deadlocks mid-batch.
                results["profiles"] = store.profile_count()
            except Exception as exc:
                results["error"] = exc

        t = threading.Thread(target=reader)
        t.start()
        t.join(timeout=10)
        assert not t.is_alive(), "GUI-style read deadlocked against an open batch"

    assert "error" not in results, f"mid-batch read raised: {results.get('error')}"
    assert results["profiles"] == 1
    store.close()
