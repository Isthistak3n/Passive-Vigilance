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
        session_id: str,
        session_start: datetime,
        session_dir: Path,
        sdr_mode,
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

        self.session_id = session_id
        self.session_start = session_start
        self._session_dir = session_dir
        self.sdr_mode = sdr_mode
        self._stop_event = stop_event

        self._gps_poll_interval = gps_poll_interval
        self._adsb_poll_interval = adsb_poll_interval
        self._kismet_poll_interval = kismet_poll_interval
        self._drone_poll_interval = drone_poll_interval
        self._remote_id_poll_interval = remote_id_poll_interval
        self._health_banner_interval = health_banner_interval
        self._max_reconnect_attempts = max_reconnect_attempts
        self._reconnect_interval = reconnect_interval
        self._modules_active = modules_active

        self._sensor_health: dict[str, bool] = {
            "gps": True, "kismet": True, "adsb": True, "drone_rf": True, "sdr": True,
            "remote_id": True,
        }
        self._degraded_log_counter: dict[str, int] = {
            "gps": 0, "kismet": 0, "adsb": 0, "drone_rf": 0, "sdr": 0, "remote_id": 0,
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
            "remote_id_detections": 0,
            "alerts_sent": 0,
            "alerts_rate_limited": 0,
            "alerts_dropped": 0,
            "persistent_detections": 0,
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
        self.drone_detections: list[dict] = []
        # Index freq-band -> the drone event already in drone_detections, so a
        # persistent emitter heard on every sweep becomes ONE event (refreshed
        # in place with the latest/peak power and a running count) instead of a
        # row per sweep. A fixed node doesn't move, so no geographic track.
        self._drone_index: dict[str, dict] = {}
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
            icao = aircraft.get("icao") or "unknown"
            existing = self._aircraft_index.get(icao)
            if existing is not None:
                # Same plane — refresh current-state fields in place and extend
                # its track (thinned). One event per ICAO, not one per sighting.
                existing.update({**aircraft, "event_type": "aircraft", "timestamp": now_iso})
                moved = self._extend_aircraft_track(existing, aircraft, now_iso)
                if self.gui_server is not None and moved:
                    self.gui_server.push_event("aircraft", existing)
            else:
                event = {**aircraft, "event_type": "aircraft", "timestamp": now_iso, "positions": []}
                self._extend_aircraft_track(event, aircraft, now_iso)
                self._aircraft_index[icao] = event
                self.aircraft_detections.append(event)
                self._append_jsonl(self._session_dir / "aircraft.jsonl", event)
                if self.gui_server is not None:
                    self.gui_server.push_event("aircraft", event)
            emergency = aircraft.get("emergency", False)
            if emergency:
                self._dispatch_alert(self.alert_backend.send_aircraft_alert, aircraft)
                self._stats["alerts_sent"] += 1
            else:
                if await self.rate_limiter.is_allowed(f"aircraft:{aircraft.get('icao', 'unknown')}"):
                    self._dispatch_alert(self.alert_backend.send_aircraft_alert, aircraft)
                    self._stats["alerts_sent"] += 1
                else:
                    self._stats["alerts_rate_limited"] += 1
        self._write_session_summary()

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

        suspicious = self.probe_analyzer.analyze(devices)
        if suspicious:
            logger.info("ProbeAnalyzer: %d suspicious probe pattern(s) detected", len(suspicious))
        try:
            detection_events = self.persistence.update(devices, gps_fix=self._current_fix)
        except Exception as exc:
            logger.warning("PersistenceEngine update error: %s", exc)
            return
        for event in detection_events:
            self._stats["persistent_detections"] += 1
            existing = self._wifi_event_index.get(event.mac)
            if existing is not None:
                # Same device flagged again — update the ongoing detection in
                # place (no new list row, no new JSONL line). Push to the GUI
                # only on an alert-level change to keep the live feed bounded.
                prev_level = existing["alert_level"]
                existing.update({
                    "score": event.score, "alert_level": event.alert_level,
                    "last_seen": event.last_seen.isoformat(),
                    "observation_count": event.observation_count,
                    "lat": event.locations[0]["lat"] if event.locations else None,
                    "lon": event.locations[0]["lon"] if event.locations else None,
                    "locations": event.locations,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                })
                if self.gui_server is not None and event.alert_level != prev_level:
                    self.gui_server.push_event("wifi", existing)
            else:
                event_dict = {
                    "event_type": "wifi", "mac": event.mac, "score": event.score,
                    "alert_level": event.alert_level, "manufacturer": event.manufacturer,
                    "device_type": event.device_type, "mac_type": event.mac_type,
                    # Which signal(s) fired — so a soak can decompose the flag mix.
                    "score_breakdown": event.score_breakdown,
                    "first_seen": event.first_seen.isoformat(), "last_seen": event.last_seen.isoformat(),
                    "observation_count": event.observation_count,
                    "lat": event.locations[0]["lat"] if event.locations else None,
                    "lon": event.locations[0]["lon"] if event.locations else None,
                    "locations": event.locations,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                }
                self._wifi_event_index[event.mac] = event_dict
                self.all_events.append(event_dict)
                self._append_jsonl(self._session_dir / "events.jsonl", event_dict)
                if self.gui_server is not None:
                    self.gui_server.push_event("wifi", event_dict)
            if await self.rate_limiter.is_allowed(f"persist:{event.mac}"):
                self._dispatch_alert(self.alert_backend.send_persistence_alert, event)
                self._stats["alerts_sent"] += 1
            else:
                self._stats["alerts_rate_limited"] += 1
        self._write_session_summary()

    async def _poll_drone_rf(self) -> None:
        '''Drain DroneRF detections buffer; append events and fire alerts.'''
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
            alert_detection = {
                "freq_mhz": freq, "power_db": power,
                "lat": detection.get("gps_lat") or 0.0, "lon": detection.get("gps_lon") or 0.0,
            }
            if await self.rate_limiter.is_allowed(f"drone:{int(freq)}mhz"):
                self._dispatch_alert(self.alert_backend.send_drone_alert, alert_detection)
                self._stats["alerts_sent"] += 1
            else:
                self._stats["alerts_rate_limited"] += 1

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
                self._extend_track(
                    existing, detection.get("drone_lat"),
                    detection.get("drone_lon"), detection.get("drone_alt_m"), now_iso,
                )
            else:
                event = {**detection, "positions": []}
                self._extend_track(
                    event, detection.get("drone_lat"),
                    detection.get("drone_lon"), detection.get("drone_alt_m"), now_iso,
                )
                self._remote_id_index[uas_id] = event
                self.remote_id_detections.append(event)
            if await self.rate_limiter.is_allowed(f"remote_id:{uas_id}"):
                self._dispatch_alert(self.alert_backend.send_remote_id_alert, detection)
                self._stats["alerts_sent"] += 1
            else:
                self._stats["alerts_rate_limited"] += 1

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
        logger.info("ADS-B:     %s | Aircraft: %d", _status("adsb"), self._stats["aircraft_seen"])
        logger.info("DroneRF:   %s | Detections: %d", _status("drone_rf"), self._stats["drone_detections"])
        logger.info("RemoteID:  %s | Detections: %d", _status("remote_id"), self._stats["remote_id_detections"])
        logger.info("SDR:       %s | Mode: %s | Owner: %s", sdr_status, self.sdr_mode.value, self.sdr_coordinator.current_owner)
        logger.info("Alerts:    %s | Sent: %d | Rate-limited: %d | Dropped: %d", backend_name, self._stats["alerts_sent"], self._stats["alerts_rate_limited"], self._stats["alerts_dropped"])
        logger.info("Events:    %d persistent | %d aircraft | %d drone | %d remote_id", self._stats["persistent_detections"], self._stats["aircraft_seen"], self._stats["drone_detections"], self._stats["remote_id_detections"])
        logger.info(sep)

    # ------------------------------------------------------------------
    # Reconnection
    # ------------------------------------------------------------------

    async def _reconnect(self, module_name: str) -> bool:
        '''Attempt to close and reconnect a named module up to max_reconnect_attempts times.

        Returns True on success, False after exhausting all attempts.
        Supports: "gps", "kismet", "adsb", "sdr".
        '''
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
                elif module_name == "sdr":
                    try:
                        await self.sdr_coordinator.stop()
                    except Exception:
                        pass
                    await self.sdr_coordinator.start()
                    self._modules_active["sdr_coordinator"] = True
                else:
                    logger.warning("_reconnect: unknown module %r — skipping", module_name)
                    return False
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
        if pts:
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
        return True

    def _extend_aircraft_track(self, event: dict, aircraft: dict, ts_iso: str) -> bool:
        '''Extend an aircraft event's flight-path track from an ADS-B sighting.'''
        return self._extend_track(
            event, aircraft.get("lat"), aircraft.get("lon"),
            aircraft.get("altitude"), ts_iso,
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
