'''SensorOrchestrator — polling loops and health tracking for Passive Vigilance.

Owns all sensor poll loops, health state, reconnection logic, and accumulated
session event lists. PassiveVigilance constructs this and delegates all
polling/health concerns to it.
'''

import asyncio
import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


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
        self._stats: dict[str, int] = {
            "kismet_devices_seen": 0,
            "aircraft_seen": 0,
            "drone_detections": 0,
            "remote_id_detections": 0,
            "alerts_sent": 0,
            "alerts_rate_limited": 0,
            "persistent_detections": 0,
        }

        self.all_events: list[dict] = []
        self.aircraft_detections: list[dict] = []
        self.drone_detections: list[dict] = []
        self.remote_id_detections: list[dict] = []
        self._current_fix: Optional[dict] = None
        self._gps_fix_count: int = 0

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

    async def _poll_gps(self) -> None:
        '''Read one GPS fix; update _current_fix and health state.'''
        loop = asyncio.get_running_loop()
        try:
            fix = await loop.run_in_executor(None, self.gps.get_fix)
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
            aircraft_list = await self.adsb.poll_aircraft()
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
        for aircraft in aircraft_list:
            self._stats["aircraft_seen"] += 1
            event = {**aircraft, "event_type": "aircraft", "timestamp": datetime.now(timezone.utc).isoformat()}
            self.aircraft_detections.append(event)
            self._append_jsonl(self._session_dir / "aircraft.jsonl", event)
            if self.gui_server is not None:
                self.gui_server.push_event("aircraft", event)
            emergency = aircraft.get("emergency", False)
            if emergency:
                self.alert_backend.send_aircraft_alert(aircraft)
                self._stats["alerts_sent"] += 1
            else:
                if await self.rate_limiter.is_allowed(f"aircraft:{aircraft.get('icao', 'unknown')}"):
                    self.alert_backend.send_aircraft_alert(aircraft)
                    self._stats["alerts_sent"] += 1
                else:
                    self._stats["alerts_rate_limited"] += 1
        self._write_session_summary()

    async def _poll_kismet(self) -> None:
        '''Poll Kismet for WiFi/BT devices; run persistence engine; fire alerts.'''
        try:
            devices = await self.kismet.poll_devices()
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
            event_dict = {
                "event_type": "wifi", "mac": event.mac, "score": event.score,
                "alert_level": event.alert_level, "manufacturer": event.manufacturer,
                "device_type": event.device_type, "mac_type": event.mac_type,
                "first_seen": event.first_seen.isoformat(), "last_seen": event.last_seen.isoformat(),
                "observation_count": event.observation_count,
                "lat": event.locations[0]["lat"] if event.locations else None,
                "lon": event.locations[0]["lon"] if event.locations else None,
                "locations": event.locations,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
            self.all_events.append(event_dict)
            self._append_jsonl(self._session_dir / "events.jsonl", event_dict)
            if self.gui_server is not None:
                self.gui_server.push_event("wifi", event_dict)
            if await self.rate_limiter.is_allowed(f"persist:{event.mac}"):
                self.alert_backend.send_persistence_alert(event)
                self._stats["alerts_sent"] += 1
            else:
                self._stats["alerts_rate_limited"] += 1
        self._write_session_summary()

    async def _poll_drone_rf(self) -> None:
        '''Drain DroneRF detections buffer; append events and fire alerts.'''
        try:
            pending = self.drone_rf._detections[:]
            self.drone_rf._detections.clear()
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
        for detection in pending:
            freq = detection.get("freq_mhz", 0)
            self._stats["drone_detections"] += 1
            event_dict = {
                "event_type": "drone", "freq_mhz": freq,
                "power_db": detection.get("power_db", 0.0),
                "lat": detection.get("gps_lat"), "lon": detection.get("gps_lon"),
                "timestamp": detection.get("timestamp", datetime.now(timezone.utc).isoformat()),
            }
            self.drone_detections.append(event_dict)
            self._append_jsonl(self._session_dir / "drone.jsonl", event_dict)
            if self.gui_server is not None:
                self.gui_server.push_event("drone", event_dict)
            alert_detection = {
                "freq_mhz": freq, "power_db": detection.get("power_db", 0.0),
                "lat": detection.get("gps_lat") or 0.0, "lon": detection.get("gps_lon") or 0.0,
            }
            if await self.rate_limiter.is_allowed(f"drone:{int(freq)}mhz"):
                self.alert_backend.send_drone_alert(alert_detection)
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
        for detection in detections:
            self._stats["remote_id_detections"] += 1
            self.remote_id_detections.append(detection)
            self._append_jsonl(self._session_dir / "remote_id.jsonl", detection)
            uas_id = detection.get("uas_id") or "unknown"
            if await self.rate_limiter.is_allowed(f"remote_id:{uas_id}"):
                self.alert_backend.send_remote_id_alert(detection)
                self._stats["alerts_sent"] += 1
            else:
                self._stats["alerts_rate_limited"] += 1

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
        logger.info("── Passive Vigilance Health ───────────────────────")
        logger.info("Session: %s | Uptime: %s", self.session_id, uptime_str)
        logger.info("GPS:     %s | %s", gps_status, gps_loc)
        logger.info("Kismet:    %s | Devices seen: %d", _status("kismet"), self._stats["kismet_devices_seen"])
        logger.info("ADS-B:     %s | Aircraft: %d", _status("adsb"), self._stats["aircraft_seen"])
        logger.info("DroneRF:   %s | Detections: %d", _status("drone_rf"), self._stats["drone_detections"])
        logger.info("RemoteID:  %s | Detections: %d", _status("remote_id"), self._stats["remote_id_detections"])
        logger.info("SDR:       %s | Mode: %s | Owner: %s", sdr_status, self.sdr_mode.value, self.sdr_coordinator.current_owner)
        logger.info("Alerts:    %s | Sent: %d | Rate-limited: %d", backend_name, self._stats["alerts_sent"], self._stats["alerts_rate_limited"])
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
                    loop = asyncio.get_running_loop()
                    try:
                        await loop.run_in_executor(None, self.gps.close)
                    except Exception:
                        pass
                    await loop.run_in_executor(None, self.gps.connect)
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
