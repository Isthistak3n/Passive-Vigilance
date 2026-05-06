'''Passive Vigilance — the main orchestrator.

Coordinates GPS, Kismet, ADS-B (readsb), Drone RF, and SDR time-sharing
into one always-on asyncio event loop. This is the central brain of the
entire system.
'''

import asyncio
import json
import logging
import os
import signal
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=getattr(logging, os.getenv("LOG_LEVEL", "INFO").upper(), logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

from modules.alerts import AlertFactory, RateLimiter
from modules.dump1090 import ADSBModule
from modules.drone_rf import DroneRFModule
from modules.gps import GPSModule
from modules.ignore_list import IgnoreList
from modules.kismet import KismetModule
from modules.persistence import PersistenceEngine
from modules.probe_analyzer import ProbeAnalyzer
from modules.sdr_coordinator import SDRCoordinator
from modules.sdr_manager import SDRMode, detect_sdr_count, resolve_sdr_mode
from modules.shapefile import ShapefileWriter
from modules.wigle import WiGLEUploader

_GUI_ENABLED = os.getenv("GUI_ENABLED", "false").lower() == "true"
_GUI_HOST    = os.getenv("GUI_HOST", "0.0.0.0")
_GUI_PORT    = int(os.getenv("GUI_PORT", "8080"))

if _GUI_ENABLED:
    from gui.server import GUIServer

_VERSION = "0.4-alpha"
_SESSION_OUTPUT_DIR = os.getenv("SESSION_OUTPUT_DIR", "data/sessions")
_RATE_LIMIT_PERSIST = "data/rate_limits.json"


class PassiveVigilance:
    """Asyncio orchestrator (P1: SDR health integration)."""

    def __init__(self) -> None:
        self.session_id: str = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        self.session_start: datetime = datetime.now(timezone.utc)

        self.gps_poll_interval = int(os.getenv("GPS_POLL_INTERVAL_SECONDS", "1"))
        self.adsb_poll_interval = int(os.getenv("ADSB_POLL_INTERVAL_SECONDS", "5"))
        self.kismet_poll_interval = int(os.getenv("KISMET_POLL_INTERVAL_SECONDS", "30"))
        self.drone_poll_interval = int(os.getenv("DRONE_POLL_INTERVAL_SECONDS", "5"))

        self._stop: asyncio.Event = asyncio.Event()

        self._gps_active: bool = False
        self._kismet_active: bool = False
        self._adsb_active: bool = False
        self._drone_active: bool = False
        self._sdr_coordinator_active: bool = False

        self._sensor_health: dict[str, bool] = {
            "gps": True,
            "kismet": True,
            "adsb": True,
            "drone_rf": True,
            "sdr": True,
        }

        self._degraded_log_counter: dict[str, int] = {
            "gps": 0, "kismet": 0, "adsb": 0, "drone_rf": 0, "sdr": 0,
        }

        self._stats: dict[str, int] = {
            "kismet_devices_seen": 0,
            "aircraft_seen": 0,
            "drone_detections": 0,
            "alerts_sent": 0,
            "alerts_rate_limited": 0,
            "persistent_detections": 0,
        }

        self._max_reconnect_attempts = int(os.getenv("MAX_RECONNECT_ATTEMPTS", "3"))
        self._reconnect_interval = int(os.getenv("RECONNECT_INTERVAL_SECONDS", "5"))
        self._health_banner_interval = int(os.getenv("HEALTH_BANNER_INTERVAL_SECONDS", "300"))

        self.all_events: list[dict] = []
        self.aircraft_detections: list[dict] = []
        self.drone_detections: list[dict] = []
        self._gps_fix_count: int = 0
        self._current_fix: Optional[dict] = None

        self._session_dir: Path = Path(_SESSION_OUTPUT_DIR) / self.session_id

        self.gps = GPSModule()
        self.ignore_list = IgnoreList(data_dir="data/ignore_lists")
        self.kismet = KismetModule(gps_module=self.gps, ignore_list=self.ignore_list)
        self.adsb = ADSBModule(gps_module=self.gps)
        self.drone_rf = DroneRFModule(gps_module=self.gps)
        self.sdr_coordinator: SDRCoordinator = SDRCoordinator(self.drone_rf)
        self.persistence = PersistenceEngine()
        self.probe_analyzer = ProbeAnalyzer()
        self.alert_backend = AlertFactory.get_backend(persist_path=_RATE_LIMIT_PERSIST)
        self.rate_limiter = RateLimiter(persist_path=_RATE_LIMIT_PERSIST)
        self.shapefile_writer = ShapefileWriter()
        self.wigle_uploader = WiGLEUploader()

        self.sdr_mode: SDRMode = SDRMode.AUTO

        self.gui_server: Optional["GUIServer"] = None
        if _GUI_ENABLED:
            self.gui_server = GUIServer(host=_GUI_HOST, port=_GUI_PORT, orchestrator=self)

    async def run(self) -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, self._stop.set)

        await self.startup()
        try:
            await self.event_loop()
        except Exception as exc:
            logger.error("Unhandled exception in event loop: %s", exc, exc_info=True)
            self._emergency_flush()
        finally:
            await self.shutdown()

    async def startup(self) -> None:
        logger.info("Starting Passive Vigilance %s — session %s", _VERSION, self.session_id)

        try:
            self.gps.connect()
            self._gps_active = True
            logger.info("GPS: connected to gpsd")
        except Exception as exc:
            logger.warning("GPS: unavailable (%s) — continuing without GPS", exc)

        if self._gps_active:
            gps_timeout = int(os.getenv("GPS_STARTUP_TIMEOUT_SECONDS", "120"))
            logger.info("GPS: waiting up to %ds for first fix...", gps_timeout)
            import time as _time
            loop = asyncio.get_running_loop()
            _gps_deadline = _time.monotonic() + gps_timeout
            _got_fix = False
            while _time.monotonic() < _gps_deadline:
                try:
                    fix = await loop.run_in_executor(None, self.gps.get_fix)
                except Exception:
                    fix = None
                if fix:
                    self._current_fix = fix
                    self._gps_fix_count += 1
                    logger.info("GPS: fix acquired (%.4f, %.4f)", fix["lat"], fix["lon"])
                    _got_fix = True
                    break
                await asyncio.sleep(1)
            if not _got_fix:
                logger.warning("⚠  No GPS fix within %ds — detections will not be location-stamped", gps_timeout)

        try:
            await self.kismet.connect()
            self._kismet_active = True
        except Exception as exc:
            logger.warning("Kismet: unavailable (%s) — WiFi/BT capture disabled", exc)

        sdr_env = os.getenv("SDR_MODE", "auto")
        _valid_sdr_modes = {"auto", "shared", "dedicated"}
        if sdr_env.strip().lower() not in _valid_sdr_modes:
            logger.warning("SDR_MODE=%r not recognised — defaulting to auto", sdr_env)
        _loop = asyncio.get_running_loop()
        sdr_count = await _loop.run_in_executor(None, detect_sdr_count)
        self.sdr_mode = resolve_sdr_mode(sdr_env, sdr_count)

        if self.sdr_mode == SDRMode.DEDICATED:
            logger.info("SDR mode: DEDICATED (%d dongle(s) detected) — ADS-B and DroneRF run simultaneously", sdr_count)
            try:
                await self.adsb.connect()
                self._adsb_active = True
            except Exception as exc:
                logger.warning("readsb: unavailable (%s) — ADS-B tracking disabled", exc)
            try:
                await self.drone_rf.start_scan()
                self._drone_active = bool(self.drone_rf._scan_task and not self.drone_rf._scan_task.done())
            except Exception as exc:
                logger.warning("DroneRF: scan not started (%s)", exc)
        else:
            if sdr_count == 0:
                logger.warning("SDR mode: SHARED — no dongle detected — ADS-B and DroneRF both disabled")
            else:
                adsb_secs = int(os.getenv("ADSB_SLICE_SECONDS", "30"))
                drone_secs = int(os.getenv("DRONE_RF_SLICE_SECONDS", "30"))
                logger.info("SDR mode: SHARED (1 dongle detected) — time-sharing ADS-B (%ds) / DroneRF (%ds)", adsb_secs, drone_secs)
                try:
                    await self.adsb.connect()
                    self._adsb_active = True
                except Exception as exc:
                    logger.warning("readsb: unavailable (%s) — ADS-B disabled in SHARED mode", exc)
                self.drone_rf.can_scan = False
                self._drone_active = True
                await self.sdr_coordinator.start()
                self._sdr_coordinator_active = True
                logger.info("SDR coordinator started — time-sharing active (P1 hardened)")

        if self.gui_server is not None:
            self.gui_server.start()

        self._validate_config()
        self._log_startup_banner()

    def _validate_config(self) -> None:
        issues = []
        if not (os.getenv("NTFY_TOPIC") or os.getenv("TELEGRAM_BOT_TOKEN") or os.getenv("DISCORD_WEBHOOK_URL")):
            issues.append("Alert backend not configured — alerts will go to console only.")
        if not os.getenv("KISMET_API_KEY"):
            issues.append("Kismet API key not set — WiFi/BT capture disabled.")
        if not self._gps_active:
            issues.append("GPS not connected — detections will not be location-stamped")
        for issue in issues:
            logger.warning("⚠  CONFIG: %s", issue)

    async def event_loop(self) -> None:
        tasks = [
            asyncio.create_task(self._poll_gps_loop(), name="poll-gps"),
            asyncio.create_task(self._poll_adsb_loop(), name="poll-adsb"),
            asyncio.create_task(self._poll_kismet_loop(), name="poll-kismet"),
            asyncio.create_task(self._poll_drone_rf_loop(), name="poll-dronrf"),
            asyncio.create_task(self._health_banner_loop(), name="health-banner"),
        ]
        if self.sdr_mode == SDRMode.SHARED and self._drone_active:
            tasks.append(asyncio.create_task(self.sdr_coordinator._coordinator_loop(), name="sdr-coordinator"))
        await self._stop.wait()
        for task in tasks:
            task.cancel()
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for task, result in zip(tasks, results):
            if isinstance(result, Exception) and not isinstance(result, asyncio.CancelledError):
                logger.error("Task %s raised %s: %s", task.get_name(), type(result).__name__, result)

    async def _poll_gps_loop(self) -> None:
        while not self._stop.is_set():
            if self._gps_active:
                await self._poll_gps()
            try:
                await asyncio.sleep(self.gps_poll_interval)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error("GPS poll loop sleep error: %s", exc)

    async def _poll_gps(self) -> None:
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

    async def _poll_adsb_loop(self) -> None:
        while not self._stop.is_set():
            if self._adsb_active:
                await self._poll_adsb()
            try:
                await asyncio.sleep(self.adsb_poll_interval)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error("ADS-B poll loop sleep error: %s", exc)

    async def _poll_adsb(self) -> None:
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

    async def _poll_kismet_loop(self) -> None:
        while not self._stop.is_set():
            if self._kismet_active:
                await self._poll_kismet()
            try:
                await asyncio.sleep(self.kismet_poll_interval)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error("Kismet poll loop sleep error: %s", exc)

    async def _poll_kismet(self) -> None:
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
                "observation_count": event.observation_count, "lat": event.locations[0]["lat"] if event.locations else None,
                "lon": event.locations[0]["lon"] if event.locations else None, "locations": event.locations,
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

    async def _poll_drone_rf_loop(self) -> None:
        while not self._stop.is_set():
            if self._drone_active:
                await self._poll_drone_rf()
            try:
                await asyncio.sleep(self.drone_poll_interval)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error("Drone RF poll loop sleep error: %s", exc)

    async def _poll_drone_rf(self) -> None:
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
                "event_type": "drone", "freq_mhz": freq, "power_db": detection.get("power_db", 0.0),
                "lat": detection.get("gps_lat"), "lon": detection.get("gps_lon"),
                "timestamp": detection.get("timestamp", datetime.now(timezone.utc).isoformat()),
            }
            self.drone_detections.append(event_dict)
            self._append_jsonl(self._session_dir / "drone.jsonl", event_dict)
            if self.gui_server is not None:
                self.gui_server.push_event("drone", event_dict)
            alert_detection = {"freq_mhz": freq, "power_db": detection.get("power_db", 0.0), "lat": detection.get("gps_lat") or 0.0, "lon": detection.get("gps_lon") or 0.0}
            if await self.rate_limiter.is_allowed(f"drone:{int(freq)}mhz"):
                self.alert_backend.send_drone_alert(alert_detection)
                self._stats["alerts_sent"] += 1
            else:
                self._stats["alerts_rate_limited"] += 1

    def _write_session_summary(self) -> None:
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
                "modules_active": {
                    "gps": self._gps_active,
                    "kismet": self._kismet_active,
                    "adsb": self._adsb_active,
                    "drone_rf": self._drone_active,
                    "sdr_coordinator": self._sdr_coordinator_active,
                },
            }
            self._session_dir.mkdir(parents=True, exist_ok=True)
            summary_path = self._session_dir / "summary.json"
            tmp_path = self._session_dir / "summary.json.tmp"
            with open(tmp_path, "w", encoding="utf-8") as fh:
                json.dump(summary, fh, indent=2, default=str)
            os.replace(tmp_path, summary_path)
        except Exception as exc:
            logger.debug("Incremental summary write error: %s", exc)

    async def shutdown(self) -> None:
        logger.info("Shutdown initiated — saving session data...")
        if self.sdr_mode == SDRMode.SHARED:
            try:
                await self.sdr_coordinator.stop()
            except Exception as exc:
                logger.debug("SDR coordinator stop error: %s", exc)
        else:
            try:
                await self.drone_rf.stop_scan()
            except Exception as exc:
                logger.debug("DroneRF stop error: %s", exc)
        for label, coro in [("Kismet", self.kismet.close()), ("readsb", self.adsb.close())]:
            try:
                await coro
            except Exception as exc:
                logger.debug("%s close error: %s", label, exc)
        try:
            self.gps.close()
        except Exception as exc:
            logger.debug("GPS close error: %s", exc)
        end_time = datetime.now(timezone.utc)
        summary = {
            "session_id": self.session_id,
            "start_time": self.session_start.isoformat(),
            "end_time": end_time.isoformat(),
            "duration_seconds": int((end_time - self.session_start).total_seconds()),
            "gps_fixes_received": self._gps_fix_count,
            "unique_devices_tracked": len({e["mac"] for e in self.all_events}),
            "persistent_detections": len(self.all_events),
            "aircraft_detected": len(self.aircraft_detections),
            "drone_detections": len(self.drone_detections),
            "modules_active": {
                "gps": self._gps_active,
                "kismet": self._kismet_active,
                "adsb": self._adsb_active,
                "drone_rf": self._drone_active,
                "sdr_coordinator": self._sdr_coordinator_active,
            },
        }
        self._session_dir.mkdir(parents=True, exist_ok=True)
        summary_path = self._session_dir / "summary.json"
        try:
            with open(summary_path, "w", encoding="utf-8") as fh:
                json.dump(summary, fh, indent=2, default=str)
            logger.info("Session summary written → %s", summary_path)
        except Exception as exc:
            logger.error("Failed to write session summary: %s", exc)
        all_session_events = self.all_events + self.aircraft_detections + self.drone_detections
        kml_path = None
        if all_session_events:
            try:
                self.shapefile_writer.write_session(self.session_id, all_session_events)
                logger.info("Shapefile written successfully")
            except Exception as exc:
                logger.error("Shapefile write failed: %s", exc)
            try:
                self.shapefile_writer.write_geojson(self.session_id, all_session_events)
                kml_path = str(self._session_dir / "detections.kml")
                logger.info("GeoJSON/KML written successfully")
            except Exception as exc:
                logger.error("GeoJSON write failed: %s", exc)
        if kml_path:
            try:
                with open(summary_path, "r+", encoding="utf-8") as fh:
                    data = json.load(fh)
                    data["kml_path"] = kml_path
                    fh.seek(0)
                    json.dump(data, fh, indent=2, default=str)
                    fh.truncate()
            except Exception as exc:
                logger.debug("Could not update summary with kml_path: %s", exc)
        if self.gui_server is not None:
            self.gui_server.stop()
        try:
            if self.wigle_uploader.is_configured():
                csv_path = self.kismet.get_wigle_csv_path() or self.wigle_uploader.find_latest_csv()
                if csv_path:
                    self.wigle_uploader.upload_session(csv_path)
                    logger.info("WiGLE upload complete")
                else:
                    logger.info("WiGLE: no .wiglecsv found — skipping upload")
        except Exception as exc:
            logger.error("WiGLE upload failed (non-fatal): %s", exc)
        if kml_path:
            logger.info("KML output → %s", kml_path)
        logger.info("Session %s complete — WiFi:%d  Aircraft:%d  Drone:%d", self.session_id, len(self.all_events), len(self.aircraft_detections), len(self.drone_detections))

    def _emergency_flush(self) -> None:
        try:
            self._session_dir.mkdir(parents=True, exist_ok=True)
            dump_path = self._session_dir / "emergency_dump.jsonl"
            total = 0
            with open(dump_path, "w", encoding="utf-8") as fh:
                for event in self.all_events:
                    fh.write(json.dumps(event, default=str) + "\n")
                    total += 1
                for event in self.aircraft_detections:
                    fh.write(json.dumps(event, default=str) + "\n")
                    total += 1
                for event in self.drone_detections:
                    fh.write(json.dumps(event, default=str) + "\n")
                    total += 1
            logger.error("Emergency flush: wrote %d events to %s", total, dump_path)
        except Exception as exc:
            logger.error("Emergency flush failed: %s", exc)

    async def _health_banner_loop(self) -> None:
        while not self._stop.is_set():
            try:
                await asyncio.sleep(self._health_banner_interval)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error("Health banner loop sleep error: %s", exc)
            if not self._stop.is_set():
                self._log_health_banner()

    def _log_health_banner(self) -> None:
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
        logger.info("Kismet:  %s | Devices seen: %d", _status("kismet"), self._stats["kismet_devices_seen"])
        logger.info("ADS-B:   %s | Aircraft: %d", _status("adsb"), self._stats["aircraft_seen"])
        logger.info("DroneRF: %s | Detections: %d", _status("drone_rf"), self._stats["drone_detections"])
        logger.info("SDR:     %s | Mode: %s | Owner: %s", sdr_status, self.sdr_mode.value, self.sdr_coordinator.current_owner)
        logger.info("Alerts:  %s | Sent: %d | Rate-limited: %d", backend_name, self._stats["alerts_sent"], self._stats["alerts_rate_limited"])
        logger.info("Events:  %d persistent | %d aircraft | %d drone", self._stats["persistent_detections"], self._stats["aircraft_seen"], self._stats["drone_detections"])
        logger.info(sep)

    async def _reconnect(self, module_name):
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
                    self._sdr_coordinator_active = True
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

    def _console_alert(self, message):
        from modules.alerts import ConsoleBackend
        ConsoleBackend().send("Sensor Health", message, priority="high", tags=["sensor", "health"])

    def _append_jsonl(self, path, data):
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(data, default=str) + "\n")
        except Exception as exc:
            logger.debug("JSONL append error (%s): %s", path.name, exc)

    def _log_startup_banner(self):
        drone_status = "active" if self._drone_active else "hardware absent"
        active_parts = []
        if self._gps_active:
            active_parts.append("GPS")
        if self._kismet_active:
            active_parts.append("Kismet")
        if self._adsb_active:
            active_parts.append("ADS-B")
        active_parts.append(f"DroneRF ({drone_status})")
        if self._sdr_coordinator_active:
            active_parts.append("SDR-Coordinator (hardened)")
        backend_name = type(self.alert_backend).__name__.replace("Backend", "")
        output_dir = self._session_dir
        logger.info("=" * 60)
        logger.info("Passive Vigilance v%s — Session %s", _VERSION, self.session_id)
        logger.info("Active modules : %s", ", ".join(active_parts))
        logger.info("Alert backend  : %s", backend_name)
        logger.info("Output         : %s", output_dir)
        if self.gui_server is not None:
            logger.info("GUI            : http://%s:%d", _GUI_HOST, _GUI_PORT)
            if self.gui_server._gui_token:
                logger.info("GUI auth       : enabled (token required)")
            else:
                logger.info("GUI auth       : DISABLED — set GUI_TOKEN in .env to restrict access")
        logger.info("=" * 60)


if __name__ == "__main__":
    orchestrator = PassiveVigilance()
    asyncio.run(orchestrator.run())
