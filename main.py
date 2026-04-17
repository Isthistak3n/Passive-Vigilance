"""Passive Vigilance — asyncio orchestrator.

Wires all sensor modules into a unified always-on event loop.
Run directly (python3 main.py) or via the passive-vigilance systemd service.
"""

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

from modules.alerts import AlertFactory, RateLimiter
from modules.dump1090 import ADSBModule
from modules.drone_rf import DroneRFModule
from modules.gps import GPSModule
from modules.ignore_list import IgnoreList
from modules.kismet import KismetModule
from modules.persistence import PersistenceEngine
from modules.probe_analyzer import ProbeAnalyzer
from modules.shapefile import ShapefileWriter
from modules.wigle import WiGLEUploader

logging.basicConfig(
    level=getattr(logging, os.getenv("LOG_LEVEL", "INFO").upper(), logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

_VERSION = "0.1.0"
_SESSION_OUTPUT_DIR = os.getenv("SESSION_OUTPUT_DIR", "data/sessions")


class PassiveVigilance:
    """Asyncio orchestrator — connects all sensor modules and runs the event loop.

    Usage::

        orchestrator = PassiveVigilance()
        asyncio.run(orchestrator.run())
    """

    def __init__(self) -> None:
        self.session_id: str = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        self.session_start: datetime = datetime.now(timezone.utc)

        # Stop signal — set by SIGINT/SIGTERM handlers
        self._stop: asyncio.Event = asyncio.Event()

        # Active-module flags (set during startup)
        self._gps_active: bool = False
        self._kismet_active: bool = False
        self._adsb_active: bool = False
        self._drone_active: bool = False

        # Session accumulators
        self.all_events: list[dict] = []           # WiFi persistence detections
        self.aircraft_detections: list[dict] = []
        self.drone_detections: list[dict] = []
        self._gps_fix_count: int = 0
        self._current_fix: Optional[dict] = None

        # Modules — KismetModule receives ignore_list after IgnoreList is ready
        self.gps = GPSModule()
        self.ignore_list = IgnoreList(data_dir="data/ignore_lists")
        self.kismet = KismetModule(gps_module=self.gps, ignore_list=self.ignore_list)
        self.adsb = ADSBModule(gps_module=self.gps)
        self.drone_rf = DroneRFModule(gps_module=self.gps)
        self.persistence = PersistenceEngine()
        self.probe_analyzer = ProbeAnalyzer()
        self.alert_backend = AlertFactory.get_backend()
        self.rate_limiter = RateLimiter()
        self.shapefile_writer = ShapefileWriter()
        self.wigle_uploader = WiGLEUploader()

    # ------------------------------------------------------------------
    # Entry point
    # ------------------------------------------------------------------

    async def run(self) -> None:
        """Main entry point — register signal handlers, start up, run, shut down."""
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, self._stop.set)

        await self.startup()
        try:
            await self.event_loop()
        except Exception as exc:
            logger.error("Unhandled exception in event loop: %s", exc, exc_info=True)
        finally:
            await self.shutdown()

    # ------------------------------------------------------------------
    # Startup
    # ------------------------------------------------------------------

    async def startup(self) -> None:
        """Connect all modules. Failures are logged as warnings and skipped."""
        logger.info("Starting Passive Vigilance %s — session %s", _VERSION, self.session_id)

        # GPS
        try:
            self.gps.connect()
            self._gps_active = True
            logger.info("GPS: connected to gpsd")
        except Exception as exc:
            logger.warning("GPS: unavailable (%s) — continuing without GPS", exc)

        # Kismet
        try:
            await self.kismet.connect()
            self._kismet_active = True
        except Exception as exc:
            logger.warning("Kismet: unavailable (%s) — WiFi/BT capture disabled", exc)

        # readsb / ADS-B
        try:
            await self.adsb.connect()
            self._adsb_active = True
        except Exception as exc:
            logger.warning("readsb: unavailable (%s) — ADS-B tracking disabled", exc)

        # Drone RF — start_scan() is self-guarding; logs a warning if no hardware
        try:
            await self.drone_rf.start_scan()
            self._drone_active = bool(
                self.drone_rf._scan_task and not self.drone_rf._scan_task.done()
            )
        except Exception as exc:
            logger.warning("DroneRF: scan not started (%s)", exc)

        self._log_startup_banner()

    # ------------------------------------------------------------------
    # Event loop
    # ------------------------------------------------------------------

    async def event_loop(self) -> None:
        """Run all polling loops concurrently until the stop event fires."""
        tasks = [
            asyncio.create_task(self._poll_gps_loop(), name="poll-gps"),
            asyncio.create_task(self._poll_adsb_loop(), name="poll-adsb"),
            asyncio.create_task(self._poll_kismet_loop(), name="poll-kismet"),
            asyncio.create_task(self._poll_drone_rf_loop(), name="poll-dronrf"),
        ]
        await self._stop.wait()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    # ------------------------------------------------------------------
    # GPS polling — every 1 second
    # ------------------------------------------------------------------

    async def _poll_gps_loop(self) -> None:
        while not self._stop.is_set():
            if self._gps_active:
                await self._poll_gps()
            await asyncio.sleep(1)

    async def _poll_gps(self) -> None:
        loop = asyncio.get_running_loop()
        try:
            fix = await loop.run_in_executor(None, self.gps.get_fix)
        except Exception as exc:
            logger.debug("GPS poll error: %s", exc)
            return

        had_fix = self._current_fix is not None
        self._current_fix = fix

        if fix:
            self._gps_fix_count += 1
            if not had_fix:
                logger.info("GPS: fix acquired (%.4f, %.4f)", fix["lat"], fix["lon"])
        elif had_fix:
            logger.warning("GPS: fix lost")

    # ------------------------------------------------------------------
    # ADS-B polling — every 5 seconds
    # ------------------------------------------------------------------

    async def _poll_adsb_loop(self) -> None:
        while not self._stop.is_set():
            if self._adsb_active:
                await self._poll_adsb()
            await asyncio.sleep(5)

    async def _poll_adsb(self) -> None:
        try:
            aircraft_list = await self.adsb.poll_aircraft()
        except Exception as exc:
            logger.debug("ADS-B poll error: %s", exc)
            return

        for aircraft in aircraft_list:
            icao = aircraft.get("icao", "unknown")
            event = {
                **aircraft,
                "event_type": "aircraft",
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
            self.aircraft_detections.append(event)

            emergency = aircraft.get("emergency", False)
            if emergency:
                # Emergency: bypass rate limiter and alert immediately
                self.alert_backend.send_aircraft_alert(aircraft)
                logger.warning("EMERGENCY aircraft: icao=%s callsign=%s", icao, aircraft.get("callsign", ""))
            else:
                if self.rate_limiter.is_allowed(f"aircraft:{icao}"):
                    self.alert_backend.send_aircraft_alert(aircraft)

    # ------------------------------------------------------------------
    # Kismet / WiFi+BT polling — every 30 seconds
    # ------------------------------------------------------------------

    async def _poll_kismet_loop(self) -> None:
        while not self._stop.is_set():
            if self._kismet_active:
                await self._poll_kismet()
            await asyncio.sleep(30)

    async def _poll_kismet(self) -> None:
        try:
            devices = await self.kismet.poll_devices()
        except Exception as exc:
            logger.debug("Kismet poll error: %s", exc)
            return

        # Probe pattern analysis
        suspicious = self.probe_analyzer.analyze(devices)
        if suspicious:
            logger.info("ProbeAnalyzer: %d suspicious probe pattern(s) detected", len(suspicious))

        # Persistence scoring
        try:
            detection_events = self.persistence.update(devices, gps_fix=self._current_fix)
        except Exception as exc:
            logger.debug("PersistenceEngine update error: %s", exc)
            return

        for event in detection_events:
            lat = event.locations[0]["lat"] if event.locations else None
            lon = event.locations[0]["lon"] if event.locations else None
            event_dict = {
                "event_type":       "wifi",
                "mac":              event.mac,
                "score":            event.score,
                "alert_level":      event.alert_level,
                "manufacturer":     event.manufacturer,
                "device_type":      event.device_type,
                "first_seen":       event.first_seen.isoformat(),
                "last_seen":        event.last_seen.isoformat(),
                "observation_count": event.observation_count,
                "lat":              lat,
                "lon":              lon,
                "timestamp":        datetime.now(timezone.utc).isoformat(),
            }
            self.all_events.append(event_dict)

            if self.rate_limiter.is_allowed(f"persist:{event.mac}"):
                self.alert_backend.send_persistence_alert(event)
                logger.info(
                    "Persistence alert: mac=%s score=%.2f level=%s",
                    event.mac, event.score, event.alert_level,
                )

    # ------------------------------------------------------------------
    # Drone RF — drain detections every 5 seconds
    # ------------------------------------------------------------------

    async def _poll_drone_rf_loop(self) -> None:
        while not self._stop.is_set():
            if self._drone_active:
                await self._poll_drone_rf()
            await asyncio.sleep(5)

    async def _poll_drone_rf(self) -> None:
        # Drain in one shot to minimise race with the scan thread
        pending = self.drone_rf._detections[:]
        self.drone_rf._detections.clear()

        for detection in pending:
            freq = detection.get("freq_mhz", 0)
            event_dict = {
                "event_type": "drone",
                "freq_mhz":   freq,
                "power_db":   detection.get("power_db", 0.0),
                "lat":        detection.get("gps_lat"),
                "lon":        detection.get("gps_lon"),
                "timestamp":  detection.get("timestamp", datetime.now(timezone.utc).isoformat()),
            }
            self.drone_detections.append(event_dict)

            alert_detection = {
                "freq_mhz": freq,
                "power_db": detection.get("power_db", 0.0),
                "lat":      detection.get("gps_lat") or 0.0,
                "lon":      detection.get("gps_lon") or 0.0,
            }
            if self.rate_limiter.is_allowed(f"drone:{int(freq)}mhz"):
                self.alert_backend.send_drone_alert(alert_detection)
                logger.info("Drone RF alert: %.1f MHz  %.1f dBm", freq, detection.get("power_db", 0))

    # ------------------------------------------------------------------
    # Shutdown
    # ------------------------------------------------------------------

    async def shutdown(self) -> None:
        """Disconnect modules, write session summary, output shapefiles, upload to WiGLE."""
        logger.info("Shutdown initiated — saving session data...")

        # Stop drone RF scan
        try:
            await self.drone_rf.stop_scan()
        except Exception as exc:
            logger.debug("DroneRF stop error: %s", exc)

        # Disconnect async modules
        for label, coro in [("Kismet", self.kismet.close()), ("readsb", self.adsb.close())]:
            try:
                await coro
            except Exception as exc:
                logger.debug("%s close error: %s", label, exc)

        # Disconnect sync GPS
        try:
            self.gps.close()
        except Exception as exc:
            logger.debug("GPS close error: %s", exc)

        end_time = datetime.now(timezone.utc)

        # Write session summary
        summary = {
            "session_id":            self.session_id,
            "start_time":            self.session_start.isoformat(),
            "end_time":              end_time.isoformat(),
            "duration_seconds":      int((end_time - self.session_start).total_seconds()),
            "gps_fixes_received":    self._gps_fix_count,
            "unique_devices_tracked": len({e["mac"] for e in self.all_events}),
            "persistent_detections": len(self.all_events),
            "aircraft_detected":     len(self.aircraft_detections),
            "drone_detections":      len(self.drone_detections),
            "modules_active": {
                "gps":      self._gps_active,
                "kismet":   self._kismet_active,
                "adsb":     self._adsb_active,
                "drone_rf": self._drone_active,
            },
        }

        session_dir = Path(_SESSION_OUTPUT_DIR) / self.session_id
        session_dir.mkdir(parents=True, exist_ok=True)
        summary_path = session_dir / "summary.json"
        with open(summary_path, "w", encoding="utf-8") as fh:
            json.dump(summary, fh, indent=2, default=str)
        logger.info("Session summary → %s", summary_path)

        # Write shapefiles / GeoJSON
        all_session_events = self.all_events + self.aircraft_detections + self.drone_detections
        if all_session_events:
            try:
                self.shapefile_writer.write_session(self.session_id, all_session_events)
                self.shapefile_writer.write_geojson(self.session_id, all_session_events)
            except Exception as exc:
                logger.error("GIS output error: %s", exc)

        # WiGLE upload
        if self.wigle_uploader.is_configured():
            csv_path = self.kismet.get_wigle_csv_path() or self.wigle_uploader.find_latest_csv()
            if csv_path:
                self.wigle_uploader.upload_session(csv_path)
            else:
                logger.info("WiGLE: no .wiglecsv found — skipping upload")

        logger.info(
            "Session %s complete — WiFi:%d  Aircraft:%d  Drone:%d",
            self.session_id,
            len(self.all_events),
            len(self.aircraft_detections),
            len(self.drone_detections),
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

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

        backend_name = type(self.alert_backend).__name__.replace("Backend", "")
        output_dir = Path(_SESSION_OUTPUT_DIR) / self.session_id

        logger.info("=" * 60)
        logger.info("Passive Vigilance v%s — Session %s", _VERSION, self.session_id)
        logger.info("Active modules : %s", ", ".join(active_parts))
        logger.info("Alert backend  : %s", backend_name)
        logger.info("Output         : %s", output_dir)
        logger.info("=" * 60)


if __name__ == "__main__":
    orchestrator = PassiveVigilance()
    asyncio.run(orchestrator.run())
