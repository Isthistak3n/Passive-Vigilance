"""entity_store — durable SQLite tables for the entity/observation foundation.

Phase A: persists, additively and alongside the existing in-memory
``PersistenceEngine._observations`` path, the probe-SSID evidence, per-device
fingerprint, logical entities, and a growing observation history. It does NOT
participate in scoring and is never on the path that produces alerts — a write
failure here must never affect detection (callers guard the call).

The single most important correctness property: every per-device, per-poll write
except the observation history is a real UPSERT (``INSERT ... ON CONFLICT ... DO
UPDATE``). A miskeyed upsert that inserts a fresh row every poll would recreate
the in-memory growth problem on disk; the row counts for a stable device set
must level off, not climb per poll. Only the ``observations`` table grows by
design (history); it is bounded by a time-based retention window — see
``prune_observations`` — so an always-on node does not fill the disk.
"""

import logging
import os
import queue
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# Default DB path derived from this file's location (modules/ -> repo root), so
# it resolves identically under systemd or by hand; lives under gitignored data/.
_DEFAULT_ENTITY_DB_PATH = str(Path(__file__).resolve().parent.parent / "data" / "entities.db")

# Observation history retention. Generous by default so cross-session entity
# resolution has plenty to work with; set the days to 0 (or negative) to keep
# history forever.
#
# NOTE: these env-derived defaults are read inside __init__, not as module-level
# constants — a module-level `os.getenv()` is baked in once at *import* time, so its
# value depends on whatever happened to already be in os.environ at that moment
# (e.g. another test module's import-time `load_dotenv()` racing this one during
# pytest collection). Reading at construction time instead makes every default here
# consistent with the rest of the codebase's env-handling convention (see GPS_MIN_
# QUALITY / GPS_MAX_HDOP in gps.py) and immune to import-order-dependent env leakage.

# AP-beacon capture + network-affinity recording. Pure capture (no scoring effect),
# default on; set false to skip the beacon_evidence / network_affinity writes.
_BEACON_CAPTURE_ENABLED = os.getenv("BEACON_CAPTURE_ENABLED", "true").strip().lower() != "false"


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def _parse_iso(s) -> Optional[datetime]:
    """Parse a stored UTC ISO timestamp, tolerant of a naive string; None on failure."""
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s)
    except (ValueError, TypeError):
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


class EntityStore:
    """SQLite-backed durable store for probe evidence, fingerprints, entities,
    and observation history. All writes are additive; none affect scoring."""

    def __init__(self, db_path: Optional[str] = None,
                 retention_days: Optional[int] = None,
                 prune_interval_s: Optional[int] = None,
                 prune_batch_rows: Optional[int] = None,
                 prune_time_budget_s: Optional[float] = None,
                 max_observation_rows: Optional[int] = None,
                 wal_checkpoint_s: Optional[int] = None,
                 async_writes: Optional[bool] = None,
                 write_queue_max: Optional[int] = None,
                 audible_window_s: Optional[int] = None) -> None:
        self._db_path = db_path or _DEFAULT_ENTITY_DB_PATH
        self._retention_days = int(
            os.getenv("ENTITY_OBSERVATION_RETENTION_DAYS", "30")
            if retention_days is None else retention_days
        )
        self._prune_interval_s = int(
            os.getenv("ENTITY_PRUNE_INTERVAL_SECONDS", "3600")
            if prune_interval_s is None else prune_interval_s
        )
        # Hard ceiling on the observations table, independent of the age window. The
        # age-only retention above deletes nothing until a row is older than the
        # window, so on a busy node the file grows unchecked for the whole window
        # first — that is how entities.db reached 7.6 GB on an SD card and stalled
        # the poll loop past the systemd watchdog (2026-07). The row cap gives a
        # real, near-term plateau: once the table exceeds it, the oldest rows are
        # pruned regardless of age. 0 disables the cap (age-only).
        self._max_obs_rows = max(0, int(
            os.getenv("ENTITY_OBSERVATION_MAX_ROWS", "4000000")
            if max_observation_rows is None else max_observation_rows
        ))
        # WAL-truncation cadence. WAL mode only auto-checkpoints in PASSIVE mode,
        # which a high-rate writer on slow storage perpetually starves — the WAL
        # then grows without bound (it reached ~1 GB alongside the 7.6 GB DB, and
        # replaying it stalled every open). A periodic TRUNCATE checkpoint takes
        # the write lock, folds the WAL back into the DB, and resets the WAL file
        # to zero. 0 disables.
        self._wal_checkpoint_s = max(0, int(
            os.getenv("ENTITY_WAL_CHECKPOINT_SECONDS", "300")
            if wal_checkpoint_s is None else wal_checkpoint_s
        ))
        self._last_wal_checkpoint: Optional[datetime] = None
        # Retention-sweep bounds: rows per DELETE statement and wall-clock budget
        # per sweep. The sweep runs on the asyncio poll thread, so one sweep must
        # never be allowed to hold the loop for minutes (see prune_observations).
        self._prune_batch_rows = max(1, int(
            os.getenv("ENTITY_PRUNE_BATCH_ROWS", "5000")
            if prune_batch_rows is None else prune_batch_rows))
        self._prune_budget_s = float(
            os.getenv("ENTITY_PRUNE_TIME_BUDGET_S", "1.0")
            if prune_time_budget_s is None else prune_time_budget_s)
        self._last_prune: Optional[datetime] = None
        # Audible-only sighting filter. Kismet's device list is CUMULATIVE for the
        # session, and fixed nodes take the full list (KISMET_ACTIVE_WINDOW_SECONDS=0
        # is required there for baseline learning) — so without this filter a device
        # heard once keeps generating a fresh observation row every poll for the rest
        # of the session. Row creation is then O(cumulative devices × polls), which is
        # what outran the pruner and filled the disk (2026-07-18, issue #211). When
        # set, only devices whose Kismet last_time falls within this many seconds of
        # the poll are persisted; devices with no last_time are recorded anyway (fail
        # open — BLE records and tests don't carry the field). The filter lives here,
        # at the persistence site: the unfiltered list still reaches scoring/baseline.
        # 0 disables it (legacy behavior: persist the entire list every poll).
        self._audible_window_s = max(0, int(
            os.getenv("ENTITY_AUDIBLE_WINDOW_SECONDS", "0")
            if audible_window_s is None else audible_window_s))
        if self._db_path != ":memory:":
            Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self._db_path)
        self._conn.row_factory = sqlite3.Row
        self._apply_pragmas(self._conn)
        self._create_schema()

        # Off-loop writer (experimental, default OFF). When enabled, record_poll
        # enqueues the poll to a dedicated writer thread with its OWN connection
        # instead of writing on the caller's (asyncio) thread — so even a multi-
        # second SD fsync can't block the poll loop past the watchdog. Reads stay
        # on the main connection (WAL lets them proceed without the writer's
        # lock). Only meaningful for a file DB; ignored for ":memory:" (a second
        # connection there is a separate database). Bounded queue: under sustained
        # backpressure a poll's observations are dropped rather than blocking
        # capture. Its own connection is created on the writer thread so SQLite's
        # per-thread ownership holds.
        if async_writes is None:
            want_async = os.getenv("ENTITY_ASYNC_WRITES", "false").strip().lower() in (
                "1", "true", "yes", "on")
        else:
            want_async = async_writes
        self._async_writes = bool(want_async) and self._db_path != ":memory:"
        self._write_queue_max = max(1, int(
            os.getenv("ENTITY_WRITE_QUEUE_MAX", "240")
            if write_queue_max is None else write_queue_max))
        self._writer_thread: Optional[threading.Thread] = None
        self._writer_conn: Optional[sqlite3.Connection] = None
        self._write_q: "Optional[queue.Queue]" = None
        self._write_drops = 0
        if self._async_writes:
            self._start_writer()

    def _apply_pragmas(self, conn: sqlite3.Connection) -> None:
        """Tune the connection for an always-on writer on slow storage.

        WAL + synchronous=NORMAL cut the per-commit fsync stall on the Pi's SD
        card (every poll commits thousands of upserts on the asyncio thread);
        busy_timeout rides out a transient lock instead of raising immediately.
        auto_vacuum=INCREMENTAL lets the retention sweep hand freed pages back
        to the filesystem — it only takes effect on a freshly created database
        (an existing file needs a one-time offline VACUUM to adopt it), so it
        is best-effort. Every pragma is guarded: a tuning failure must never
        block the store.
        """
        for pragma in (
            "PRAGMA auto_vacuum=INCREMENTAL",
            "PRAGMA journal_mode=WAL",
            "PRAGMA synchronous=NORMAL",
            "PRAGMA busy_timeout=5000",
        ):
            try:
                conn.execute(pragma)
            except sqlite3.Error as exc:  # pragma: no cover - defensive
                logger.debug("EntityStore pragma failed (%s): %s", pragma, exc)

    # ------------------------------------------------------------------
    # Off-loop writer (experimental; default off)
    # ------------------------------------------------------------------

    def _active_conn(self) -> sqlite3.Connection:
        """The connection the write path should use: the writer thread's own
        connection when a poll is being drained on it, else the main connection
        (sync mode, and all main-thread reads). In sync mode this is always the
        main connection, so the default path is byte-for-byte unchanged."""
        if (self._async_writes
                and threading.current_thread() is self._writer_thread
                and self._writer_conn is not None):
            return self._writer_conn
        return self._conn

    def _start_writer(self) -> None:
        self._write_q = queue.Queue(maxsize=self._write_queue_max)
        self._writer_thread = threading.Thread(
            target=self._writer_loop, name="entity-writer", daemon=True)
        self._writer_thread.start()

    def _writer_loop(self) -> None:
        """Drain queued polls on a dedicated connection until the sentinel. Its own
        connection is created here so it is owned by this thread; a write failure is
        swallowed (capture must never be affected by the store). Every item is
        task_done'd even if the connection failed to open, so ``flush`` can't hang."""
        conn = None
        try:
            conn = sqlite3.connect(self._db_path)
            conn.row_factory = sqlite3.Row
            self._apply_pragmas(conn)
            self._writer_conn = conn
        except sqlite3.Error as exc:  # pragma: no cover - defensive
            logger.error("EntityStore writer connection failed — async writes "
                         "will be dropped: %s", exc)
        while True:
            item = self._write_q.get()
            try:
                if item is None:  # shutdown sentinel
                    break
                if conn is not None:
                    devices, gps_fix, now = item
                    self._write_poll(devices, gps_fix=gps_fix, now=now)
            except Exception as exc:  # pragma: no cover - defensive
                logger.debug("EntityStore async write failed: %s", exc)
            finally:
                self._write_q.task_done()
        if conn is not None:
            try:
                self.checkpoint_wal()  # truncate this connection's WAL before exit
                conn.close()
            except sqlite3.Error as exc:  # pragma: no cover - defensive
                logger.debug("EntityStore writer close error: %s", exc)

    def flush(self) -> None:
        """Block until the writer has drained the queue. A no-op in sync mode;
        used by tests and by shutdown to make queued writes durable."""
        if self._async_writes and self._write_q is not None:
            self._write_q.join()

    # ------------------------------------------------------------------
    # Schema
    # ------------------------------------------------------------------

    def _create_schema(self) -> None:
        cur = self._conn.cursor()
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS probe_evidence (
                mac        TEXT    NOT NULL,
                ssid       TEXT    NOT NULL,
                first_seen TEXT    NOT NULL,
                last_seen  TEXT    NOT NULL,
                probe_count INTEGER NOT NULL DEFAULT 1,
                PRIMARY KEY (mac, ssid)
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS device_fingerprint (
                mac               TEXT PRIMARY KEY,
                probe_fingerprint INTEGER,
                num_probed_ssids  INTEGER,
                first_seen        TEXT NOT NULL,
                last_seen         TEXT NOT NULL
            )
            """
        )
        # pnl_evidence — the preferred-network list accumulated per ROTATION-STABLE
        # anchor (the IE-set hash), not per MAC. A device's probed SSIDs accrue here
        # across its MAC rotations, so the full PNL ("former networks") survives
        # rotation instead of fragmenting into per-MAC rows like probe_evidence.
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS pnl_evidence (
                probe_fingerprint INTEGER NOT NULL,
                ssid        TEXT    NOT NULL,
                first_seen  TEXT    NOT NULL,
                last_seen   TEXT    NOT NULL,
                probe_count INTEGER NOT NULL DEFAULT 1,
                PRIMARY KEY (probe_fingerprint, ssid)
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS entities (
                entity_id   INTEGER PRIMARY KEY,
                entity_type TEXT    NOT NULL,
                identifier  TEXT    NOT NULL,
                first_seen  TEXT    NOT NULL,
                last_seen   TEXT    NOT NULL,
                obs_count   INTEGER NOT NULL DEFAULT 0,
                UNIQUE (entity_type, identifier)
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS observations (
                obs_id         INTEGER PRIMARY KEY,
                entity_id      INTEGER NOT NULL REFERENCES entities(entity_id),
                timestamp      TEXT    NOT NULL,
                lat            REAL,
                lon            REAL,
                pos_source     TEXT,
                pos_confidence REAL,
                signal         REAL
            )
            """
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_obs_entity ON observations(entity_id, timestamp)"
        )
        # Plain timestamp index for the retention sweep, which deletes across all
        # entities by age rather than per-entity.
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_obs_timestamp ON observations(timestamp)"
        )
        # Persisted contact-designator instance numbers (design-contact-designators).
        # Maps a device's rotation-stable identity key to a number that is sequential
        # within its CLASS-IDENT group and STABLE across rotations/restarts/sessions.
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS contact_designator (
                identity_key   TEXT PRIMARY KEY,
                group_key      TEXT NOT NULL,
                number         INTEGER NOT NULL,
                first_assigned TEXT
            )
            """
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_contact_group ON contact_designator(group_key)"
        )
        # AP beacon evidence — per beaconing AP + advertised SSID, with running RSSI
        # stats (Welford). Lets us confirm which networks actually exist HERE and how
        # strongly, so a client's probe for a locally-beaconed SSID is meaningful.
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS beacon_evidence (
                bssid        TEXT    NOT NULL,
                ssid         TEXT    NOT NULL,
                channel      INTEGER,
                crypt        INTEGER,
                first_seen   TEXT    NOT NULL,
                last_seen    TEXT    NOT NULL,
                beacon_count INTEGER NOT NULL DEFAULT 1,
                sig_n        INTEGER NOT NULL DEFAULT 0,
                sig_mean     REAL    NOT NULL DEFAULT 0.0,
                sig_m2       REAL    NOT NULL DEFAULT 0.0,
                PRIMARY KEY (bssid, ssid)
            )
            """
        )
        # Network affinity — keyed on the rotation-stable IE hash (like pnl_evidence):
        # a probed SSID that is ALSO being beaconed locally. The set of a device's
        # locally-confirmed networks is a robust, rotation-surviving cross-session
        # matcher; a shift in it is a fixed-mode deviation.
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS network_affinity (
                probe_fingerprint INTEGER NOT NULL,
                ssid        TEXT    NOT NULL,
                first_seen  TEXT    NOT NULL,
                last_seen   TEXT    NOT NULL,
                match_count INTEGER NOT NULL DEFAULT 1,
                PRIMARY KEY (probe_fingerprint, ssid)
            )
            """
        )
        # Cross-session contact registry (P4 phase B): the durable memory of a
        # rotation-stable CONTACT identity (the wifi-fp:/ble-fp: key from
        # device_identity.contact_identity) — when it was first/last seen, how many
        # distinct SESSIONS and distinct DAYS it has appeared in, and the last session
        # id. This is what lets a device seen on a prior day/restart be recognised as
        # a RETURNING ENTITY rather than a fresh contact ("has this been here before?").
        # Keyed on the contact identity, NOT the MAC, so it survives rotation.
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS contact_registry (
                identity_key  TEXT PRIMARY KEY,
                first_seen    TEXT    NOT NULL,
                last_seen     TEXT    NOT NULL,
                last_session  TEXT,
                last_day      TEXT    NOT NULL,
                visits        INTEGER NOT NULL DEFAULT 1,
                distinct_days INTEGER NOT NULL DEFAULT 1
            )
            """
        )
        # Cross-PHY co-presence links (P4 phase C): a pair of rotation-stable contact
        # identities observed travelling together (present in nearly the same polls) —
        # the two radios of one "person". Durable so a returning pair re-links fast.
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS contact_links (
                key_a        TEXT NOT NULL,
                key_b        TEXT NOT NULL,
                first_linked TEXT NOT NULL,
                last_seen    TEXT NOT NULL,
                PRIMARY KEY (key_a, key_b)
            )
            """
        )
        self._conn.commit()

    # ------------------------------------------------------------------
    # Write path — one call per poll, all writes for the poll in one commit
    # ------------------------------------------------------------------

    def record_poll(self, devices: list, *, gps_fix: Optional[dict] = None,
                    now: Optional[datetime] = None) -> None:
        """Persist one poll cycle. In sync mode (default) the write happens inline;
        with the off-loop writer enabled it is handed to the writer thread so a slow
        SD commit can't block the caller's (asyncio) loop. A full queue drops the
        poll rather than blocking capture — history is lossy under stress by design.

        With ENTITY_AUDIBLE_WINDOW_SECONDS set, devices that were not actually heard
        within that window of this poll are filtered out here — before the write (and
        before the async snapshot/queue), so silent devices cost nothing downstream."""
        now = now or datetime.now(timezone.utc)
        devices = self._audible_only(devices, now)
        if not self._async_writes:
            self._write_poll(devices, gps_fix=gps_fix, now=now)
            return
        try:
            # Snapshot the list so the caller can reuse it; device dicts are treated
            # as read-only downstream, so a shallow copy is enough.
            self._write_q.put_nowait((list(devices), gps_fix, now))
        except queue.Full:
            self._write_drops += 1
            if self._write_drops % 100 == 1:
                logger.warning(
                    "EntityStore write queue full — dropping poll (%d dropped total); "
                    "the writer isn't keeping up with storage", self._write_drops)

    def _audible_only(self, devices: list, now: datetime) -> list:
        """Drop devices whose Kismet ``last_time`` is older than the audible window.
        Devices with a missing/zero ``last_time`` are kept (fail open). Returns the
        input list unchanged when the filter is disabled."""
        if self._audible_window_s <= 0:
            return devices
        cutoff = now.timestamp() - self._audible_window_s
        return [d for d in devices
                if not (0 < (d.get("last_time") or 0) < cutoff)]

    def _write_poll(self, devices: list, *, gps_fix: Optional[dict] = None,
                    now: Optional[datetime] = None) -> None:
        """Do the actual per-poll DB work: per device, upsert probe evidence /
        fingerprint / entity and insert one observation row. Runs on the caller's
        thread (sync mode) or the writer thread (async); ``_active_conn`` picks the
        matching connection. The node's own GPS fix is the position for every device
        this poll; all four position fields are null when there is no fix."""
        now = now or datetime.now(timezone.utc)
        ts = _iso(now)
        if gps_fix:
            lat, lon = gps_fix.get("lat"), gps_fix.get("lon")
            pos_source, pos_confidence = "gps_node", 1.0
        else:
            lat = lon = pos_source = pos_confidence = None

        conn = self._active_conn()
        cur = conn.cursor()

        # First pass: record beaconing APs and collect the SSIDs being beaconed
        # locally THIS poll, so a client's probe for one of them counts as a network
        # confirmed to exist here (network affinity, below).
        beaconed_ssids = set()
        if _BEACON_CAPTURE_ENABLED:
            for device in devices:
                ssid_b = (device.get("beaconed_ssid") or "").strip()
                bssid = device.get("macaddr", "")
                if ssid_b and bssid:
                    beaconed_ssids.add(ssid_b)
                    self._upsert_beacon(cur, bssid, ssid_b, device, ts)

        for device in devices:
            mac = device.get("macaddr", "")
            if not mac:
                continue

            # device_fingerprint — one row per device (covers wildcard-only ones).
            cur.execute(
                "INSERT INTO device_fingerprint "
                "(mac, probe_fingerprint, num_probed_ssids, first_seen, last_seen) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(mac) DO UPDATE SET "
                "  last_seen = excluded.last_seen, "
                "  probe_fingerprint = excluded.probe_fingerprint, "
                "  num_probed_ssids = excluded.num_probed_ssids",
                (mac, device.get("probe_fingerprint"),
                 device.get("num_probed_ssids", 0), ts, ts),
            )

            # probe_evidence — one row per NAMED ssid; wildcard/blank excluded.
            probe_fp = device.get("probe_fingerprint")
            for ssid in device.get("probe_ssids", []) or []:
                if not isinstance(ssid, str) or not ssid.strip():
                    continue
                cur.execute(
                    "INSERT INTO probe_evidence "
                    "(mac, ssid, first_seen, last_seen, probe_count) "
                    "VALUES (?, ?, ?, ?, 1) "
                    "ON CONFLICT(mac, ssid) DO UPDATE SET "
                    "  last_seen = excluded.last_seen, "
                    "  probe_count = probe_count + 1",
                    (mac, ssid, ts, ts),
                )
                # Accumulate the PNL under the rotation-stable IE hash so it survives
                # MAC rotation. Only when the device has an IE fingerprint to key on.
                if probe_fp:
                    cur.execute(
                        "INSERT INTO pnl_evidence "
                        "(probe_fingerprint, ssid, first_seen, last_seen, probe_count) "
                        "VALUES (?, ?, ?, ?, 1) "
                        "ON CONFLICT(probe_fingerprint, ssid) DO UPDATE SET "
                        "  last_seen = excluded.last_seen, "
                        "  probe_count = probe_count + 1",
                        (probe_fp, ssid, ts, ts),
                    )
                    # Network affinity — this probed SSID is being beaconed locally,
                    # so it's a network confirmed to exist here. Keyed on the IE hash
                    # so it survives MAC rotation.
                    if ssid in beaconed_ssids:
                        cur.execute(
                            "INSERT INTO network_affinity "
                            "(probe_fingerprint, ssid, first_seen, last_seen, match_count) "
                            "VALUES (?, ?, ?, ?, 1) "
                            "ON CONFLICT(probe_fingerprint, ssid) DO UPDATE SET "
                            "  last_seen = excluded.last_seen, "
                            "  match_count = match_count + 1",
                            (probe_fp, ssid, ts, ts),
                        )

            # entities — upsert the wifi entity, then read its id for the FK.
            cur.execute(
                "INSERT INTO entities "
                "(entity_type, identifier, first_seen, last_seen, obs_count) "
                "VALUES ('wifi', ?, ?, ?, 1) "
                "ON CONFLICT(entity_type, identifier) DO UPDATE SET "
                "  last_seen = excluded.last_seen, "
                "  obs_count = obs_count + 1",
                (mac, ts, ts),
            )
            entity_id = cur.execute(
                "SELECT entity_id FROM entities WHERE entity_type = 'wifi' AND identifier = ?",
                (mac,),
            ).fetchone()[0]

            # observations — history; this is the only table that grows per poll.
            cur.execute(
                "INSERT INTO observations "
                "(entity_id, timestamp, lat, lon, pos_source, pos_confidence, signal) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (entity_id, ts, lat, lon, pos_source, pos_confidence,
                 device.get("last_signal")),
            )

        conn.commit()
        self._maybe_prune(now)
        self._maybe_checkpoint(now)

    def _upsert_beacon(self, cur, bssid: str, ssid: str, device: dict, ts: str) -> None:
        """Upsert one beaconing AP + advertised SSID, folding its RSSI into a running
        Welford mean/variance (zero/None readings skipped, like baseline_store)."""
        signal = device.get("last_signal")
        row = cur.execute(
            "SELECT sig_n, sig_mean, sig_m2 FROM beacon_evidence WHERE bssid = ? AND ssid = ?",
            (bssid, ssid),
        ).fetchone()
        sig_n, sig_mean, sig_m2 = row if row else (0, 0.0, 0.0)
        if signal is not None and signal != 0:
            sig_n += 1
            delta = signal - sig_mean
            sig_mean += delta / sig_n
            sig_m2 += delta * (signal - sig_mean)
        cur.execute(
            "INSERT INTO beacon_evidence "
            "(bssid, ssid, channel, crypt, first_seen, last_seen, beacon_count, sig_n, sig_mean, sig_m2) "
            "VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, ?) "
            "ON CONFLICT(bssid, ssid) DO UPDATE SET "
            "  last_seen = excluded.last_seen, "
            "  channel = excluded.channel, "
            "  crypt = excluded.crypt, "
            "  beacon_count = beacon_count + 1, "
            "  sig_n = excluded.sig_n, "
            "  sig_mean = excluded.sig_mean, "
            "  sig_m2 = excluded.sig_m2",
            (bssid, ssid, device.get("beacon_channel"), device.get("beacon_crypt"),
             ts, ts, sig_n, sig_mean, sig_m2),
        )

    # ------------------------------------------------------------------
    # Observation history retention
    # ------------------------------------------------------------------

    def prune_observations(self, now: Optional[datetime] = None) -> int:
        """Delete observation rows past the retention window OR beyond the row
        cap, in bounded batches, and return the number removed this sweep.

        Two independent limits, both enforced here so neither can let the table
        run away:

        * **Age** (``ENTITY_OBSERVATION_RETENTION_DAYS``, 0 = keep forever) —
          rows older than the window. Timestamps are uniform UTC ISO strings, so
          a string comparison against the cutoff is correct.
        * **Size** (``ENTITY_OBSERVATION_MAX_ROWS``, 0 = uncapped) — the oldest
          rows beyond the newest N. The age limit alone deletes nothing until a
          row crosses the window, so on a busy node the file grows unchecked for
          the whole window first; the cap gives a near-term plateau. Cheap to
          target: observation rowids are monotonic (append-only insert), so the
          newest N are ``rowid > MAX(rowid) - N`` with no COUNT scan.

        The delete is batched (``ENTITY_PRUNE_BATCH_ROWS`` per statement) under a
        shared wall-clock budget (``ENTITY_PRUNE_TIME_BUDGET_S``), committed per
        batch, because this runs on the asyncio poll thread: one unbounded DELETE
        over a large backlog held the loop past systemd's watchdog and
        crash-looped the node (2026-06, ~28M-row table) — and, as a single
        transaction, it rolled back on every kill, so no restart ever made
        progress. Batching caps the stall, and per-batch commits make each
        sweep's progress durable; a backlog larger than one budget drains across
        successive sweeps.
        """
        now = now or datetime.now(timezone.utc)
        deadline = time.monotonic() + self._prune_budget_s
        total = 0

        # Age-based: delete rows older than the retention window.
        if self._retention_days > 0:
            cutoff = _iso(now - timedelta(days=self._retention_days))
            total += self._delete_batched(
                "SELECT rowid FROM observations WHERE timestamp < ? LIMIT ?",
                (cutoff,), deadline, reason="age")

        # Size-based: delete the oldest rows beyond the newest _max_obs_rows.
        if self._max_obs_rows > 0 and time.monotonic() < deadline:
            top = self._active_conn().execute(
                "SELECT MAX(rowid) FROM observations").fetchone()[0]
            if top is not None:
                floor_rowid = top - self._max_obs_rows
                if floor_rowid > 0:
                    total += self._delete_batched(
                        "SELECT rowid FROM observations WHERE rowid <= ? LIMIT ?",
                        (floor_rowid,), deadline, reason="cap")

        if total:
            self._reclaim_pages()
            logger.info(
                "EntityStore pruned %d observation(s) (retention=%dd, cap=%s rows)",
                total, self._retention_days, self._max_obs_rows or "off")
        return total

    def _delete_batched(self, select_sql: str, params: tuple,
                        deadline: float, *, reason: str) -> int:
        """Delete the rows a SELECT picks, in ``_prune_batch_rows`` batches under a
        shared time budget. ``select_sql`` must end in ``LIMIT ?`` — the batch size
        is appended. Committed per batch so progress survives a mid-sweep kill. The
        budget is checked *after* each batch, so a sweep always makes at least one
        batch of progress even when it starts already over budget."""
        total = 0
        conn = self._active_conn()
        while True:
            cur = conn.execute(
                f"DELETE FROM observations WHERE rowid IN ({select_sql})",
                (*params, self._prune_batch_rows),
            )
            conn.commit()
            n = cur.rowcount or 0
            total += n
            if n < self._prune_batch_rows:
                break  # backlog for this limit cleared
            if time.monotonic() >= deadline:
                logger.info(
                    "EntityStore prune: time budget (%.1fs) hit during %s sweep "
                    "after %d row(s) — remaining backlog resumes next sweep",
                    self._prune_budget_s, reason, total)
                break
        return total

    def checkpoint_wal(self) -> None:
        """Fold the WAL back into the DB and truncate it to zero. WAL mode only
        auto-checkpoints in PASSIVE mode, which a busy writer on slow storage
        starves indefinitely, letting the WAL grow without bound (~1 GB observed,
        stalling every open). TRUNCATE takes the write lock and resets the file.
        Guarded and bounded: on a healthy cadence the WAL is small, so this is
        cheap; a checkpoint failure must never disturb capture."""
        try:
            self._active_conn().execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchall()
        except sqlite3.Error as exc:  # pragma: no cover - defensive
            logger.debug("EntityStore wal_checkpoint failed: %s", exc)

    def _maybe_checkpoint(self, now: datetime) -> None:
        """TRUNCATE-checkpoint the WAL at most once per checkpoint interval. Like
        the prune sweep, the first eligible run is deferred one interval so it
        never lands in the busy startup window."""
        if self._wal_checkpoint_s <= 0:
            return
        if self._last_wal_checkpoint is None:
            self._last_wal_checkpoint = now
            return
        if (now - self._last_wal_checkpoint).total_seconds() < self._wal_checkpoint_s:
            return
        self._last_wal_checkpoint = now
        self.checkpoint_wal()

    def storage_stats(self) -> dict:
        """Cheap on-disk footprint for the health banner / a size guard: DB and
        WAL bytes, plus an O(1) observation-row estimate (rowid span — exact for
        an append-only table, an over-estimate only if rowids were ever reused).
        All best-effort; a failure yields zeros rather than raising."""
        stats = {"db_bytes": 0, "wal_bytes": 0, "observation_rows": 0}
        try:
            if self._db_path != ":memory:":
                stats["db_bytes"] = os.path.getsize(self._db_path)
                wal = self._db_path + "-wal"
                if os.path.exists(wal):
                    stats["wal_bytes"] = os.path.getsize(wal)
        except OSError:  # pragma: no cover - defensive
            pass
        try:
            row = self._conn.execute(
                "SELECT MIN(rowid), MAX(rowid) FROM observations").fetchone()
            if row and row[0] is not None:
                stats["observation_rows"] = row[1] - row[0] + 1
        except sqlite3.Error:  # pragma: no cover - defensive
            pass
        return stats

    def _reclaim_pages(self) -> None:
        """Hand a bounded number of freed pages back to the filesystem. A no-op
        unless the database file was created with auto_vacuum enabled (see
        _apply_pragmas); bounded so it can never become its own stall."""
        try:
            conn = self._active_conn()
            # fetchall() matters: the pragma frees pages as the statement is
            # stepped, so an un-drained cursor reclaims nothing.
            conn.execute("PRAGMA incremental_vacuum(2000)").fetchall()
            conn.commit()
        except sqlite3.Error as exc:  # pragma: no cover - defensive
            logger.debug("EntityStore incremental_vacuum failed: %s", exc)

    def _maybe_prune(self, now: datetime) -> None:
        """Run the retention sweep at most once per prune interval. Guarded so a
        prune failure never disturbs the write path that just committed.

        The first eligible sweep after construction is deferred by one
        interval: sweeping on the first poll after a restart is what turned a
        big backlog into a startup crash-loop (delete → watchdog kill →
        restart → delete …), and startup is when the loop is busiest anyway.
        The batching above bounds the sweep regardless; this keeps it out of
        the startup window entirely.
        """
        if self._retention_days <= 0:
            return
        if self._last_prune is None:
            self._last_prune = now
            return
        if (now - self._last_prune).total_seconds() < self._prune_interval_s:
            return
        self._last_prune = now
        try:
            self.prune_observations(now)
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug("EntityStore prune error: %s", exc)

    # ------------------------------------------------------------------
    # Read helpers (tests / inspection)
    # ------------------------------------------------------------------

    def count(self, table: str) -> int:
        if table not in ("probe_evidence", "device_fingerprint", "entities",
                         "observations", "contact_designator", "pnl_evidence",
                         "beacon_evidence", "network_affinity", "contact_registry",
                         "contact_links"):
            raise ValueError(f"unknown table {table!r}")
        return self._conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]

    def record_contact_link(self, key_a: str, key_b: str, now: datetime) -> None:
        """Persist (or refresh) a co-presence link between two contact identities
        (P4-C). Keys are stored order-independently so a pair has one durable row,
        letting a returning person re-link immediately across sessions."""
        a, b = (key_a, key_b) if key_a <= key_b else (key_b, key_a)
        self._conn.execute(
            "INSERT INTO contact_links (key_a, key_b, first_linked, last_seen) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(key_a, key_b) DO UPDATE SET last_seen = excluded.last_seen",
            (a, b, _iso(now), _iso(now)),
        )
        self._conn.commit()

    def known_links(self) -> list:
        """All durable contact links as ``[(key_a, key_b), ...]`` — loaded at startup
        so a person seen on a prior session/day re-links fast."""
        return [(r["key_a"], r["key_b"]) for r in self._conn.execute(
            "SELECT key_a, key_b FROM contact_links")]

    def record_contact_sighting(self, identity_key: str, now: datetime,
                                session_id: str) -> dict:
        """Record that CONTACT ``identity_key`` was seen ``now`` in ``session_id`` and
        return its state as it was BEFORE this sighting (cross-session memory, P4-B).

        The returned dict describes the prior record so the caller can decide whether
        this is a returning entity::

            {"known": bool,                 # was this contact in the registry already?
             "prior_last_seen": datetime|None,
             "visits": int,                 # distinct sessions before this one
             "distinct_days": int,          # distinct UTC days before this one
             "last_session": str|None}      # the session it was last seen in

        A ``visits``/``distinct_days`` only advances when the session / UTC day changes,
        so many sightings within one run don't inflate the counts. Called once per new
        contact per session by the orchestrator, so write volume is low. Never called
        for a ``mac:`` identity (those rotate and can't be tracked across sessions).
        """
        today = now.astimezone(timezone.utc).date().isoformat()
        row = self._conn.execute(
            "SELECT first_seen, last_seen, last_session, last_day, visits, distinct_days "
            "FROM contact_registry WHERE identity_key = ?", (identity_key,),
        ).fetchone()
        if row is None:
            self._conn.execute(
                "INSERT INTO contact_registry "
                "(identity_key, first_seen, last_seen, last_session, last_day, visits, distinct_days) "
                "VALUES (?, ?, ?, ?, ?, 1, 1)",
                (identity_key, _iso(now), _iso(now), session_id, today),
            )
            self._conn.commit()
            return {"known": False, "prior_last_seen": None, "visits": 0,
                    "distinct_days": 0, "last_session": None}
        prior = {
            "known": True,
            "prior_last_seen": _parse_iso(row["last_seen"]),
            "visits": int(row["visits"]),
            "distinct_days": int(row["distinct_days"]),
            "last_session": row["last_session"],
        }
        new_visits = row["visits"] + (1 if session_id != row["last_session"] else 0)
        new_days = row["distinct_days"] + (1 if today != row["last_day"] else 0)
        self._conn.execute(
            "UPDATE contact_registry SET last_seen = ?, last_session = ?, last_day = ?, "
            "visits = ?, distinct_days = ? WHERE identity_key = ?",
            (_iso(now), session_id, today, new_visits, new_days, identity_key),
        )
        self._conn.commit()
        return prior

    def contact_registry_row(self, identity_key: str):
        """Raw registry row for a contact identity, or None (tests / inspection)."""
        return self._conn.execute(
            "SELECT * FROM contact_registry WHERE identity_key = ?", (identity_key,)
        ).fetchone()

    def accumulated_pnl(self, probe_fingerprint) -> list:
        """The preferred-network list (named SSIDs) accumulated under this IE-set
        hash across MAC rotations, most-probed first. Empty when the fingerprint is
        falsy or unseen. Read-only — safe from the poll thread."""
        if not probe_fingerprint:
            return []
        rows = self._conn.execute(
            "SELECT ssid FROM pnl_evidence WHERE probe_fingerprint = ? "
            "ORDER BY probe_count DESC, ssid ASC", (probe_fingerprint,),
        ).fetchall()
        return [r["ssid"] for r in rows]

    def network_affinity_profile(self, probe_fingerprint) -> dict:
        """Locally-confirmed networks for this IE-set hash → match_count (the times a
        probed SSID coincided with a local beacon), most-confirmed first. Empty when
        the fingerprint is falsy or unseen. Read-only — safe from the poll thread."""
        if not probe_fingerprint:
            return {}
        rows = self._conn.execute(
            "SELECT ssid, match_count FROM network_affinity WHERE probe_fingerprint = ? "
            "ORDER BY match_count DESC, ssid ASC", (probe_fingerprint,),
        ).fetchall()
        return {r["ssid"]: r["match_count"] for r in rows}

    def beacon_rssi(self, bssid: str, ssid: str):
        """Running RSSI stats (mean, variance, sample count) for one beaconing AP +
        SSID, or None if unseen. Variance is population variance (m2 / n)."""
        row = self._conn.execute(
            "SELECT sig_n, sig_mean, sig_m2 FROM beacon_evidence WHERE bssid = ? AND ssid = ?",
            (bssid, ssid),
        ).fetchone()
        if not row or not row["sig_n"]:
            return None
        n = row["sig_n"]
        return {"mean": row["sig_mean"], "var": row["sig_m2"] / n, "count": n}

    def distinctive_anchors(self, max_df: int = 3) -> dict:
        """Map each IE-set hash → its rarest *distinctive* probed SSID, or omit it.

        Distinctiveness is cross-device rarity: an SSID's document frequency (df) is
        the number of distinct IE hashes that probe it. A popular public SSID
        (``xfinitywifi``) has a high df and almost no discriminating power; a home/
        private SSID has df≈1 and near-uniquely anchors a device. For each IE hash we
        return its lowest-df SSID, but only if that df ``<= max_df`` — so an IE hash
        whose whole PNL is common public networks gets NO anchor (the caller leaves
        it ``mac:``-keyed, un-trackable, which is over-merge-safe). The rarest SSID is
        stable (a device's home network doesn't churn), so the resulting key is both
        distinctive and rotation-stable. Read-only; built in one pass for the poll
        loop to cache."""
        df = {}
        for r in self._conn.execute(
            "SELECT ssid, COUNT(DISTINCT probe_fingerprint) AS d FROM pnl_evidence "
            "GROUP BY ssid"
        ):
            df[r["ssid"]] = r["d"]
        best = {}   # probe_fingerprint -> (df, ssid)
        for r in self._conn.execute("SELECT probe_fingerprint, ssid FROM pnl_evidence"):
            fp, ssid = r["probe_fingerprint"], r["ssid"]
            d = df.get(ssid, 1)
            if d > max_df:
                continue
            cur = best.get(fp)
            # lowest df wins; ssid as a stable tie-break
            if cur is None or (d, ssid) < cur:
                best[fp] = (d, ssid)
        return {fp: v[1] for fp, v in best.items()}

    def assign_contact_number(self, identity_key: str, group_key: str,
                              now: Optional[datetime] = None) -> int:
        """Return this identity's stable instance number within ``group_key``.

        Returns the existing number if the identity has one; otherwise assigns
        ``max(number in group) + 1`` and persists it, so a device keeps the same
        contact designator across rotations, restarts, and sessions. Called only
        from the poll (asyncio) thread.
        """
        row = self._conn.execute(
            "SELECT number FROM contact_designator WHERE identity_key = ?",
            (identity_key,),
        ).fetchone()
        if row is not None:
            return row["number"]
        nxt = self._conn.execute(
            "SELECT COALESCE(MAX(number), 0) + 1 AS n FROM contact_designator "
            "WHERE group_key = ?", (group_key,),
        ).fetchone()["n"]
        ts = (now or datetime.now(timezone.utc)).isoformat()
        self._conn.execute(
            "INSERT INTO contact_designator (identity_key, group_key, number, first_assigned) "
            "VALUES (?, ?, ?, ?)", (identity_key, group_key, nxt, ts),
        )
        self._conn.commit()
        return nxt

    def probe_evidence_row(self, mac: str, ssid: str):
        return self._conn.execute(
            "SELECT * FROM probe_evidence WHERE mac = ? AND ssid = ?", (mac, ssid)
        ).fetchone()

    def device_fingerprint_row(self, mac: str):
        return self._conn.execute(
            "SELECT * FROM device_fingerprint WHERE mac = ?", (mac,)
        ).fetchone()

    def entity_row(self, identifier: str, entity_type: str = "wifi"):
        return self._conn.execute(
            "SELECT * FROM entities WHERE entity_type = ? AND identifier = ?",
            (entity_type, identifier),
        ).fetchone()

    def close(self) -> None:
        # Drain and stop the writer thread first (async mode) so queued polls are
        # durable and its connection is checkpointed + closed on its own thread.
        if self._async_writes and self._writer_thread is not None:
            try:
                self.flush()
                self._write_q.put(None)          # shutdown sentinel
                self._writer_thread.join(timeout=10)
            except Exception as exc:  # pragma: no cover - defensive
                logger.debug("EntityStore writer shutdown error: %s", exc)
        # Truncate the WAL on the way out so a large one can't persist to the next
        # start, where replaying it stalls the first open (the 2026-07 failure).
        self.checkpoint_wal()
        try:
            self._conn.close()
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug("EntityStore close error: %s", exc)
