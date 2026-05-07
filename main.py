'''Passive Vigilance — asyncio orchestrator. Handles startup/shutdown; delegates polling to SensorOrchestrator.'''

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
from modules.orchestrator import SensorOrchestrator
from modules.persistence import PersistenceEngine
from modules.probe_analyzer import ProbeAnalyzer
from modules.remote_id import RemoteIDModule
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

    def __init__(self) -> None:
        self.session_id: str = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        self.session_start: datetime = datetime.now(timezone.utc)
        self._stop: asyncio.Event = asyncio.Event()
        self._modules_active: dict[str, bool] = {
            "gps": False, "kismet": False, "adsb": False,
            "drone_rf": False, "sdr_coordinator": False, "remote_id": False,
        }
        self._session_dir: Path = Path(_SESSION_OUTPUT_DIR) / self.session_id

        self.gps = GPSModule()
        self.ignore_list = IgnoreList(data_dir="data/ignore_lists")
        self.kismet = KismetModule(gps_module=self.gps, ignore_list=self.ignore_list)
        self.adsb = ADSBModule(gps_module=self.gps)
        self.drone_rf = DroneRFModule(gps_module=self.gps)
        self.sdr_coordinator: SDRCoordinator = SDRCoordinator(self.drone_rf)
        self.persistence = PersistenceEngine()
        self.probe_analyzer = ProbeAnalyzer()
        self.remote_id = RemoteIDModule(gps_module=self.gps)
        self.alert_backend = AlertFactory.get_backend(persist_path=_RATE_LIMIT_PERSIST)
        self.rate_limiter = RateLimiter(persist_path=_RATE_LIMIT_PERSIST)
        self.shapefile_writer = ShapefileWriter()
        self.wigle_uploader = WiGLEUploader()
        self.sdr_mode: SDRMode = SDRMode.AUTO
        self.gui_server: Optional["GUIServer"] = None

        self.sensor_orchestrator = SensorOrchestrator(
            gps=self.gps, kismet=self.kismet, adsb=self.adsb,
            drone_rf=self.drone_rf, sdr_coordinator=self.sdr_coordinator,
            alert_backend=self.alert_backend, rate_limiter=self.rate_limiter,
            persistence=self.persistence, probe_analyzer=self.probe_analyzer,
            gui_server=None, remote_id=self.remote_id,
            session_id=self.session_id, session_start=self.session_start,
            session_dir=self._session_dir, sdr_mode=self.sdr_mode,
            stop_event=self._stop,
            gps_poll_interval=int(os.getenv("GPS_POLL_INTERVAL_SECONDS", "1")),
            adsb_poll_interval=int(os.getenv("ADSB_POLL_INTERVAL_SECONDS", "5")),
            kismet_poll_interval=int(os.getenv("KISMET_POLL_INTERVAL_SECONDS", "30")),
            drone_poll_interval=int(os.getenv("DRONE_POLL_INTERVAL_SECONDS", "5")),
            remote_id_poll_interval=int(os.getenv("REMOTE_ID_POLL_INTERVAL_SECONDS", "5")),
            health_banner_interval=int(os.getenv("HEALTH_BANNER_INTERVAL_SECONDS", "300")),
            max_reconnect_attempts=int(os.getenv("MAX_RECONNECT_ATTEMPTS", "3")),
            reconnect_interval=int(os.getenv("RECONNECT_INTERVAL_SECONDS", "5")),
            modules_active=self._modules_active,
        )

        if _GUI_ENABLED:
            self.gui_server = GUIServer(host=_GUI_HOST, port=_GUI_PORT, orchestrator=self.sensor_orchestrator)
            self.sensor_orchestrator.gui_server = self.gui_server

    # Active-flag properties: read/write the shared _modules_active dict
    @property
    def _gps_active(self) -> bool:
        return self._modules_active["gps"]

    @_gps_active.setter
    def _gps_active(self, v: bool) -> None:
        self._modules_active["gps"] = v

    @property
    def _kismet_active(self) -> bool:
        return self._modules_active["kismet"]

    @_kismet_active.setter
    def _kismet_active(self, v: bool) -> None:
        self._modules_active["kismet"] = v

    @property
    def _adsb_active(self) -> bool:
        return self._modules_active["adsb"]

    @_adsb_active.setter
    def _adsb_active(self, v: bool) -> None:
        self._modules_active["adsb"] = v

    @property
    def _drone_active(self) -> bool:
        return self._modules_active["drone_rf"]

    @_drone_active.setter
    def _drone_active(self, v: bool) -> None:
        self._modules_active["drone_rf"] = v

    @property
    def _sdr_coordinator_active(self) -> bool:
        return self._modules_active["sdr_coordinator"]

    @_sdr_coordinator_active.setter
    def _sdr_coordinator_active(self, v: bool) -> None:
        self._modules_active["sdr_coordinator"] = v

    @property
    def _remote_id_active(self) -> bool:
        return self._modules_active["remote_id"]

    @_remote_id_active.setter
    def _remote_id_active(self, v: bool) -> None:
        self._modules_active["remote_id"] = v

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

    async def event_loop(self) -> None:
        so = self.sensor_orchestrator
        tasks = [
            asyncio.create_task(so._poll_gps_loop(), name="poll-gps"),
            asyncio.create_task(so._poll_adsb_loop(), name="poll-adsb"),
            asyncio.create_task(so._poll_kismet_loop(), name="poll-kismet"),
            asyncio.create_task(so._poll_drone_rf_loop(), name="poll-dronrf"),
            asyncio.create_task(so._poll_remote_id_loop(), name="poll-remoteid"),
            asyncio.create_task(so._health_banner_loop(), name="health-banner"),
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
                    self.sensor_orchestrator._current_fix = fix
                    self.sensor_orchestrator._gps_fix_count += 1
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

        try:
            await self.remote_id.connect()
            self._remote_id_active = True
        except Exception as exc:
            logger.warning("RemoteID: unavailable (%s) — Remote ID detection disabled", exc)

        sdr_env = os.getenv("SDR_MODE", "auto")
        _valid_sdr_modes = {"auto", "shared", "dedicated"}
        if sdr_env.strip().lower() not in _valid_sdr_modes:
            logger.warning("SDR_MODE=%r not recognised — defaulting to auto", sdr_env)
        _loop = asyncio.get_running_loop()
        sdr_count = await _loop.run_in_executor(None, detect_sdr_count)
        self.sdr_mode = resolve_sdr_mode(sdr_env, sdr_count)
        self.sensor_orchestrator.sdr_mode = self.sdr_mode

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
        close_coros = [
            ("Kismet", self.kismet.close()),
            ("readsb", self.adsb.close()),
            ("RemoteID", self.remote_id.close()),
        ]
        for label, coro in close_coros:
            try:
                await coro
            except Exception as exc:
                logger.debug("%s close error: %s", label, exc)
        try:
            self.gps.close()
        except Exception as exc:
            logger.debug("GPS close error: %s", exc)

        ev = self.sensor_orchestrator.collected_events
        all_events = ev.all_events
        aircraft_detections = ev.aircraft_detections
        drone_detections = ev.drone_detections
        remote_id_detections = ev.remote_id_detections

        end_time = datetime.now(timezone.utc)
        summary = {
            "session_id": self.session_id,
            "start_time": self.session_start.isoformat(),
            "end_time": end_time.isoformat(),
            "duration_seconds": int((end_time - self.session_start).total_seconds()),
            "gps_fixes_received": self.sensor_orchestrator._gps_fix_count,
            "unique_devices_tracked": len({e["mac"] for e in all_events}),
            "persistent_detections": len(all_events),
            "aircraft_detected": len(aircraft_detections),
            "drone_detections": len(drone_detections),
            "remote_id_detections": len(remote_id_detections),
            "modules_active": dict(self._modules_active),
        }
        self._session_dir.mkdir(parents=True, exist_ok=True)
        summary_path = self._session_dir / "summary.json"
        try:
            with open(summary_path, "w", encoding="utf-8") as fh:
                json.dump(summary, fh, indent=2, default=str)
            logger.info("Session summary written → %s", summary_path)
        except Exception as exc:
            logger.error("Failed to write session summary: %s", exc)

        all_session_events = all_events + aircraft_detections + drone_detections + remote_id_detections
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
        logger.info(
            "Session %s complete — WiFi:%d  Aircraft:%d  Drone:%d  RemoteID:%d",
            self.session_id, len(all_events), len(aircraft_detections),
            len(drone_detections), len(remote_id_detections),
        )

    def _emergency_flush(self) -> None:
        try:
            ev = self.sensor_orchestrator.collected_events
            self._session_dir.mkdir(parents=True, exist_ok=True)
            dump_path = self._session_dir / "emergency_dump.jsonl"
            total = 0
            with open(dump_path, "w", encoding="utf-8") as fh:
                for event in (
                    ev.all_events + ev.aircraft_detections
                    + ev.drone_detections + ev.remote_id_detections
                ):
                    fh.write(json.dumps(event, default=str) + "\n")
                    total += 1
            logger.error("Emergency flush: wrote %d events to %s", total, dump_path)
        except Exception as exc:
            logger.error("Emergency flush failed: %s", exc)

    def _log_startup_banner(self) -> None:
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
