'''SensorOrchestrator — polling loops and health tracking for Passive Vigilance.

Owns all sensor poll loops, health state, reconnection logic, and accumulated
session event lists. PassiveVigilance constructs this and delegates all
polling/health concerns to it.
'''

import asyncio
import json
import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from math import atan2, cos, radians, sin, sqrt
from pathlib import Path
from typing import Optional

from modules import air_geometry, air_scoring, contact_designator, device_identity
from modules.mac_utils import get_mac_type, is_randomized_mac, normalize_mac
from modules.sdr_manager import SDRMode
from modules.wifi_fingerprint import compute_pnl_fingerprint


def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    '''Great-circle distance in metres between two GPS coordinates.'''
    R = 6_371_000.0
    p1, p2 = radians(lat1), radians(lat2)
    dphi, dlmb = radians(lat2 - lat1), radians(lon2 - lon1)
    a = sin(dphi / 2) ** 2 + cos(p1) * cos(p2) * sin(dlmb / 2) ** 2
    return R * 2 * atan2(sqrt(a), sqrt(1.0 - a))


@dataclass
class CollectedEvents:
    '''Named container for all session event lists returned at shutdown.'''
    all_events: list = field(default_factory=list)
    aircraft_detections: list = field(default_factory=list)
    drone_detections: list = field(default_factory=list)
    remote_id_detections: list = field(default_factory=list)


logger = logging.getLogger(__name__)

# Cap on the in-memory raw ACARS feed (the durable record is acars.jsonl; the GUI
# cache is bounded separately). Keeps a busy datalink environment from growing it.
_ACARS_FEED_MAX = 500


class SensorOrchestrator:
    '''Owns all sensor polling loops, health tracking, and session event accumulation.

    Constructed by PassiveVigilance with explicit references to every module it
    needs. Exposes collected_events so PassiveVigilance can read session data
    during shutdown without coupling via global state.
    '''

    def __init__(
        self, *,
        gps,
        kismet,
        adsb,
        drone_rf,
        sdr_coordinator,
        alert_backend,
        rate_limiter,
        persistence,
        probe_analyzer,
        gui_server,
        entity_store=None,
        remote_id=None,
        ble_scanner=None,
        ais=None,
        acars=None,
        aircraft_registry=None,
        session_id: str,
        session_start: datetime,
        session_dir: Path,
        sdr_mode,
        node_mode: str = "",
        stop_event: asyncio.Event,
        gps_poll_interval: int,
        adsb_poll_interval: int,
        kismet_poll_interval: int,
        drone_poll_interval: int,
        remote_id_poll_interval: int = 5,
        health_banner_interval: int,
        max_reconnect_attempts: int,
        reconnect_interval: int,
        modules_active: dict,
    ) -> None:
        self.gps = gps
        self.kismet = kismet
        self.adsb = adsb
        self.drone_rf = drone_rf
        self.sdr_coordinator = sdr_coordinator
        self.alert_backend = alert_backend
        self.rate_limiter = rate_limiter
        self.persistence = persistence
        self.probe_analyzer = probe_analyzer
        self.gui_server = gui_server
        # Durable entity/observation store (Phase A). Recording is orthogonal to
        # scoring strategy, so it runs here at the poll site for EVERY NODE_MODE,
        # not inside any ScoringEngine. May be None (recording disabled).
        self.entity_store = entity_store
        self.remote_id = remote_id
        # Passive BLE advertisement scanner (owns hci0). Optional — None unless
        # BLE_SCANNER_ENABLED. It captures adverts continuously off the asyncio
        # loop; we buffer the latest advert per address and flush the buffer into
        # the device list each Kismet poll, so BLE flows through the same
        # entity/scoring/GUI path as WiFi (it carries advert fields the unified
        # fingerprint uses). Replaces Kismet's empty linuxbluetooth feed.
        self.ble_scanner = ble_scanner
        self._ble_adverts: dict = {}
        if ble_scanner is not None:
            ble_scanner.on_advert = self._on_ble_advert
        # Optional AIS (marine VHF) capture — an SDR band like DroneRF/ADS-B. None
        # unless AIS_ENABLED. Consumes an AIS-catcher JSON feed; off by default.
        self.ais = ais
        # Optional ACARS (aviation VHF datalink) capture + the connectivity-adaptive
        # ICAO→registration registry used to correlate a decoded ACARS message back to
        # a live ADS-B contact. Both None unless ACARS_ENABLED.
        self.acars = acars
        self.aircraft_registry = aircraft_registry

        self.session_id = session_id
        self.session_start = session_start
        self._session_dir = session_dir
        self.sdr_mode = sdr_mode
        # Resolved NODE_MODE ("fixed"/"mobile"); gates the mobile-only nearby feed.
        self._node_mode = node_mode
        self._stop_event = stop_event

        self._gps_poll_interval = gps_poll_interval
        self._adsb_poll_interval = adsb_poll_interval
        self._kismet_poll_interval = kismet_poll_interval
        self._drone_poll_interval = drone_poll_interval
        self._ais_poll_interval = int(os.getenv("AIS_POLL_INTERVAL_SECONDS", "10"))
        # AIS is VHF (line-of-sight, realistically <~100 km). A positioned vessel
        # reported far beyond range when we have a GPS fix is physically impossible to
        # have received directly — a misdecode (noise that passed CRC). Drop those.
        self._ais_max_range_km = float(os.getenv("AIS_MAX_RANGE_KM", "100"))
        self._acars_poll_interval = int(os.getenv("ACARS_POLL_INTERVAL_SECONDS", "5"))
        self._acars_enabled = acars is not None
        # Online enrichment is opt-in via the API key; without it, registration
        # resolution stays fully offline (no outbound queries) — opsec default.
        self._adsb_enrich_enabled = bool(os.getenv("ADSBXLOL_API_KEY", "").strip())
        # ACARS preemption: when an ADS-B contact is continuously held past the
        # trigger, request a bounded ACARS window from the SDR coordinator (single
        # dongle). On a dedicated VHF dongle ACARS runs continuously (no trigger).
        self._acars_trigger_s = float(os.getenv("ACARS_TRIGGER_SECONDS", "30"))
        self._acars_window_s = float(os.getenv("ACARS_WINDOW_SECONDS", "25"))
        self._remote_id_poll_interval = remote_id_poll_interval
        self._health_banner_interval = health_banner_interval
        self._max_reconnect_attempts = max_reconnect_attempts
        self._reconnect_interval = reconnect_interval
        self._modules_active = modules_active

        self._sensor_health: dict[str, bool] = {
            "gps": True, "kismet": True, "adsb": True, "drone_rf": True, "sdr": True,
            "remote_id": True, "ais": True, "acars": True,
        }
        self._degraded_log_counter: dict[str, int] = {
            "gps": 0, "kismet": 0, "adsb": 0, "drone_rf": 0, "sdr": 0, "remote_id": 0,
            "ais": 0, "acars": 0,
        }
        # Watchdog: monotonic timestamp of each sensor's last COMPLETED poll. The
        # exception-based health flips above only catch a poll that *raises*; a
        # poll loop that goes silent (a hung await, a dead task) leaves health
        # showing ✓ while nothing is captured. The watchdog flips a sensor
        # degraded when its loop stops completing — liveness is the loop running,
        # not data volume, so an idle-but-healthy sensor is never flagged.
        self._last_poll_ts: dict[str, float] = {}
        self._watchdog_stall_s = float(os.getenv("SENSOR_STALL_SECONDS", "180"))
        # Data-progress watchdog: a poll loop that keeps *completing* while its
        # upstream source is dead (frozen cumulative counter, no exception) is the
        # liveness check's blind spot. For data-bearing sensors we also track the
        # monotonic time their cumulative stat last advanced, and trip when it has
        # been frozen longer than SENSOR_DATA_STALL_SECONDS. Kismet re-reports its
        # active device set every poll so its counter climbs continuously; an exact
        # multi-minute freeze is unambiguous dead-capture. ADS-B is off by default
        # (an empty sky is a legitimate flat counter).
        self._data_stall_s = float(os.getenv("SENSOR_DATA_STALL_SECONDS", "600"))
        self._watchdog_interval_s = float(os.getenv("WATCHDOG_INTERVAL_SECONDS", "30"))
        _data_sensors = os.getenv("WATCHDOG_DATA_SENSORS", "kismet")
        self._data_sensors: set[str] = {
            s.strip() for s in _data_sensors.split(",") if s.strip()
        }
        # Which cumulative stat key signals progress for each data sensor.
        self._data_stat_key: dict[str, str] = {
            "kismet": "kismet_devices_seen",
            "adsb": "aircraft_seen",
        }
        # Last observed counter value and the monotonic time it last advanced.
        self._last_progress_value: dict[str, int] = {}
        self._last_progress_ts: dict[str, float] = {}
        # Sensors that have already tripped a data/loop stall this episode, so a
        # re-trip after a reconnect (within the window) escalates to self-restart.
        self._stalled_since_reconnect: set[str] = set()
        # Graceful-startup / radio-health alerting: the radios main.py expected to
        # bring up, and the ones we've already alerted as DOWN (debounce, so a
        # persistently-down radio alerts once, not every watchdog tick). A radio
        # that comes up disabled at startup, or drops mid-run (a USB unplug like the
        # 2026-06-20 incident), fires one operator alert here — the stall watchdog
        # only catches *running* loops that go silent, never a disabled sensor.
        self._expected_radios: set[str] = set()
        self._radio_down_alerted: set[str] = set()
        # Self-restart crash-guard (persisted, since os._exit wipes memory).
        self._max_restarts = int(os.getenv("WATCHDOG_MAX_RESTARTS", "5"))
        self._restart_window_s = float(os.getenv("WATCHDOG_RESTART_WINDOW_S", "1800"))
        _data_dir = Path(__file__).resolve().parent.parent / "data"
        self._restart_log_path = _data_dir / "watchdog_restarts.json"
        # sd_notify: writes to $NOTIFY_SOCKET when run under systemd Type=notify;
        # a no-op (empty string) on dev / non-systemd runs.
        self._notify_socket = os.getenv("NOTIFY_SOCKET", "")
        self._stats: dict[str, int] = {
            "kismet_devices_seen": 0,
            "aircraft_seen": 0,
            "drone_detections": 0,
            "ais_vessels_seen": 0,
            "ais_out_of_range_dropped": 0,
            "acars_messages_seen": 0,
            "acars_correlated": 0,
            "remote_id_detections": 0,
            "alerts_sent": 0,
            "alerts_rate_limited": 0,
            "alerts_dropped": 0,
            "alerts_below_threshold": 0,
            "persistent_detections": 0,
            "aircraft_idless_skipped": 0,
            "aircraft_returns": 0,
        }

        self.all_events: list[dict] = []
        # Index mac -> the event_dict already in all_events, so a device that
        # re-flags every poll updates one ongoing detection in place instead of
        # appending a new row. Bounds all_events / events.jsonl to distinct
        # devices (the post-freeze growth fix); the dict values ARE the list
        # elements, so all_events stays a plain list for the shutdown writers.
        self._wifi_event_index: dict[str, dict] = {}
        self.aircraft_detections: list[dict] = []
        # Index icao -> the aircraft event already in aircraft_detections, so a
        # plane re-seen every poll becomes ONE event accumulating a positions[]
        # track instead of hundreds of rows. Track points are distance/time
        # thinned so a slow/hovering target doesn't bloat the list.
        self._aircraft_index: dict[str, dict] = {}
        self._aircraft_track_min_m = float(os.getenv("AIRCRAFT_TRACK_MIN_METERS", "250"))
        self._aircraft_track_min_s = float(os.getenv("AIRCRAFT_TRACK_MIN_SECONDS", "15"))
        # How long an aircraft is retained in the panel/index after it was last
        # seen — this is the persistent detection LOG window (so the table survives a
        # refresh, like the WiFi/BT tab), bounded so the index doesn't grow on a
        # multi-day run. Default 24h: keeping a day of airframes lets a returning
        # ICAO be recognised as the SAME identity (and flagged of-interest) rather
        # than re-appearing as a fresh contact. The map is a separate, shorter
        # current-sky lens — the client fades/expires markers by recency
        # (AIRCRAFT_MAP_DECAY_MS) so the map shows what's overhead now.
        self._aircraft_retention_s = float(os.getenv("AIRCRAFT_RETENTION_SECONDS", "86400"))
        # A re-sighting after a gap longer than this counts as a RETURN: the same
        # airframe was absent then came back. We mark the track with a gap (so the
        # flight path doesn't draw a straight line across the absence) and flag the
        # contact as of-interest. Default 10 min — long enough to clear sparse-
        # reception poll gaps, short enough to catch a real depart-and-return.
        self._aircraft_return_gap_s = float(os.getenv("AIRCRAFT_RETURN_GAP_SECONDS", "600"))
        # Hard cap on a single track's point count. Thinning (above) keeps points
        # sparse, but an orbiter/loiterer seen for hours still grows its track
        # without bound — unbounded memory across a multi-day run, and the whole
        # track ships to the GUI on every push. Cap it (drop oldest); shared by the
        # ADS-B and Remote ID flight paths via _extend_track.
        self._track_max_points = int(os.getenv("AIRCRAFT_TRACK_MAX_POINTS", "500"))
        # P7 air-of-interest persistence scoring (transit≈0, loiter/return climbs).
        # Parameters resolved once; the scorer is pure (modules/air_scoring.py).
        self._air_params = air_scoring.AirParams.from_env(os.environ)
        # P7: a drone-RF band must be heard on this many sweeps before it alerts, so
        # a single fleeting blip is shown but not paged — sustained presence is the
        # signal. The detection is always logged/displayed regardless.
        self._drone_min_sweeps = int(os.getenv("DRONE_RF_MIN_SWEEPS", "2"))
        # Paging threshold: a WiFi/BT detection still shows in the panel at any
        # score, but only one scoring >= this pages (backend send + Alerts feed).
        # Default 0.7 (likely) — the 2026-06 post-freeze read drowned the operator in
        # ~50 suspicious (0.5) flags/poll vs 2 high; suspicious is display-only now.
        self._wifi_page_min_score = float(os.getenv("WIFI_ALERT_MIN_SCORE", "0.7"))
        self.drone_detections: list[dict] = []
        # Index freq-band -> the drone event already in drone_detections, so a
        # persistent emitter heard on every sweep becomes ONE event (refreshed
        # in place with the latest/peak power and a running count) instead of a
        # row per sweep. A fixed node doesn't move, so no geographic track.
        self._drone_index: dict[str, dict] = {}
        # AIS vessels — dedup by MMSI into one event per vessel (refreshed in place
        # with the latest position/name), mirroring the drone band's index. A vessel
        # carries its OWN position in the AIS message, so the map uses that.
        self.ais_detections: list[dict] = []
        self._ais_index: dict[str, dict] = {}
        # ACARS decoded messages (raw feed for the GUI); correlation stashes matched
        # messages onto the aircraft event under event["acars"].
        self.acars_detections: list[dict] = []
        self.remote_id_detections: list[dict] = []
        # Index UAS ID -> the Remote ID event already in remote_id_detections, so
        # a drone broadcasting every frame becomes ONE event accumulating a
        # positions[] flight path (the drone's own reported position, thinned)
        # instead of a row per frame.
        self._remote_id_index: dict[str, dict] = {}
        self._current_fix: Optional[dict] = None
        self._gps_fix_count: int = 0

        # Dedicated single-thread pool for blocking gpsd calls (get_fix / connect
        # / close). Defense in depth: if a gpsd read still wedges its worker
        # thread despite the per-read socket timeout, it can only starve this
        # one-thread pool — never the default executor shared by the SQLite
        # stores, SDR systemctl calls, and other blocking work.
        self._gps_executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="gps"
        )
        # Hard ceiling on awaiting any GPS executor dispatch, so even a fully
        # wedged worker thread can't stall the awaiting coroutine forever.
        self._gps_call_timeout = float(
            os.getenv("GPS_READ_TIMEOUT_SECONDS", "2.0")
        ) + 1.0

        # Alert dispatch runs OFF the event loop. Backends do synchronous network
        # I/O (requests with retries + a multi-second timeout); calling them inline
        # blocks every async task, including the watchdog heartbeat, so a slow or
        # misconfigured backend turns a detection flood into a systemd-watchdog kill
        # loop. A single worker keeps sends serial (the backends' rate limiters are
        # not thread-safe) and a bounded in-flight count drops rather than queues
        # unboundedly when a hung backend can't keep up.
        self._alert_executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="alert"
        )
        self._alert_max_inflight = int(os.getenv("ALERT_MAX_INFLIGHT", "32"))
        self._alerts_inflight = 0

    @property
    def collected_events(self) -> CollectedEvents:
        '''Return all session event lists as a CollectedEvents dataclass for shutdown use.'''
        return CollectedEvents(
            all_events=self.all_events,
            aircraft_detections=self.aircraft_detections,
            drone_detections=self.drone_detections,
            remote_id_detections=self.remote_id_detections,
        )

    # ------------------------------------------------------------------
    # Poll loops
    # ------------------------------------------------------------------

    async def _poll_gps_loop(self) -> None:
        '''Run GPS polling on a fixed interval until stop is signalled.'''
        while not self._stop_event.is_set():
            if self._modules_active.get("gps", False):
                await self._poll_gps()
                self._mark_poll("gps")
            try:
                await asyncio.sleep(self._gps_poll_interval)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error("GPS poll loop sleep error: %s", exc)

    async def _poll_adsb_loop(self) -> None:
        '''Run ADS-B polling on a fixed interval until stop is signalled.'''
        while not self._stop_event.is_set():
            if self._modules_active.get("adsb", False):
                await self._poll_adsb()
                self._mark_poll("adsb")
            try:
                await asyncio.sleep(self._adsb_poll_interval)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error("ADS-B poll loop sleep error: %s", exc)

    async def _poll_kismet_loop(self) -> None:
        '''Run Kismet polling on a fixed interval until stop is signalled.'''
        while not self._stop_event.is_set():
            if self._modules_active.get("kismet", False):
                await self._poll_kismet()
                self._mark_poll("kismet")
            try:
                await asyncio.sleep(self._kismet_poll_interval)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error("Kismet poll loop sleep error: %s", exc)

    async def _poll_drone_rf_loop(self) -> None:
        '''Run DroneRF polling on a fixed interval until stop is signalled.'''
        while not self._stop_event.is_set():
            if self._modules_active.get("drone_rf", False):
                await self._poll_drone_rf()
                self._mark_poll("drone_rf")
            try:
                await asyncio.sleep(self._drone_poll_interval)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error("Drone RF poll loop sleep error: %s", exc)

    async def _poll_ais_loop(self) -> None:
        '''Run AIS polling on a fixed interval until stop is signalled.'''
        while not self._stop_event.is_set():
            if self._modules_active.get("ais", False):
                await self._poll_ais()
                self._mark_poll("ais")
            try:
                await asyncio.sleep(self._ais_poll_interval)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error("AIS poll loop sleep error: %s", exc)

    async def _poll_acars_loop(self) -> None:
        '''Run ACARS polling on a fixed interval until stop is signalled.'''
        while not self._stop_event.is_set():
            if self._modules_active.get("acars", False):
                await self._poll_acars()
                self._mark_poll("acars")
            try:
                await asyncio.sleep(self._acars_poll_interval)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error("ACARS poll loop sleep error: %s", exc)

    async def _poll_remote_id_loop(self) -> None:
        '''Run Remote ID polling on a fixed interval until stop is signalled.'''
        while not self._stop_event.is_set():
            if self._modules_active.get("remote_id", False):
                await self._poll_remote_id()
                self._mark_poll("remote_id")
            try:
                await asyncio.sleep(self._remote_id_poll_interval)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error("Remote ID poll loop sleep error: %s", exc)

    async def _health_banner_loop(self) -> None:
        '''Emit a structured health banner every health_banner_interval seconds.'''
        while not self._stop_event.is_set():
            try:
                await asyncio.sleep(self._health_banner_interval)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error("Health banner loop sleep error: %s", exc)
            if not self._stop_event.is_set():
                self._log_health_banner()

    # ------------------------------------------------------------------
    # Rolling-baseline adaptation sweep (P3) — separate from entity-prune
    # ------------------------------------------------------------------

    def _adaptation_sweep_enabled(self) -> bool:
        """True only when a fixed engine opts in to rolling adaptation.

        Independently disablable from the entity-prune sweep: the env kill-switch
        ``ADAPTATION_SWEEP_ENABLED`` (default on) AND a non-``off`` posture on a
        fixed engine that supports the sweep. Posture ``off`` (the fail-safe) or a
        mobile engine starts no task at all.
        """
        if os.getenv("ADAPTATION_SWEEP_ENABLED", "true").strip().lower() == "false":
            return False
        if not callable(getattr(self.persistence, "run_adaptation_sweep", None)):
            return False
        posture = getattr(self.persistence, "_adaptation_posture", "off")
        # isinstance guard: a mocked/mobile engine exposes truthy attrs that are not
        # real posture strings — only a genuine fixed engine's string posture counts.
        return isinstance(posture, str) and posture != "off"

    async def _adaptation_sweep_loop(self) -> None:
        """Periodically promote sustained-presence devices into the baseline and
        demote long-absent promoted ones (P3).

        A SEPARATE task from the entity-prune sweep on purpose — different failure
        semantics (prune failure = disk; adaptation failure = scoring correctness)
        — so it is independently guarded and disablable. Any sweep failure is logged
        and swallowed; it never touches capture or scoring. The engine returns
        demotion events (it stays DB-only); this loop writes them to events.jsonl.
        """
        interval = float(os.getenv("ADAPTATION_SWEEP_INTERVAL_SECONDS", "3600"))
        while not self._stop_event.is_set():
            try:
                await asyncio.sleep(interval)
            except asyncio.CancelledError:
                raise
            if self._stop_event.is_set():
                break
            try:
                events = self.persistence.run_adaptation_sweep()
                for ev in events or []:
                    self._emit_demotion_event(ev)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error("Adaptation sweep error (scoring unaffected): %s", exc)

    def _emit_demotion_event(self, ev: dict) -> None:
        """Enrich a demotion event with its contact designator and append it to
        events.jsonl as a first-class ``baseline_demotion`` event. This is the P3
        producer; the "graveyard" GUI panel is a deferred consumer. Guarded so a
        write failure never disturbs the sweep."""
        try:
            record = dict(ev)
            record.setdefault("contact", self._demotion_contact(ev))
            self._append_jsonl(self._session_dir / "events.jsonl", record)
        except Exception as exc:
            logger.debug("demotion event emit error: %s", exc)

    def _demotion_contact(self, ev: dict) -> str:
        """Best-effort CLASS-IDENT-# contact for a demoted fingerprint, from the
        fields the store retained (device_type, manufacturer, fingerprint). The
        instance number is the persisted, rotation-stable assignment keyed by the
        fingerprint, so it matches what was shown while the device was live."""
        cls = contact_designator.class_token(ev.get("device_type", "") or "")
        ident = contact_designator.ident_token(
            manufacturer=ev.get("manufacturer", "") or "",
            fingerprint=ev.get("fingerprint", "") or "",
        )
        group = contact_designator.group_key(cls, ident)
        identity_key = ev.get("fingerprint") or ""
        number = self._assign_contact_number(identity_key, group)
        return contact_designator.designator(cls, ident, number)

    # ------------------------------------------------------------------
    # Inner poll methods
    # ------------------------------------------------------------------

    async def _run_gps_call(self, func):
        '''Run a blocking GPS callable on the dedicated GPS pool under a hard timeout.

        Uses the single-thread GPS executor (so a wedged read can't starve the
        default pool) and asyncio.wait_for (so even a wedged worker thread can't
        stall the awaiting coroutine). asyncio.TimeoutError surfaces to the
        caller's except path, which treats it like any other GPS failure.
        '''
        loop = asyncio.get_running_loop()
        return await asyncio.wait_for(
            loop.run_in_executor(self._gps_executor, func),
            timeout=self._gps_call_timeout,
        )

    async def _poll_gps(self) -> None:
        '''Read one GPS fix; update _current_fix and health state.'''
        try:
            fix = await self._run_gps_call(self.gps.get_fix)
        except Exception as exc:
            if self._sensor_health["gps"]:
                logger.warning("Sensor gps degraded: %s", exc)
                self._console_alert(f"Sensor gps degraded: {exc}")
                self._sensor_health["gps"] = False
                reconnected = await self._reconnect("gps")
                if not reconnected:
                    logger.error("GPS failed to reconnect — continuing with degraded state")
            else:
                self._degraded_log_counter["gps"] += 1
                if self._degraded_log_counter["gps"] % 10 == 0:
                    logger.warning("Sensor gps still degraded after %d consecutive failures", self._degraded_log_counter["gps"])
            return
        if not self._sensor_health["gps"]:
            logger.info("Sensor gps recovered")
            self._sensor_health["gps"] = True
            self._degraded_log_counter["gps"] = 0
        had_fix = self._current_fix is not None
        self._current_fix = fix
        if fix:
            self._gps_fix_count += 1
            if not had_fix:
                logger.info("GPS: fix acquired (%.4f, %.4f)", fix["lat"], fix["lon"])
        elif had_fix:
            logger.warning("GPS: fix lost")

    async def _poll_adsb(self) -> None:
        '''Poll readsb for aircraft; append to aircraft_detections and fire alerts.'''
        try:
            # GPS-stamp from the orchestrator's own fresh fix; the module no
            # longer reads the shared gpsd socket on the poll loop.
            aircraft_list = await self.adsb.poll_aircraft(gps_fix=self._current_fix)
        except Exception as exc:
            if self._sensor_health["adsb"]:
                logger.warning("Sensor adsb degraded: %s", exc)
                self._console_alert(f"Sensor adsb degraded: {exc}")
                self._sensor_health["adsb"] = False
                reconnected = await self._reconnect("adsb")
                if not reconnected:
                    logger.error("ADS-B failed to reconnect — continuing with degraded state")
            else:
                self._degraded_log_counter["adsb"] += 1
                if self._degraded_log_counter["adsb"] % 10 == 0:
                    logger.warning("Sensor adsb still degraded after %d consecutive failures", self._degraded_log_counter["adsb"])
            return
        if not self._sensor_health["adsb"]:
            logger.info("Sensor adsb recovered")
            self._sensor_health["adsb"] = True
            self._degraded_log_counter["adsb"] = 0
        now_iso = datetime.now(timezone.utc).isoformat()
        for aircraft in aircraft_list:
            self._stats["aircraft_seen"] += 1
            icao = aircraft.get("icao")
            if not icao:
                # No hex address — can't be correlated across sightings. Merging
                # every ID-less target under one "unknown" key made a single bogus
                # airframe that teleports around the sky, so skip it from the
                # per-ICAO log/track entirely.
                self._stats["aircraft_idless_skipped"] += 1
                logger.debug("ADS-B: skipping ID-less aircraft (no icao)")
                continue
            existing = self._aircraft_index.get(icao)
            if existing is not None:
                # Same plane — refresh current-state fields in place and extend
                # its track (thinned). One event per ICAO, not one per sighting.
                # A long gap since the last sighting means the airframe left and
                # came back: same identity, marked track gap, flagged of-interest.
                prior_gap = self._aircraft_age_seconds(existing, datetime.fromisoformat(now_iso))
                returned = prior_gap > self._aircraft_return_gap_s
                existing.update({**aircraft, "event_type": "aircraft", "timestamp": now_iso})
                if returned:
                    self._note_aircraft_return(existing, icao, prior_gap, now_iso)
                    # The contact left and came back — restart the continuous-hold
                    # clock and re-arm the one-shot ACARS trigger for this new segment.
                    existing["segment_start"] = now_iso
                    existing["_acars_fired"] = False
                moved = self._extend_aircraft_track(existing, aircraft, now_iso)
                if returned:
                    # Persist the re-acquisition so the durable aircraft log reflects
                    # it across a restart (the disk history is dedup-newest per ICAO;
                    # /api/aircraft seeds from it). Mirrors the WiFi level-change
                    # persistence in _poll_kismet — only this meaningful state change
                    # is written, never routine position churn.
                    self._append_jsonl(self._session_dir / "aircraft.jsonl", existing)
                # Push to the live map whenever the contact currently has a position —
                # not only when its track advanced. readsb holds a plane's position with
                # a growing seen_pos while it isn't sending fresh fixes (fringe reception,
                # slow/distant target), so the position can sit frozen across polls.
                # Gating the push on track movement (moved) left such a plane in the
                # table/API but never pushed it to the map — the live marker never
                # appeared even though the record had a position. The marker's own decay
                # expires it once the plane leaves; the GUI dedups by ICAO so re-pushing
                # the same contact each poll just refreshes it.
                has_pos = existing.get("lat") is not None and existing.get("lon") is not None
                if self.gui_server is not None and (has_pos or moved or returned):
                    self.gui_server.push_event("aircraft", existing)
            else:
                event = {**aircraft, "event_type": "aircraft", "timestamp": now_iso,
                         "positions": [], "segment_start": now_iso}
                self._extend_aircraft_track(event, aircraft, now_iso)
                self._aircraft_index[icao] = event
                self.aircraft_detections.append(event)
                self._append_jsonl(self._session_dir / "aircraft.jsonl", event)
                if self.gui_server is not None:
                    self.gui_server.push_event("aircraft", event)
            # P7: persistence-score the contact (transit≈0, loiter/orbit/return
            # climbs). Stashes air_score/air_severity on the event for the GUI.
            contact = existing if existing is not None else event
            air = self._score_aircraft(contact)
            if self.gui_server is not None and air.of_interest:
                self.gui_server.push_event("aircraft", contact)
            # ACARS: a contact held continuously past the trigger requests a bounded
            # ACARS decode window (single dongle) and resolves its registration so a
            # decoded message can be tied back to it.
            await self._maybe_trigger_acars(contact, icao, now_iso)
            emergency = aircraft.get("emergency", False)
            label = aircraft.get("callsign") or aircraft.get("registration") or icao
            body = (f"{label}: alt {aircraft.get('altitude', '?')} ft, "
                    f"spd {aircraft.get('speed', '?')} kt")
            if emergency:
                # Emergencies rate-limit on their OWN key so a routine
                # of-interest alert can never suppress the first emergency page
                # — but a squawk held in view must not re-page on every 5 s
                # poll (it did: one backend send + one alerts.jsonl line per
                # poll for the whole duration).
                if await self.rate_limiter.is_allowed(f"aircraft-emergency:{icao}"):
                    self._dispatch_alert(self.alert_backend.send_aircraft_alert, aircraft)
                    self._stats["alerts_sent"] += 1
                    self._record_alert("aircraft", f"EMERGENCY — {label}", body,
                                       severity="high", icao=icao)
                else:
                    self._stats["alerts_rate_limited"] += 1
            elif air.of_interest:
                # P7: only an aircraft OF INTEREST alerts — a loiterer/orbiter near
                # the node, or a returner — never routine transit (which used to
                # alert on every airframe). No geometry reference (GPS down) ->
                # nothing scores of-interest, so only emergencies alert.
                if await self.rate_limiter.is_allowed(f"aircraft:{icao}"):
                    self._dispatch_alert(self.alert_backend.send_aircraft_alert, aircraft)
                    self._stats["alerts_sent"] += 1
                    self._record_alert(
                        "aircraft", f"Aircraft of interest — {label}",
                        f"{body} — {air.severity} (score {air.score})",
                        severity=air.severity, icao=icao,
                        air_score=air.score, air_breakdown=air.breakdown,
                    )
                else:
                    self._stats["alerts_rate_limited"] += 1
            # else: transit / not-yet-of-interest — display-only, no alert.
        # Drop aircraft gone from the sky so the index stays the live picture and
        # bounded across a multi-day run (runs in the asyncio thread; the GUI reads
        # a snapshot via current_aircraft()).
        self._prune_aircraft_index(datetime.now(timezone.utc))
        self._write_session_summary()

    def _aircraft_age_seconds(self, event: dict, now: datetime) -> float:
        """Seconds since this aircraft was last seen (large if timestamp missing)."""
        ts = event.get("timestamp")
        if not ts:
            return float("inf")
        try:
            return (now - datetime.fromisoformat(ts)).total_seconds()
        except (ValueError, TypeError):
            return float("inf")

    def _prune_aircraft_index(self, now: datetime) -> None:
        """Drop aircraft past the retention window so the index stays a bounded log."""
        stale = [
            icao for icao, ev in self._aircraft_index.items()
            if self._aircraft_age_seconds(ev, now) > self._aircraft_retention_s
        ]
        for icao in stale:
            del self._aircraft_index[icao]

    def current_aircraft(self) -> list:
        """The retained aircraft detection log (one entry per ICAO, within the
        retention window).

        Serves /api/aircraft so a page refresh rebuilds the panel from the per-ICAO
        index — a persistent log that survives refresh, not a churn-evicted slice of
        the push-log (P6). The map's shorter current-sky decay is applied client-side
        by recency, so the table persists while the map shows what's overhead now.
        Read-only snapshot — safe from the GUI thread; expiry happens in the poll loop.
        """
        now = datetime.now(timezone.utc)
        return [
            ev for ev in list(self._aircraft_index.values())
            if self._aircraft_age_seconds(ev, now) <= self._aircraft_retention_s
        ]

    def current_remote_id(self) -> list:
        """The retained Remote ID detection log (one entry per UAS ID, within the
        retention window).

        Serves /api/remote_id so a refresh rebuilds the panel from the live per-UAS
        index — an air contact, so the current-sky lens (like /api/aircraft), not the
        disk-history lens. Read-only snapshot — safe from the GUI thread.
        """
        now = datetime.now(timezone.utc)
        return [
            ev for ev in list(self._remote_id_index.values())
            if self._event_age_seconds(ev, now, "timestamp") <= self._aircraft_retention_s
        ]

    def _event_age_seconds(self, event: dict, now: datetime, *ts_keys: str) -> float:
        """Seconds since *event* was last seen, reading the first present timestamp
        key in *ts_keys* (e.g. ``"last_seen"``, ``"timestamp"``). Large if absent."""
        ts = None
        for key in ts_keys:
            ts = event.get(key)
            if ts:
                break
        if not ts:
            return float("inf")
        try:
            return (now - datetime.fromisoformat(ts)).total_seconds()
        except (ValueError, TypeError):
            return float("inf")

    def _prune_remote_id_index(self, now: datetime) -> None:
        """Drop Remote ID contacts past the retention window so the index stays a
        bounded log across a multi-day run (it was never pruned before)."""
        stale = [
            uas_id for uas_id, ev in self._remote_id_index.items()
            if self._event_age_seconds(ev, now, "timestamp") > self._aircraft_retention_s
        ]
        for uas_id in stale:
            del self._remote_id_index[uas_id]

    def _on_ble_advert(self, advert) -> None:
        '''Buffer one passively-captured BLE advertisement as a device record.

        Called from the asyncio loop's socket reader (same thread). Keyed by
        address so only the latest advert per address is kept between polls; the
        buffer is drained and merged into the device list in :meth:`_poll_kismet`.
        Carries the advert fields the unified fingerprint consumes (the BLE side
        of :mod:`modules.fixed_scoring`), plus a real RSSI as ``last_signal``.
        '''
        try:
            mac = normalize_mac(advert.address)
            self._ble_adverts[mac] = {
                "macaddr": mac,
                "type": "BTLE",
                "phyname": "BTLE",
                "name": advert.local_name or "",
                "manuf": "",
                "last_signal": advert.rssi,
                "mac_type": get_mac_type(mac),
                "is_randomized": is_randomized_mac(mac),
                "company_ids": advert.company_ids,
                "service_uuids": advert.service_uuids,
                "service_data_uuids": advert.service_data_uuids,
                "appearance": advert.appearance,
                # Enriched reconnect/identity signals (Phase: capture + display).
                "service_uuids_128": advert.service_uuids_128,
                "solicited_uuids": advert.solicited_uuids,
                "solicited_uuids_128": advert.solicited_uuids_128,
                "mfg_structures": advert.mfg_structures,
                "ble_directed": advert.directed,
            }
        except Exception:  # a malformed advert must never disturb capture
            logger.debug("BLE advert buffering failed", exc_info=True)

    def _drain_ble_adverts(self) -> list:
        '''Return and clear the BLE adverts buffered since the last poll.'''
        if not self._ble_adverts:
            return []
        drained = list(self._ble_adverts.values())
        self._ble_adverts = {}
        return drained

    def _distinctive_anchor_map(self) -> dict:
        '''TTL-cached {IE-hash: rarest distinctive SSID} from the EntityStore, used to
        attach each WiFi device's over-merge-safe identity anchor before scoring.
        Refreshed every FP_ANCHOR_REFRESH_SECONDS (default 600) — anchors stabilise
        within minutes, and the GROUP-BY over pnl_evidence should not run every poll.'''
        if self.entity_store is None:
            return {}
        ttl = float(os.getenv("FP_ANCHOR_REFRESH_SECONDS", "600"))
        if (time.monotonic() - getattr(self, "_anchor_map_ts", 0.0)) > ttl:
            try:
                self._anchor_map = self.entity_store.distinctive_anchors(
                    max_df=int(os.getenv("FP_DISTINCTIVE_MAX_DF", "3")))
                self._anchor_map_ts = time.monotonic()
            except Exception:
                logger.debug("distinctive_anchors refresh failed", exc_info=True)
        return getattr(self, "_anchor_map", {})

    def _enriched_identity_fields(self, device) -> dict:
        '''Enriched identity/reconnect signals for the GUI + analysis ONLY — not used
        for scoring or the live fingerprint key (this round is capture + display).

        WiFi: the accumulated preferred-network list ("former networks") under the
        rotation-stable IE hash, plus a stable PNL-anchored key. BLE: the
        directed-advert / solicited-service "calling out to reconnect" signals.'''
        empty = {"probe_ssids_all": [], "fingerprint_pnl": "",
                 "reconnect": False, "solicited": [], "network_affinity": []}
        if not device:
            return empty
        probe_fp = device.get("probe_fingerprint")
        pnl = []
        affinity = {}
        if self.entity_store is not None and probe_fp:
            try:
                pnl = self.entity_store.accumulated_pnl(probe_fp)
                affinity = self.entity_store.network_affinity_profile(probe_fp)
            except Exception:
                logger.debug("PNL/affinity lookup failed", exc_info=True)
        pnl_fp = compute_pnl_fingerprint(device, pnl)
        solicited = [f"{u:04x}" for u in (device.get("solicited_uuids") or [])]
        solicited += list(device.get("solicited_uuids_128") or [])
        return {
            "probe_ssids_all": list(pnl_fp.pnl) if pnl_fp else [],
            "fingerprint_pnl": pnl_fp.key if pnl_fp else "",
            "reconnect": bool(device.get("ble_directed")) or bool(solicited),
            "solicited": solicited,
            # Probed networks confirmed to exist here (probe matched a local beacon),
            # most-confirmed first — a rotation-surviving "belongs here" signal.
            "network_affinity": list(affinity.keys()),
        }

    def _contact_designator(self, event) -> str:
        '''Build the stable CLASS-IDENT-# contact designator for a WiFi/BT event.

        Class + IDENT come from fields already on the DetectionEvent; the instance
        number is the persisted, rotation-stable assignment keyed by the device's
        fingerprint (design-contact-designators.md).
        '''
        cls = contact_designator.class_token(event.device_type)
        ident = contact_designator.ident_token(
            ssid=event.ssid, label=event.fingerprint_label,
            manufacturer=event.manufacturer, fingerprint=event.fingerprint,
            mac=event.mac,
        )
        group = contact_designator.group_key(cls, ident)
        identity_key = event.fingerprint or ("mac:" + event.mac)
        number = self._assign_contact_number(identity_key, group)
        return contact_designator.designator(cls, ident, number)

    def _assign_contact_number(self, identity_key: str, group_key: str) -> int:
        '''Persisted-stable instance number via the entity store; a stable hash
        fallback when the store is unavailable.'''
        if self.entity_store is not None:
            try:
                return self.entity_store.assign_contact_number(identity_key, group_key)
            except Exception:
                logger.debug("contact number assignment failed", exc_info=True)
        return contact_designator.fallback_number(identity_key)

    async def _poll_kismet(self) -> None:
        '''Poll Kismet for WiFi/BT devices; run persistence engine; fire alerts.'''
        try:
            # GPS-stamp from the orchestrator's own fresh fix; the module no
            # longer reads the shared gpsd socket on the poll loop.
            devices = await self.kismet.poll_devices(gps_fix=self._current_fix)
        except Exception as exc:
            if self._sensor_health["kismet"]:
                logger.warning("Sensor kismet degraded: %s", exc)
                self._console_alert(f"Sensor kismet degraded: {exc}")
                self._sensor_health["kismet"] = False
                reconnected = await self._reconnect("kismet")
                if not reconnected:
                    logger.error("Kismet failed to reconnect — continuing with degraded state")
            else:
                self._degraded_log_counter["kismet"] += 1
                if self._degraded_log_counter["kismet"] % 10 == 0:
                    logger.warning("Sensor kismet still degraded after %d consecutive failures", self._degraded_log_counter["kismet"])
            return
        if not self._sensor_health["kismet"]:
            logger.info("Sensor kismet recovered")
            self._sensor_health["kismet"] = True
            self._degraded_log_counter["kismet"] = 0

        # Merge passively-captured BLE advertisements (if the scanner is running)
        # so they flow through the same entity/scoring/GUI path as WiFi devices.
        ble_devices = self._drain_ble_adverts()
        if ble_devices:
            devices = devices + ble_devices
        self._stats["kismet_devices_seen"] += len(devices)

        # Durable entity/observation recording — runs for EVERY node mode,
        # independent of which ScoringEngine processes the poll. Same device list
        # and GPS fix the scorer sees. Guarded: a store failure must never affect
        # capture or detection.
        if self.entity_store is not None:
            try:
                self.entity_store.record_poll(devices, gps_fix=self._current_fix)
            except Exception as exc:
                logger.warning("EntityStore write failed (non-fatal): %s", exc)

        # Attach each WiFi device's distinctive identity anchor (its rarest
        # near-unique probed SSID) BEFORE scoring, so the scorer keys on the enriched,
        # over-merge-safe identity (IE hash + anchor) rather than the per-poll
        # signature. TTL-cached; a device with no distinctive anchor is left without
        # one and stays mac:-keyed (un-trackable, as before).
        anchor_map = self._distinctive_anchor_map()
        if anchor_map:
            for d in devices:
                fp = d.get("probe_fingerprint")
                if fp:
                    anchor = anchor_map.get(fp)
                    if anchor:
                        d["fp_anchor"] = anchor

        # Live "nearby" feed for the mobile GUI — every currently-polled device
        # (WiFi + BLE), independent of the persistence engine's score/GPS-cluster
        # gate. Pushed AFTER anchor attachment so WiFi devices carry fp_anchor and
        # can generate the enriched, rotation-stable fingerprint label.
        # Mobile-only: nothing on a fixed node consumes "nearby", and a fixed
        # node keeps Kismet's full historical device list — pushing thousands of
        # events per poll overflows the 500-slot SSE queues and evicts every
        # connected dashboard client as "dead".
        if self.gui_server is not None and self._node_mode == "mobile":
            now_iso = datetime.now(timezone.utc).isoformat()
            for d in devices:
                fp = device_identity.strong_fingerprint(d) or ""
                fp_label = device_identity.fingerprint_label(d) if fp else ""
                self.gui_server.push_event("nearby", {
                    "mac": d.get("macaddr"),
                    "name": d.get("name"),
                    "manufacturer": d.get("manuf"),
                    "device_type": d.get("type"),
                    "mac_type": d.get("mac_type"),
                    "last_signal": d.get("last_signal"),
                    "probe_ssids": d.get("probe_ssids"),
                    "fingerprint": fp,
                    "fingerprint_label": fp_label,
                    "timestamp": now_iso,
                })

        suspicious = self.probe_analyzer.analyze(devices)
        if suspicious:
            logger.info("ProbeAnalyzer: %d suspicious probe pattern(s) detected", len(suspicious))
        try:
            detection_events = self.persistence.update(devices, gps_fix=self._current_fix)
        except Exception as exc:
            logger.warning("PersistenceEngine update error: %s", exc)
            return
        dev_by_mac = {d.get("macaddr"): d for d in devices}
        for event in detection_events:
            self._stats["persistent_detections"] += 1
            enriched = self._enriched_identity_fields(dev_by_mac.get(event.mac))
            existing = self._wifi_event_index.get(event.mac)
            if existing is not None:
                # Same device flagged again — update the ongoing detection in place
                # (no new list row). On an alert-LEVEL change, also append a fresh
                # events.jsonl line and push it: the durable history is dedup-newest
                # per MAC, so without this a page refresh re-seeds the stale
                # first-flag level while the live feed shows the current one. Gating
                # on level-change (not every poll) keeps the file bounded.
                prev_level = existing["alert_level"]
                existing.update({
                    "score": event.score, "alert_level": event.alert_level,
                    "score_breakdown": event.score_breakdown,
                    "last_seen": event.last_seen.isoformat(),
                    "observation_count": event.observation_count,
                    "lat": event.locations[0]["lat"] if event.locations else None,
                    "lon": event.locations[0]["lon"] if event.locations else None,
                    "locations": event.locations,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                })
                if event.alert_level != prev_level:
                    self._append_jsonl(self._session_dir / "events.jsonl", existing)
                    if self.gui_server is not None:
                        self.gui_server.push_event("wifi", existing)
            else:
                event_dict = {
                    "event_type": "wifi", "mac": event.mac, "score": event.score,
                    "alert_level": event.alert_level, "manufacturer": event.manufacturer,
                    "device_type": event.device_type, "mac_type": event.mac_type,
                    "ssid": event.ssid,
                    "fingerprint": event.fingerprint,
                    "fingerprint_label": event.fingerprint_label,
                    "contact": self._contact_designator(event),
                    # Which signal(s) fired — so a soak can decompose the flag mix.
                    "score_breakdown": event.score_breakdown,
                    "first_seen": event.first_seen.isoformat(), "last_seen": event.last_seen.isoformat(),
                    "observation_count": event.observation_count,
                    "lat": event.locations[0]["lat"] if event.locations else None,
                    "lon": event.locations[0]["lon"] if event.locations else None,
                    "locations": event.locations,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    **enriched,
                }
                self._wifi_event_index[event.mac] = event_dict
                self.all_events.append(event_dict)
                self._append_jsonl(self._session_dir / "events.jsonl", event_dict)
                if self.gui_server is not None:
                    self.gui_server.push_event("wifi", event_dict)
            if event.score < self._wifi_page_min_score and not event.force_page:
                # Below the paging bar — shown in the WiFi panel above, but not paged
                # (no backend send, no Alerts-feed entry). Keeps low-confidence
                # suspicious flags visible without drowning the operator.
                # force_page events (egregious-during-baseline, design 5.2) are the
                # deliberate exception: a single egregious signal scores 0.5, which
                # never clears the bar, but it is a safety-net alert that must page.
                self._stats["alerts_below_threshold"] = self._stats.get("alerts_below_threshold", 0) + 1
            elif await self.rate_limiter.is_allowed(f"persist:{event.mac}"):
                self._dispatch_alert(self.alert_backend.send_persistence_alert, event)
                self._stats["alerts_sent"] += 1
                contact = self._contact_designator(event)
                self._record_alert(
                    "wifi", f"{contact} — {event.alert_level}",
                    f"{event.device_type or 'device'} {event.mac}: score "
                    f"{event.score:.2f}, {event.observation_count} obs",
                    severity=event.alert_level, mac=event.mac,
                    alert_level=event.alert_level, score=event.score,
                    contact=contact,
                )
            else:
                self._stats["alerts_rate_limited"] += 1
        self._write_session_summary()

    async def _poll_drone_rf(self) -> None:
        '''Drain DroneRF detections buffer; append events and fire alerts.'''
        # Reflect a runtime auto-disable (#63 crash guard gave up) in live status so
        # the GUI chiclet greys out instead of staying green on a dead scanner.
        if getattr(self.drone_rf, "auto_disabled", False) and self._modules_active.get("drone_rf"):
            self._modules_active["drone_rf"] = False
            logger.warning("DroneRF auto-disabled (crash guard) — marking sensor inactive")
        try:
            pending = self.drone_rf.drain_detections()
        except Exception as exc:
            if self._sensor_health["drone_rf"]:
                logger.warning("Sensor drone_rf degraded: %s", exc)
                self._console_alert(f"Sensor drone_rf degraded: {exc}")
                self._sensor_health["drone_rf"] = False
            else:
                self._degraded_log_counter["drone_rf"] += 1
                if self._degraded_log_counter["drone_rf"] % 10 == 0:
                    logger.warning("Sensor drone_rf still degraded after %d consecutive failures", self._degraded_log_counter["drone_rf"])
            return
        if not self._sensor_health["drone_rf"]:
            logger.info("Sensor drone_rf recovered")
            self._sensor_health["drone_rf"] = True
            self._degraded_log_counter["drone_rf"] = 0
        now_iso = datetime.now(timezone.utc).isoformat()
        for detection in pending:
            freq = detection.get("freq_mhz", 0)
            power = detection.get("power_db", 0.0)
            ts = detection.get("timestamp", now_iso)
            self._stats["drone_detections"] += 1
            # Full per-sweep forensic series stays on disk (append-only, bounded
            # by the session); the in-memory list is deduped to one row per band.
            self._append_jsonl(self._session_dir / "drone.jsonl", {
                "event_type": "drone", "freq_mhz": freq, "power_db": power,
                "lat": detection.get("gps_lat"), "lon": detection.get("gps_lon"),
                "timestamp": ts,
            })
            band = str(int(freq))
            existing = self._drone_index.get(band)
            if existing is not None:
                # Same band still hot — refresh in place; don't grow the list.
                existing["power_db"] = power
                existing["peak_power_db"] = max(existing.get("peak_power_db", power), power)
                existing["lat"] = detection.get("gps_lat")
                existing["lon"] = detection.get("gps_lon")
                existing["last_seen"] = ts
                existing["timestamp"] = ts
                existing["observation_count"] = existing.get("observation_count", 1) + 1
            else:
                event_dict = {
                    "event_type": "drone", "freq_mhz": freq, "power_db": power,
                    "peak_power_db": power,
                    "lat": detection.get("gps_lat"), "lon": detection.get("gps_lon"),
                    "first_seen": ts, "last_seen": ts, "timestamp": ts,
                    "observation_count": 1,
                }
                self._drone_index[band] = event_dict
                self.drone_detections.append(event_dict)
                if self.gui_server is not None:
                    self.gui_server.push_event("drone", event_dict)
            # P7: gate on persistence — a band heard only once is a transient blip
            # (shown above, not paged). Alert once it recurs on enough sweeps.
            if self._drone_index[band].get("observation_count", 1) < self._drone_min_sweeps:
                continue
            alert_detection = {
                "freq_mhz": freq, "power_db": power,
                "lat": detection.get("gps_lat") or 0.0, "lon": detection.get("gps_lon") or 0.0,
            }
            if await self.rate_limiter.is_allowed(f"drone:{int(freq)}mhz"):
                self._dispatch_alert(self.alert_backend.send_drone_alert, alert_detection)
                self._stats["alerts_sent"] += 1
                self._record_alert(
                    "drone", f"Drone RF — {freq:.0f} MHz",
                    f"{freq:.0f} MHz at {power:.1f} dB",
                    freq_mhz=freq, power_db=power,
                )
            else:
                self._stats["alerts_rate_limited"] += 1

    async def _poll_ais(self) -> None:
        '''Drain AIS vessel reports; dedup by MMSI, log, and surface to the GUI.'''
        if getattr(self.ais, "auto_disabled", False) and self._modules_active.get("ais"):
            self._modules_active["ais"] = False
            logger.warning("AIS auto-disabled — marking sensor inactive")
        try:
            pending = self.ais.drain_detections()
        except Exception as exc:
            if self._sensor_health["ais"]:
                logger.warning("Sensor ais degraded: %s", exc)
                self._console_alert(f"Sensor ais degraded: {exc}")
                self._sensor_health["ais"] = False
            else:
                self._degraded_log_counter["ais"] += 1
                if self._degraded_log_counter["ais"] % 10 == 0:
                    logger.warning("Sensor ais still degraded after %d consecutive failures",
                                   self._degraded_log_counter["ais"])
            return
        if not self._sensor_health["ais"]:
            logger.info("Sensor ais recovered")
            self._sensor_health["ais"] = True
            self._degraded_log_counter["ais"] = 0
        now_iso = datetime.now(timezone.utc).isoformat()
        for vessel in pending:
            mmsi = vessel.get("mmsi")
            if mmsi is None:
                continue
            # Sanity gate: drop a positioned vessel that's implausibly far for VHF
            # (a misdecode). GPS-gated — no fix → no filtering. Position-less (static)
            # reports are kept; there's nothing to range-check.
            vlat, vlon = vessel.get("lat"), vessel.get("lon")
            fix = self._current_fix
            if (self._ais_max_range_km > 0
                    and vlat is not None and vlon is not None and fix
                    and fix.get("lat") is not None and fix.get("lon") is not None):
                dist_km = _haversine_m(fix["lat"], fix["lon"], vlat, vlon) / 1000.0
                if dist_km > self._ais_max_range_km:
                    self._stats["ais_out_of_range_dropped"] = (
                        self._stats.get("ais_out_of_range_dropped", 0) + 1)
                    logger.debug("AIS: dropped MMSI %s at %.0f km (> %.0f km) — misdecode",
                                 mmsi, dist_km, self._ais_max_range_km)
                    continue
            ts = vessel.get("timestamp", now_iso)
            key = str(mmsi)
            # Forensic series stays on disk; the in-memory list is one row per MMSI.
            self._append_jsonl(self._session_dir / "ais.jsonl", {
                "event_type": "ais", "mmsi": mmsi,
                "lat": vessel.get("lat"), "lon": vessel.get("lon"),
                "name": vessel.get("name"), "ship_type": vessel.get("ship_type"),
                "timestamp": ts,
            })
            existing = self._ais_index.get(key)
            if existing is not None:
                # Same vessel — refresh position/name/identity in place (static and
                # position reports arrive separately; merge, keeping known values).
                if vessel.get("lat") is not None:
                    existing["lat"] = vessel["lat"]
                    existing["lon"] = vessel["lon"]
                if vessel.get("name"):
                    existing["name"] = vessel["name"]
                if vessel.get("ship_type") is not None:
                    existing["ship_type"] = vessel["ship_type"]
                existing["last_seen"] = ts
                existing["timestamp"] = ts
                existing["observation_count"] = existing.get("observation_count", 1) + 1
            else:
                self._stats["ais_vessels_seen"] += 1
                event_dict = {
                    "event_type": "ais", "mmsi": mmsi,
                    "lat": vessel.get("lat"), "lon": vessel.get("lon"),
                    "name": vessel.get("name"), "ship_type": vessel.get("ship_type"),
                    "first_seen": ts, "last_seen": ts, "timestamp": ts,
                    "observation_count": 1,
                }
                self._ais_index[key] = event_dict
                self.ais_detections.append(event_dict)
                if self.gui_server is not None:
                    self.gui_server.push_event("ais", event_dict)

    # ------------------------------------------------------------------
    # ACARS — decode + correlate to live ADS-B contacts
    # ------------------------------------------------------------------

    async def _maybe_trigger_acars(self, contact: dict, icao: str, now_iso: str) -> None:
        '''If a contact has been held continuously past the trigger, request a bounded
        ACARS decode window (single dongle) and resolve its registration so a decoded
        message can be tied back. One-shot per contact segment.'''
        if not self._acars_enabled:
            return
        seg = contact.get("segment_start")
        if not seg:
            return
        try:
            held = (datetime.fromisoformat(now_iso) - datetime.fromisoformat(seg)).total_seconds()
        except (ValueError, TypeError):
            return
        contact["held_seconds"] = round(held)
        if held < self._acars_trigger_s or contact.get("_acars_fired"):
            return
        contact["_acars_fired"] = True
        # Resolve registration now (connectivity-adaptive) so tail↔reg matching works.
        await self._resolve_registration(contact, icao)
        # Single-dongle SHARED: preempt for a bounded ACARS window. DEDICATED runs
        # ACARS continuously on its own dongle, so no preemption is needed there.
        coord = self.sdr_coordinator
        if (coord is not None and self.sdr_mode == SDRMode.SHARED
                and hasattr(coord, "request_band_window")):
            if coord.request_band_window("acars", self._acars_window_s):
                logger.info("ACARS: %s held %ds — requested a %ds decode window",
                            contact.get("callsign") or icao, int(held), int(self._acars_window_s))

    async def _resolve_registration(self, contact: dict, icao: str) -> None:
        '''Fill contact['registration'] (and type/operator) via the connectivity-
        adaptive registry — online adsb.lol when an API key is set, else offline DB.'''
        if self.aircraft_registry is None or contact.get("registration"):
            return
        online = self.adsb.enrich_aircraft if (self._adsb_enrich_enabled and self.adsb) else None
        try:
            rec = await self.aircraft_registry.resolve(icao, online_enrich=online)
        except Exception as exc:
            logger.debug("registration resolve error for %s: %s", icao, exc)
            return
        if rec.get("registration"):
            contact["registration"] = rec["registration"]
            if rec.get("aircraft_type") and not contact.get("aircraft_type"):
                contact["aircraft_type"] = rec["aircraft_type"]
            if rec.get("operator"):
                contact["operator"] = rec["operator"]
            if "military" in rec and "military" not in contact:
                contact["military"] = rec["military"]

    @staticmethod
    def _ident_norm(s) -> str:
        '''Normalize a tail/callsign for matching: alphanumerics only, lowercased.
        (acarsdec pads tails; ADS-B callsigns carry trailing spaces.)'''
        return "".join(c for c in (s or "") if c.isalnum()).lower()

    def _correlate_acars(self, msg: dict) -> Optional[dict]:
        '''Find the live ADS-B contact a decoded ACARS message belongs to — by tail ↔
        registration first, then flight-id ↔ callsign. Returns the event or None.'''
        tail = self._ident_norm(msg.get("tail"))
        flight = self._ident_norm(msg.get("flight_id"))
        if not tail and not flight:
            return None
        for ev in self._aircraft_index.values():
            if tail:
                reg = self._ident_norm(ev.get("registration"))
                if reg and reg == tail:
                    return ev
            if flight:
                cs = self._ident_norm(ev.get("callsign"))
                if cs and cs == flight:
                    return ev
        return None

    async def _poll_acars(self) -> None:
        '''Drain decoded ACARS messages; surface them and correlate to ADS-B contacts.'''
        if getattr(self.acars, "auto_disabled", False) and self._modules_active.get("acars"):
            self._modules_active["acars"] = False
            logger.warning("ACARS auto-disabled — marking sensor inactive")
        try:
            pending = self.acars.drain_detections()
        except Exception as exc:
            if self._sensor_health["acars"]:
                logger.warning("Sensor acars degraded: %s", exc)
                self._console_alert(f"Sensor acars degraded: {exc}")
                self._sensor_health["acars"] = False
            else:
                self._degraded_log_counter["acars"] += 1
                if self._degraded_log_counter["acars"] % 10 == 0:
                    logger.warning("Sensor acars still degraded after %d consecutive failures",
                                   self._degraded_log_counter["acars"])
            return
        if not self._sensor_health["acars"]:
            logger.info("Sensor acars recovered")
            self._sensor_health["acars"] = True
            self._degraded_log_counter["acars"] = 0
        now_iso = datetime.now(timezone.utc).isoformat()
        for msg in pending:
            ts = msg.get("timestamp", now_iso)
            self._stats["acars_messages_seen"] += 1
            record = {
                "event_type": "acars", "tail": msg.get("tail"),
                "flight_id": msg.get("flight_id"), "label": msg.get("label"),
                "text": msg.get("text"), "timestamp": ts,
            }
            self._append_jsonl(self._session_dir / "acars.jsonl", record)
            # Correlate to a live contact (tail↔reg / flight↔callsign).
            matched = self._correlate_acars(msg)
            if matched is not None:
                self._stats["acars_correlated"] += 1
                record["icao"] = matched.get("icao")
                acars_list = matched.setdefault("acars", [])
                acars_list.append(record)
                # Bound the per-contact message list so a chatty airframe can't grow it.
                if len(acars_list) > 20:
                    del acars_list[:-20]
                if self.gui_server is not None:
                    self.gui_server.push_event("aircraft", matched)
            # Always surface the raw decoded message on the ACARS feed. Bounded so a
            # chatty datalink environment can't grow this list without limit (the GUI
            # cache is separately capped); newest-kept.
            self.acars_detections.append(record)
            if len(self.acars_detections) > _ACARS_FEED_MAX:
                del self.acars_detections[:-_ACARS_FEED_MAX]
            if self.gui_server is not None:
                self.gui_server.push_event("acars", record)

    async def _poll_remote_id(self) -> None:
        '''Poll Kismet for Remote ID frames; append events and fire alerts.'''
        if self.remote_id is None:
            return
        try:
            detections = await self.remote_id.poll()
        except Exception as exc:
            if self._sensor_health["remote_id"]:
                logger.warning("Sensor remote_id degraded: %s", exc)
                self._console_alert(f"Sensor remote_id degraded: {exc}")
                self._sensor_health["remote_id"] = False
            else:
                self._degraded_log_counter["remote_id"] += 1
                if self._degraded_log_counter["remote_id"] % 10 == 0:
                    logger.warning(
                        "Sensor remote_id still degraded after %d consecutive failures",
                        self._degraded_log_counter["remote_id"],
                    )
            return
        if not self._sensor_health["remote_id"]:
            logger.info("Sensor remote_id recovered")
            self._sensor_health["remote_id"] = True
            self._degraded_log_counter["remote_id"] = 0
        now_iso = datetime.now(timezone.utc).isoformat()
        for detection in detections:
            self._stats["remote_id_detections"] += 1
            # Full per-frame forensic log stays on disk; the in-memory list is
            # deduped to one event per UAS ID with a thinned drone flight path.
            self._append_jsonl(self._session_dir / "remote_id.jsonl", detection)
            uas_id = detection.get("uas_id") or "unknown"
            existing = self._remote_id_index.get(uas_id)
            if existing is not None:
                # Same drone still broadcasting — refresh state and extend its
                # flight path in place instead of appending a row per frame.
                existing.update(detection)
                existing["timestamp"] = now_iso
                moved = self._extend_track(
                    existing, detection.get("drone_lat"),
                    detection.get("drone_lon"), detection.get("drone_alt_m"), now_iso,
                )
                # Push only on a track move to keep the live feed bounded (mirrors
                # the aircraft path).
                if self.gui_server is not None and moved:
                    self.gui_server.push_event("remote_id", existing)
            else:
                event = {**detection, "positions": [], "timestamp": now_iso}
                self._extend_track(
                    event, detection.get("drone_lat"),
                    detection.get("drone_lon"), detection.get("drone_alt_m"), now_iso,
                )
                self._remote_id_index[uas_id] = event
                self.remote_id_detections.append(event)
                if self.gui_server is not None:
                    self.gui_server.push_event("remote_id", event)
            if await self.rate_limiter.is_allowed(f"remote_id:{uas_id}"):
                self._dispatch_alert(self.alert_backend.send_remote_id_alert, detection)
                self._stats["alerts_sent"] += 1
                self._record_alert(
                    "aircraft", f"Remote ID — {uas_id}",
                    f"UAS {uas_id}: "
                    f"{detection.get('ua_type', 'UAS')} "
                    f"op {detection.get('operator_id', '?')}",
                    severity="high", uas_id=uas_id,
                )
            else:
                self._stats["alerts_rate_limited"] += 1
        # Expire departed UAS so the index stays the live picture and bounded.
        self._prune_remote_id_index(datetime.now(timezone.utc))

    # ------------------------------------------------------------------
    # Alert dispatch (off the event loop)
    # ------------------------------------------------------------------

    def _dispatch_alert(self, send_fn, *args) -> bool:
        """Fire an alert off the event loop, fire-and-forget and bounded.

        ``send_fn`` is a blocking backend method (``send_persistence_alert`` etc.)
        that does synchronous network I/O. Running it inline on the event loop
        starves all async tasks — including the watchdog heartbeat — so a slow or
        unreachable backend becomes a systemd-watchdog kill loop (the soak-#3
        cascade). Offload to the single-thread alert pool and never await: a hung
        backend can only delay alerts, never the loop. Returns True if the send was
        scheduled, False if it was dropped because too many sends are already in
        flight (backlog bound — better to drop noise than grow an unbounded queue
        behind a wedged backend).
        """
        if self._alerts_inflight >= self._alert_max_inflight:
            self._stats["alerts_dropped"] += 1
            return False
        self._alerts_inflight += 1
        loop = asyncio.get_running_loop()
        future = loop.run_in_executor(self._alert_executor, send_fn, *args)
        future.add_done_callback(self._alert_done)
        return True

    def _alert_done(self, future) -> None:
        """Done-callback for a dispatched alert; runs on the event loop thread."""
        self._alerts_inflight -= 1
        try:
            exc = future.exception()
        except Exception:
            return  # cancelled (e.g. executor shutdown) — nothing to report
        if exc is not None:
            logger.warning("alert send raised off-loop: %s", exc)

    def _record_alert(self, kind: str, title: str, body: str, *,
                      severity: str = "default", **meta) -> None:
        '''Persist an alert and push it to the GUI Alerts feed.

        Alerts are otherwise fire-and-forget through the backend
        (``_dispatch_alert``), so they leave no durable trace and never reach the
        GUI Alerts tab. This records each alert we decide to send as one line in
        the session's ``alerts.jsonl`` and live-pushes it (``push_event("alert")``),
        so the operator surface shows alerts and survives a refresh or a restart
        (P5 durable history). ``kind`` is one of ``wifi`` / ``aircraft`` / ``drone``
        / ``system`` (drives the GUI card colour). Guarded — a logging or GUI
        failure never affects the alert send itself.
        '''
        record = {
            "kind": kind, "title": title, "body": body, "severity": severity,
            "timestamp": datetime.now(timezone.utc).isoformat(), **meta,
        }
        self._append_jsonl(self._session_dir / "alerts.jsonl", record)
        if self.gui_server is not None:
            try:
                self.gui_server.push_event("alert", record)
            except Exception as exc:
                logger.debug("alert push to GUI failed: %s", exc)

    # ------------------------------------------------------------------
    # Graceful startup / radio-health alerting
    # ------------------------------------------------------------------

    _RADIO_LABELS = {"kismet": "WiFi", "ble": "BLE", "adsb": "ADS-B",
                     "drone_rf": "DroneRF", "gps": "GPS", "remote_id": "Remote ID",
                     "ais": "AIS", "acars": "ACARS"}

    def _radio_is_up(self, name: str) -> bool:
        '''Effective "is this radio capturing?" — module enabled AND healthy.

        ``_modules_active`` alone misses a mid-run death that never disables
        the module: a readsb/USB drop only flips ``_sensor_health`` (the poll
        keeps failing), and a BLE controller loss only flips the scanner's
        ``available`` flag. Folding those in is what lets the watchdog alert a
        2026-06-20-style USB drop on the radios most likely to suffer one.
        '''
        if not self._modules_active.get(name):
            return False
        if name == "ble":
            scanner = self.ble_scanner
            return scanner is None or bool(getattr(scanner, "available", True))
        if name == "adsb" and self._modules_active.get("sdr_coordinator"):
            # Time-share blackouts make ADS-B poll health flap by design (the
            # decoder is stopped during other bands' slices) — never read a
            # scheduled blackout as a dead radio.
            return True
        return self._sensor_health.get(name, True)

    def startup_health_report(self, expected: set) -> None:
        '''Assess each expected radio after startup bring-up: log a one-line health
        summary and fire an operator ALERT for any that came up DOWN. Called once from
        main.py at the end of startup. *expected* is what main.py tried to bring up
        (kismet/gps/remote_id always; ble/adsb/drone_rf per config + SDR detection).'''
        self._expected_radios = set(expected)
        summary = " ".join(
            f"{self._RADIO_LABELS.get(n, n)}{'✓' if self._modules_active.get(n) else '✗'}"
            for n in sorted(self._expected_radios)
        )
        logger.info("Startup health: %s", summary)
        self._check_radio_health()

    def _check_radio_health(self) -> None:
        '''Alert (once, debounced) on any expected radio that is DOWN and note when a
        previously-down one RECOVERS. Shared by startup_health_report and the watchdog,
        so a startup-disabled radio and a mid-run drop are handled identically — the
        gap that let the 2026-06-20 USB drop go unnoticed for ~7 h.'''
        for name in self._expected_radios:
            up = self._radio_is_up(name)
            if not up and name not in self._radio_down_alerted:
                self._radio_down_alerted.add(name)
                self._alert_radio_down(name)
            elif up and name in self._radio_down_alerted:
                self._radio_down_alerted.discard(name)
                label = self._RADIO_LABELS.get(name, name)
                logger.info("Sensor recovered: %s back online", label)
                self._record_alert("system", f"{label} recovered",
                                   f"{label} is back online.", severity="default",
                                   sensor=name)

    def _alert_radio_down(self, name: str) -> None:
        '''One loud operator alert (console + GUI Alerts tab + persisted) that a radio
        is down. A wedged USB radio is not software-recoverable (a PV restart does not
        fix it), so this surfaces it for a physical replug/reboot rather than looping.'''
        label = self._RADIO_LABELS.get(name, name)
        hint = {
            "ble": "BT controller unavailable — replug the dongle or reboot",
            "adsb": "no RTL-SDR detected — reseat the SDR or reboot",
            "drone_rf": "no RTL-SDR detected — reseat the SDR or reboot",
            "gps": "gpsd unreachable — check the gpsd service and antenna",
            "kismet": "Kismet REST unreachable — check the kismet service",
        }.get(name, "sensor unavailable — check the device")
        body = f"{label} is DOWN: {hint}."
        logger.warning("Sensor down: %s", body)
        try:
            self._console_alert(body)
        except Exception:
            logger.debug("console alert failed", exc_info=True)
        self._record_alert("system", f"{label} down", body, severity="high", sensor=name)

    # ------------------------------------------------------------------
    # Watchdog
    # ------------------------------------------------------------------

    def _mark_poll(self, name: str) -> None:
        '''Record that sensor *name* just completed a poll cycle (liveness).'''
        self._last_poll_ts[name] = time.monotonic()

    def _data_is_progressing(self, name: str) -> bool:
        '''True if *name*'s cumulative stat advanced within the data-stall window.

        Used both by the data-stall trip and by the systemd heartbeat (which only
        beats while the primary sensor is genuinely capturing). A sensor not on the
        data-progress list, or one that has not produced a first reading yet, is
        treated as progressing so it never falsely suppresses the heartbeat.
        '''
        if name not in self._data_sensors:
            return True
        last_adv = self._last_progress_ts.get(name)
        if last_adv is None:
            return True
        return (time.monotonic() - last_adv) <= self._data_stall_s

    def _check_watchdog(self) -> list[str]:
        '''Flip degraded any sensor whose poll loop has gone silent OR whose data
        has frozen, and return the list of sensors that newly tripped.

        Two failure modes are caught:

        - **Loop-liveness:** a poll loop that stops *completing* without raising (a
          hung await, a dead task) — the node keeps reporting ✓ while nothing is
          captured. Liveness is the poll cycle completing, not data volume, so an
          idle-but-healthy sensor (e.g. no aircraft overhead) is never flagged.
        - **Data-progress:** a loop that keeps completing while its source is dead
          upstream — the cumulative counter flatlines. Applied only to the
          configured data sensors (kismet by default); ADS-B is off by default
          because an empty sky is a legitimate flat counter.

        A sensor that has not polled yet (startup) is skipped until its first
        completed cycle. Returns the names that tripped on this pass so the caller
        can drive recovery (reconnect → restart).
        '''
        now = time.monotonic()
        tripped: list[str] = []
        for name in ("gps", "kismet", "adsb", "drone_rf", "remote_id"):
            if not self._modules_active.get(name, False):
                continue
            last = self._last_poll_ts.get(name)
            if last is None:
                continue
            if not self._sensor_health.get(name, False):
                continue

            idle = now - last
            loop_stalled = idle > self._watchdog_stall_s

            # Data-progress: snapshot the counter and remember when it last moved.
            data_stalled = False
            data_frozen_s = 0.0
            if name in self._data_sensors:
                stat_key = self._data_stat_key.get(name)
                if stat_key is not None:
                    value = self._stats.get(stat_key, 0)
                    prev = self._last_progress_value.get(name)
                    if prev is None or value != prev:
                        self._last_progress_value[name] = value
                        self._last_progress_ts[name] = now
                    else:
                        data_frozen_s = now - self._last_progress_ts.get(name, now)
                        data_stalled = data_frozen_s > self._data_stall_s

            if loop_stalled:
                logger.warning(
                    "Watchdog: sensor %s has not completed a poll in %.0fs "
                    "(threshold %.0fs) — marking degraded",
                    name, idle, self._watchdog_stall_s,
                )
                self._console_alert(
                    f"Sensor {name} stalled — no successful poll in {int(idle)}s "
                    f"(was reporting healthy)"
                )
                self._sensor_health[name] = False
                tripped.append(name)
            elif data_stalled:
                logger.warning(
                    "Watchdog: sensor %s loop is alive but its data has not "
                    "advanced in %.0fs (threshold %.0fs) — capture frozen, "
                    "marking degraded",
                    name, data_frozen_s, self._data_stall_s,
                )
                self._console_alert(
                    f"Sensor {name} capture frozen — no new data in "
                    f"{int(data_frozen_s)}s (loop still running)"
                )
                self._sensor_health[name] = False
                tripped.append(name)
            else:
                # Sensor passed every check this pass: healthy, loop live, and
                # (for a data sensor) data progressing again. The stall episode
                # is over, so drop any one-reconnect-per-episode marker — a fresh
                # future stall earns its own reconnect before escalating.
                self._stalled_since_reconnect.discard(name)
        return tripped

    # ------------------------------------------------------------------
    # Watchdog loop + recovery escalation
    # ------------------------------------------------------------------

    async def _watchdog_loop(self) -> None:
        '''Dedicated pure-async watchdog: detect stalls, drive recovery, beat systemd.

        Runs on its own short interval (WATCHDOG_INTERVAL_SECONDS, default 30s),
        independent of the 5-minute health banner, so detection latency is roughly
        the stall threshold rather than up to five minutes. Kept pure-async (only
        asyncio.sleep, in-memory reads, _reconnect, and os._exit) so it stays
        schedulable even if the default executor thread-pool is starved — a prime
        suspect for a process-wide wedge.
        '''
        # Notify systemd we have finished startup (required by Type=notify).
        self._sd_notify("READY=1")
        while not self._stop_event.is_set():
            try:
                await asyncio.sleep(self._watchdog_interval_s)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error("Watchdog loop sleep error: %s", exc)
            if self._stop_event.is_set():
                break
            try:
                tripped = self._check_watchdog()
                if tripped:
                    await self._handle_stall(tripped)
                # Alert on a radio that came up disabled or dropped mid-run (a USB
                # unplug cleanly disables a sensor without stalling a poll loop, so
                # the stall check above misses it). Debounced; no auto-restart — a
                # wedged USB radio isn't fixed by restarting.
                self._check_radio_health()
                # Beat systemd's hardware-style watchdog ONLY while capture is
                # genuinely progressing. If the primary sensor's data has frozen,
                # heartbeats stop and systemd restarts us after WatchdogSec — the
                # layer that survives even a total in-process wedge.
                if self._capture_progressing():
                    self._sd_notify("WATCHDOG=1")
            except Exception as exc:
                logger.error("Watchdog evaluation error: %s", exc, exc_info=True)

    def _capture_progressing(self) -> bool:
        '''True if every active data sensor is still advancing its counter.

        Gates the systemd heartbeat: a single frozen data sensor stops the beat so
        the outer net can act even when the in-process recovery cannot fire.
        '''
        for name in self._data_sensors:
            if not self._modules_active.get(name, False):
                continue
            if not self._data_is_progressing(name):
                return False
        return True

    async def _handle_stall(self, tripped: list[str]) -> None:
        '''Recover from one or more stalled sensors: reconnect, else self-restart.

        Escalates to a self-restart (os._exit(1) → systemd Restart=always respawn,
        durable BaselineStore resumes the learning window) when: a reconnect fails,
        OR two or more sensors are stalled at once (a process-wide wedge — exactly
        the incident this guards), OR a sensor re-trips after a reconnect within
        this episode. A persistent root cause cannot become a restart loop: the
        crash-guard caps self-restarts per window.
        '''
        # Two or more sensors stalled together ⇒ process-wide wedge ⇒ restart now.
        if len(tripped) >= 2:
            self._self_restart(
                f"{len(tripped)} sensors stalled simultaneously "
                f"({', '.join(sorted(tripped))}) — process-wide wedge"
            )
            return

        for name in tripped:
            if name in self._stalled_since_reconnect:
                # Already reconnected once this episode and it stalled again.
                self._self_restart(
                    f"sensor {name} re-stalled after a reconnect — escalating"
                )
                return
            reconnected = await self._reconnect(name)
            if not reconnected:
                self._self_restart(
                    f"sensor {name} stalled and failed to reconnect — escalating"
                )
                return
            # Reconnect succeeded: clear the frozen-data baseline so the next pass
            # measures progress fresh, and remember we already gave it one chance.
            self._last_progress_ts[name] = time.monotonic()
            self._last_progress_value.pop(name, None)
            self._stalled_since_reconnect.add(name)

    def _self_restart(self, reason: str) -> None:
        '''Exit the process so systemd respawns it — unless the crash-guard trips.

        os._exit(1) is dependency-free and fires even through a wedged event loop.
        Restart timestamps are persisted (os._exit wipes memory) so more than
        WATCHDOG_MAX_RESTARTS within WATCHDOG_RESTART_WINDOW_S stops the loop: the
        node logs CRITICAL and stays up degraded rather than crash-looping on a
        persistent root cause.
        '''
        now = time.time()
        recent = self._record_restart(now)
        if len(recent) > self._max_restarts:
            logger.critical(
                "Watchdog: %s — but %d self-restarts already in the last %.0fs "
                "(limit %d). Suppressing restart; staying up DEGRADED. A persistent "
                "root cause needs manual attention.",
                reason, len(recent), self._restart_window_s, self._max_restarts,
            )
            self._console_alert(
                f"Watchdog restart loop suppressed ({len(recent)} restarts in window) "
                f"— node staying up degraded; manual attention needed"
            )
            return
        logger.critical(
            "Watchdog: %s — self-restarting (restart %d in window) so systemd "
            "respawns a clean process.",
            reason, len(recent),
        )
        self._console_alert(f"Watchdog self-restart: {reason}")
        # Best-effort: stop the systemd heartbeat before we go.
        self._sd_notify("STOPPING=1")
        os._exit(1)

    def _record_restart(self, now: float) -> list[float]:
        '''Append *now* to the persisted restart log and return the in-window list.

        Returns the timestamps (including this one) that fall inside the restart
        window — its length is what the crash-guard checks. Failures to read/write
        the log are non-fatal: an empty/corrupt log just means we count from here.
        '''
        timestamps: list[float] = []
        try:
            if self._restart_log_path.exists():
                raw = json.loads(self._restart_log_path.read_text(encoding="utf-8"))
                if isinstance(raw, list):
                    timestamps = [float(t) for t in raw]
        except Exception as exc:
            logger.warning("Watchdog restart-log read failed (non-fatal): %s", exc)
            timestamps = []
        timestamps.append(now)
        # Keep only timestamps inside the window so the file stays small.
        cutoff = now - self._restart_window_s
        in_window = [t for t in timestamps if t >= cutoff]
        try:
            self._restart_log_path.parent.mkdir(parents=True, exist_ok=True)
            self._restart_log_path.write_text(
                json.dumps(in_window), encoding="utf-8"
            )
        except Exception as exc:
            logger.warning("Watchdog restart-log write failed (non-fatal): %s", exc)
        return in_window

    def _sd_notify(self, state: str) -> None:
        '''Send *state* to systemd via $NOTIFY_SOCKET (sd_notify), stdlib-only.

        No-op when NOTIFY_SOCKET is unset (dev / non-systemd runs). A leading '@'
        denotes a Linux abstract namespace socket. Any failure is swallowed — a
        missing heartbeat must never crash the watchdog.
        '''
        if not self._notify_socket:
            return
        import socket
        addr = self._notify_socket
        if addr.startswith("@"):
            addr = "\0" + addr[1:]
        try:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
            try:
                sock.sendto(state.encode("utf-8"), addr)
            finally:
                sock.close()
        except Exception as exc:
            logger.debug("sd_notify(%s) failed: %s", state, exc)

    # ------------------------------------------------------------------
    # Health banner
    # ------------------------------------------------------------------

    def _log_health_banner(self) -> None:
        '''Emit a structured INFO log summarising session health and cumulative stats.'''
        uptime = datetime.now(timezone.utc) - self.session_start
        total_secs = int(uptime.total_seconds())
        h = total_secs // 3600
        m = (total_secs % 3600) // 60
        s = total_secs % 60
        uptime_str = f"{h}h {m:02d}m {s:02d}s"

        if self._current_fix:
            gps_status = "✓ Fixed"
            gps_loc = f"Lat: {self._current_fix['lat']:.4f} Lon: {self._current_fix['lon']:.4f}"
        else:
            gps_status = "✗ No fix"
            gps_loc = "Lat: N/A  Lon: N/A"

        def _status(key):
            return "✓ Active" if self._sensor_health.get(key, False) else "✗ Degraded"

        backend_name = type(self.alert_backend).__name__.replace("Backend", "")
        sep = "─" * 54
        sdr_status = "✓ Healthy" if getattr(self.sdr_coordinator, "healthy", True) else "✗ Degraded"

        logger.info(sep)
        logger.info("── Passive Vigilance Health ──────────────────────────")
        logger.info("Session: %s | Uptime: %s", self.session_id, uptime_str)
        logger.info("GPS:     %s | %s", gps_status, gps_loc)
        logger.info("Kismet:    %s | Devices seen: %d", _status("kismet"), self._stats["kismet_devices_seen"])
        # "Sightings" not "Aircraft": aircraft_seen is a cumulative per-poll sighting
        # tally (one bump per plane per 5s poll, pre-dedup), not the count of distinct
        # airframes — the GUI Aircraft tab is the distinct count.
        logger.info("ADS-B:     %s | Sightings: %d", _status("adsb"), self._stats["aircraft_seen"])
        logger.info("DroneRF:   %s | Detections: %d", _status("drone_rf"), self._stats["drone_detections"])
        logger.info("RemoteID:  %s | Detections: %d", _status("remote_id"), self._stats["remote_id_detections"])
        logger.info("SDR:       %s | Mode: %s | Owner: %s", sdr_status, self.sdr_mode.value, self.sdr_coordinator.current_owner)
        logger.info("Alerts:    %s | Sent: %d | Rate-limited: %d | Dropped: %d", backend_name, self._stats["alerts_sent"], self._stats["alerts_rate_limited"], self._stats["alerts_dropped"])
        logger.info("Events:    %d persistent | %d aircraft-sightings | %d drone | %d remote_id", self._stats["persistent_detections"], self._stats["aircraft_seen"], self._stats["drone_detections"], self._stats["remote_id_detections"])
        logger.info(sep)

    # ------------------------------------------------------------------
    # Reconnection
    # ------------------------------------------------------------------

    async def _reconnect(self, module_name: str) -> bool:
        '''Attempt to close and reconnect a named module up to max_reconnect_attempts times.

        Returns True on success, False after exhausting all attempts.
        Supports: "gps", "kismet", "adsb", "sdr", "remote_id".
        '''
        if module_name not in ("gps", "kismet", "adsb", "sdr", "remote_id") or (
                module_name == "remote_id" and self.remote_id is None):
            # DroneRF (subprocess/coordinator-managed) and anything unknown
            # have no in-place reconnect path. Returning False lets the stall
            # handler escalate to the crash-guarded self-restart deliberately,
            # not by falling through an unknown-module branch.
            logger.warning("_reconnect: no reconnect path for %r — skipping", module_name)
            return False
        max_attempts = self._max_reconnect_attempts
        for attempt in range(1, max_attempts + 1):
            logger.warning("Attempting reconnect %s (%d/%d)...", module_name, attempt, max_attempts)
            try:
                if module_name == "gps":
                    try:
                        await self._run_gps_call(self.gps.close)
                    except Exception:
                        pass
                    await self._run_gps_call(self.gps.connect)
                elif module_name == "kismet":
                    try:
                        await self.kismet.close()
                    except Exception:
                        pass
                    await self.kismet.connect()
                elif module_name == "adsb":
                    try:
                        await self.adsb.close()
                    except Exception:
                        pass
                    await self.adsb.connect()
                elif module_name == "remote_id":
                    # Shares Kismet's REST endpoint — its natural recovery is
                    # a fresh session, same as the Kismet path.
                    try:
                        await self.remote_id.close()
                    except Exception:
                        pass
                    await self.remote_id.connect()
                elif module_name == "sdr":
                    try:
                        await self.sdr_coordinator.stop()
                    except Exception:
                        pass
                    await self.sdr_coordinator.start()
                    self._modules_active["sdr_coordinator"] = True
                logger.info("Sensor %s reconnected successfully", module_name)
                self._sensor_health[module_name] = True
                return True
            except Exception as exc:
                logger.warning("Reconnect attempt %d/%d failed for %s: %s", attempt, max_attempts, module_name, exc)
                if attempt < max_attempts:
                    await asyncio.sleep(self._reconnect_interval)
        logger.error("Sensor %s failed to reconnect after %d attempts — giving up until next health check", module_name, max_attempts)
        return False

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _extend_track(self, event: dict, lat, lon, alt, ts_iso: str) -> bool:
        '''Append a thinned ``{lat, lon, altitude, timestamp}`` point to
        ``event['positions']``.

        Adds a point only if there is a position AND the target has moved at
        least ``AIRCRAFT_TRACK_MIN_METERS`` or ``AIRCRAFT_TRACK_MIN_SECONDS`` have
        passed since the last point — so a slow/hovering target doesn't bloat the
        track. Positionless sightings add no point. Returns True if a point was
        added. Shared by the aircraft (ADS-B) and Remote ID flight-path tracks.
        '''
        if lat is None or lon is None:
            return False
        try:
            lat, lon = float(lat), float(lon)
        except (TypeError, ValueError):
            return False
        pts = event.setdefault("positions", [])
        # Skip thinning when the last entry is a gap sentinel (a returning airframe):
        # the first point after an absence is always kept, starting the new leg.
        if pts and "lat" in pts[-1]:
            last = pts[-1]
            dist = _haversine_m(last["lat"], last["lon"], lat, lon)
            try:
                dt = (datetime.fromisoformat(ts_iso)
                      - datetime.fromisoformat(last["timestamp"])).total_seconds()
            except Exception:
                dt = self._aircraft_track_min_s + 1.0
            if dist < self._aircraft_track_min_m and dt < self._aircraft_track_min_s:
                return False
        pts.append({"lat": lat, "lon": lon, "altitude": alt, "timestamp": ts_iso})
        # Bound the track so a long loiter can't grow it without limit.
        if len(pts) > self._track_max_points:
            del pts[: len(pts) - self._track_max_points]
        return True

    def _extend_aircraft_track(self, event: dict, aircraft: dict, ts_iso: str) -> bool:
        '''Extend an aircraft event's flight-path track from an ADS-B sighting.'''
        return self._extend_track(
            event, aircraft.get("lat"), aircraft.get("lon"),
            aircraft.get("altitude"), ts_iso,
        )

    def _score_aircraft(self, event: dict) -> "air_scoring.AirScore":
        '''Persistence-score one aircraft event against the node reference and stash
        air_score / air_severity / air_of_interest / air_breakdown on it (P7). Pure
        scorer; no-op-safe when there is no GPS/home reference (score 0). The
        long-horizon return_count is read from the event (#136) when present.'''
        reference = air_geometry.resolve_reference(self._current_fix, os.environ)
        flags = air_scoring.InterestFlags(
            military=bool(event.get("military")),
            no_callsign=not (event.get("callsign") or "").strip(),
        )
        result = air_scoring.score_air_contact(
            event.get("positions", []), reference,
            return_count=int(event.get("return_count", 0) or 0),
            flags=flags, params=self._air_params,
        )
        event["air_score"] = result.score
        event["air_severity"] = result.severity
        event["air_of_interest"] = result.of_interest
        event["air_breakdown"] = result.breakdown
        return result

    def _mark_track_gap(self, event: dict, ts_iso: str) -> None:
        '''Insert a gap sentinel into an aircraft's track so the flight path breaks
        across an absence instead of drawing a straight line over it. Consecutive
        gaps are coalesced; the gap respects the track point cap.'''
        pts = event.setdefault("positions", [])
        if not pts or pts[-1].get("gap"):
            return
        pts.append({"gap": True, "timestamp": ts_iso})
        if len(pts) > self._track_max_points:
            del pts[: len(pts) - self._track_max_points]

    def _note_aircraft_return(self, event: dict, icao: str, gap_seconds: float, ts_iso: str) -> None:
        '''Flag a returning airframe as of-interest: mark a track gap, tag the event,
        record it to the alerts feed/history, and log. No backend send (avoids alert
        fatigue) — the operator sees it on the dashboard. Returns are naturally
        bounded per airframe by the gap threshold.'''
        self._mark_track_gap(event, ts_iso)
        event["returning"] = True
        event["return_count"] = int(event.get("return_count", 0)) + 1
        event["last_gap_seconds"] = round(gap_seconds)
        self._stats["aircraft_returns"] = self._stats.get("aircraft_returns", 0) + 1
        label = event.get("callsign") or event.get("registration") or icao
        mins = round(gap_seconds / 60)
        logger.info("Aircraft of interest: %s (%s) returned after ~%d min absence (return #%d)",
                    label, icao, mins, event["return_count"])
        self._record_alert(
            "aircraft", f"Returning aircraft — {label}",
            f"Re-seen after ~{mins} min absence (return #{event['return_count']})",
            severity="default", icao=icao, returning=True,
            return_count=event["return_count"], gap_seconds=round(gap_seconds),
        )

    def _console_alert(self, message: str) -> None:
        '''Send an alert to the console backend (used for sensor health degradation).'''
        from modules.alerts import ConsoleBackend
        ConsoleBackend().send("Sensor Health", message, priority="high", tags=["sensor", "health"])

    def _append_jsonl(self, path: Path, data: dict) -> None:
        '''Append a JSON line to a .jsonl file, creating parent dirs as needed.'''
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(data, default=str) + "\n")
        except Exception as exc:
            logger.debug("JSONL append error (%s): %s", path.name, exc)

    def _write_session_summary(self) -> None:
        '''Write an incremental summary.json to the session directory.'''
        try:
            now = datetime.now(timezone.utc)
            summary = {
                "session_id": self.session_id,
                "start_time": self.session_start.isoformat(),
                "end_time": now.isoformat(),
                "duration_seconds": int((now - self.session_start).total_seconds()),
                "gps_fixes_received": self._gps_fix_count,
                "unique_devices_tracked": len({e["mac"] for e in self.all_events}),
                "persistent_detections": len(self.all_events),
                "aircraft_detected": len(self.aircraft_detections),
                "drone_detections": len(self.drone_detections),
                "remote_id_detections": len(self.remote_id_detections),
                "modules_active": dict(self._modules_active),
            }
            self._session_dir.mkdir(parents=True, exist_ok=True)
            summary_path = self._session_dir / "summary.json"
            tmp_path = self._session_dir / "summary.json.tmp"
            with open(tmp_path, "w", encoding="utf-8") as fh:
                json.dump(summary, fh, indent=2, default=str)
            os.replace(tmp_path, summary_path)
        except Exception as exc:
            logger.debug("Incremental summary write error: %s", exc)
