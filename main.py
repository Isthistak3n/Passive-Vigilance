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
from modules.ble_scanner import BLEScanner
from modules.drone_rf import DroneRFModule
from modules.ais import AISModule
from modules.acars import ACARSModule
from modules.aircraft_registry import AircraftRegistry
from modules.gps import GPSModule
from modules.ignore_list import IgnoreList
from modules.kismet import KismetModule
from modules.fixed_scoring import FixedScoring
from modules.orchestrator import SensorOrchestrator
from modules.entity_store import EntityStore
from modules.survey_store import SurveyStore
from modules.persistence import PersistenceEngine
from modules.probe_analyzer import ProbeAnalyzer
from modules.remote_id import RemoteIDModule
from modules.sdr_coordinator import SDRCoordinator
from modules.sdr_manager import SDRMode, detect_sdr_count, resolve_sdr_mode
from modules.shapefile import ShapefileWriter
from modules.wigle import WiGLEUploader

_BLE_SCANNER_ENABLED = os.getenv("BLE_SCANNER_ENABLED", "false").lower() == "true"
# AIS (marine VHF) capture — optional/best-effort, off by default (VHF won't
# receive on a 1090 antenna). The AIS-catcher decoder runs as a systemd service.
_AIS_ENABLED = os.getenv("AIS_ENABLED", "false").lower() == "true"
_AIS_SERVICE = os.getenv("AIS_SERVICE", "ais-catcher")
# ACARS (aviation VHF datalink) — optional, off by default. Decoder service invoked
# on a >30s-held ADS-B contact (single dongle) or continuously (dedicated dongle).
_ACARS_ENABLED = os.getenv("ACARS_ENABLED", "false").lower() == "true"
_ACARS_SERVICE = os.getenv("ACARS_SERVICE", "acarsdec")
_GUI_ENABLED = os.getenv("GUI_ENABLED", "false").lower() == "true"
_GUI_HOST    = os.getenv("GUI_HOST", "0.0.0.0")
_GUI_PORT    = int(os.getenv("GUI_PORT", "8080"))
# Recon-pair survey (design §5.5): the fixed node issues survey taskings and the
# mobile node offloads bed-down findings. Master switch, off by default so a node
# that doesn't participate carries zero overhead (no DB, no sync task).
_SURVEY_ENABLED = os.getenv("SURVEY_ENABLED", "false").lower() == "true"

if _GUI_ENABLED:
    from gui.server import GUIServer

_VERSION = "0.7.0-alpha"
_SESSION_OUTPUT_DIR = os.getenv("SESSION_OUTPUT_DIR", "data/sessions")
_RATE_LIMIT_PERSIST = "data/rate_limits.json"

_VALID_NODE_MODES = ("fixed", "mobile")


def resolve_node_mode(env_value: Optional[str], cli_mode: Optional[str]) -> str:
    """Resolve the node's scoring mode, failing loud rather than guessing.

    Precedence (design 2.1 — explicit, required, no silent default):
      1. ``NODE_MODE`` from the environment, if set and valid — wins.
      2. else the ``--mode`` CLI flag, if valid.
      3. else abort: the node was never told whether it is fixed or mobile.

    A present-but-invalid ``NODE_MODE`` is itself a misconfiguration and aborts;
    it does not silently fall through to the flag ("fail loud, never guess").

    Raises:
        SystemExit: when no valid mode can be resolved.
    """
    if env_value is not None and env_value.strip():
        v = env_value.strip().lower()
        if v in _VALID_NODE_MODES:
            return v
        logger.error(
            "NODE_MODE=%r is not valid — must be one of %s. Refusing to start.",
            env_value, " | ".join(_VALID_NODE_MODES),
        )
        raise SystemExit(2)

    if cli_mode is not None:
        v = cli_mode.strip().lower()
        if v in _VALID_NODE_MODES:
            return v
        logger.error(
            "--mode %r is not valid — must be one of %s. Refusing to start.",
            cli_mode, " | ".join(_VALID_NODE_MODES),
        )
        raise SystemExit(2)

    logger.error(
        "Node mode not configured. Set NODE_MODE=fixed|mobile in .env or pass "
        "--mode fixed|mobile. There is NO default — a fixed node run as mobile "
        "never alerts (#50); a mobile node run as fixed flags everything. "
        "Refusing to enter scoring under an assumed mode.",
    )
    raise SystemExit(2)


class PassiveVigilance:

    def __init__(self, cli_mode: Optional[str] = None) -> None:
        # Resolve the scoring mode first — fail loud BEFORE constructing any
        # sensor modules or the scoring engine (design 2.1). .env wins over the
        # --mode flag; an unset/invalid mode aborts startup here.
        self.node_mode: str = resolve_node_mode(os.getenv("NODE_MODE"), cli_mode)

        self.session_id: str = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        self.session_start: datetime = datetime.now(timezone.utc)
        self._stop: asyncio.Event = asyncio.Event()
        self._modules_active: dict[str, bool] = {
            "gps": False, "kismet": False, "adsb": False,
            "drone_rf": False, "sdr_coordinator": False, "remote_id": False,
            "ble": False, "ais": False, "acars": False,
        }
        self._session_dir: Path = Path(_SESSION_OUTPUT_DIR) / self.session_id

        self.gps = GPSModule()
        self.ignore_list = IgnoreList(data_dir="data/ignore_lists")
        # Auto-ignore the node's own interface MACs so it never self-detects — the
        # management WiFi sits at the sensor (max signal) and would otherwise trip
        # egregious-during-baseline and pollute the baseline. Set IGNORE_SELF_MACS=false
        # to disable. Guarded: a sysfs read failure must never block startup.
        if os.getenv("IGNORE_SELF_MACS", "true").strip().lower() != "false":
            try:
                n_self = self.ignore_list.add_self_macs()
                if n_self:
                    logger.info("Auto-ignored %d of the node's own interface MAC(s)", n_self)
            except Exception:
                logger.debug("self-MAC auto-ignore failed (non-fatal)", exc_info=True)
        # Kismet and ADS-B are GPS-stamped by the orchestrator from its own fresh
        # fix (poll_devices/poll_aircraft gps_fix=); they no longer read the
        # shared gpsd socket on the poll loop, which is what coupled the two
        # pollers and let a silent gpsd wedge both at once.
        self.kismet = KismetModule(ignore_list=self.ignore_list)
        self.adsb = ADSBModule()
        self.drone_rf = DroneRFModule(gps_module=self.gps)
        # AIS (marine VHF) — optional SDR band; None unless AIS_ENABLED. Consumes an
        # AIS-catcher JSON feed. DroneRF is retired (default off); ADS-B + AIS are the
        # active bands. The coordinator time-shares them on a single dongle (SHARED).
        self.ais = AISModule() if _AIS_ENABLED else None
        # ACARS (aviation datalink) + the connectivity-adaptive ICAO→registration
        # registry it correlates through. Both None unless ACARS_ENABLED.
        self.acars = ACARSModule() if _ACARS_ENABLED else None
        self.aircraft_registry = AircraftRegistry() if _ACARS_ENABLED else None
        self.sdr_coordinator: SDRCoordinator = SDRCoordinator(
            self.drone_rf, cycle_slices=self._derive_sdr_cycle())
        if self.ais is not None:
            self.sdr_coordinator.add_decoder_band("ais", _AIS_SERVICE, self.ais)
        if self.acars is not None:
            # Registered as a band but NOT in the default cycle — invoked only via
            # request_band_window() on a >30s-held contact.
            self.sdr_coordinator.add_decoder_band("acars", _ACARS_SERVICE, self.acars)
        # Scoring engine forked by mode: fixed = baseline-deviation (durable
        # SQLite), mobile = location-diversity (existing PersistenceEngine).
        # The orchestrator calls .update() on whichever is injected here.
        if self.node_mode == "fixed":
            self.persistence = FixedScoring()
            logger.info("NODE_MODE=fixed — baseline-deviation scoring (FixedScoring)")
        else:
            self.persistence = PersistenceEngine()
            logger.info("NODE_MODE=mobile — location-diversity scoring (PersistenceEngine)")
        self.probe_analyzer = ProbeAnalyzer()
        # Durable entity/observation store — recording runs at the poll site for
        # every NODE_MODE (orthogonal to scoring), injected into the orchestrator.
        self.entity_store = EntityStore()
        # Recon-pair survey store — on the fixed node it holds taskings + received
        # findings; on the mobile node it holds pulled taskings + survey observations.
        # None (and the whole survey path inert) unless SURVEY_ENABLED.
        self.survey_store = SurveyStore() if _SURVEY_ENABLED else None
        self.remote_id = RemoteIDModule(gps_module=self.gps)
        # Passive BLE advertisement scanner — owns hci0 (raw HCI) when enabled,
        # replacing Kismet's empty linuxbluetooth feed. Default off so non-BLE
        # nodes are unaffected; needs CAP_NET_RAW+CAP_NET_ADMIN (granted by the
        # service unit) and exclusive use of the dongle.
        self.ble_scanner = BLEScanner() if _BLE_SCANNER_ENABLED else None
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
            entity_store=self.entity_store,
            survey_store=self.survey_store,
            gui_server=None, remote_id=self.remote_id,
            ble_scanner=self.ble_scanner, ais=self.ais,
            acars=self.acars, aircraft_registry=self.aircraft_registry,
            session_id=self.session_id, session_start=self.session_start,
            session_dir=self._session_dir, sdr_mode=self.sdr_mode,
            node_mode=self.node_mode,
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
            self.gui_server = GUIServer(host=_GUI_HOST, port=_GUI_PORT,
                                        orchestrator=self.sensor_orchestrator,
                                        survey_store=self.survey_store)
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
    def _ais_active(self) -> bool:
        return self._modules_active["ais"]

    @_ais_active.setter
    def _ais_active(self, v: bool) -> None:
        self._modules_active["ais"] = v

    @property
    def _acars_active(self) -> bool:
        return self._modules_active["acars"]

    @_acars_active.setter
    def _acars_active(self, v: bool) -> None:
        self._modules_active["acars"] = v

    @property
    def _remote_id_active(self) -> bool:
        return self._modules_active["remote_id"]

    @_remote_id_active.setter
    def _remote_id_active(self, v: bool) -> None:
        self._modules_active["remote_id"] = v

    def _derive_sdr_cycle(self):
        """Build the single-dongle SDR time-share cycle from the enabled bands.

        An explicit ``SDR_CYCLE_SLICES`` env wins (returns None so the coordinator
        parses it). Otherwise: ADS-B-only when no other band is enabled (None →
        coordinator default, and we won't even start the coordinator); else a
        derived cycle — ADS-B for the bulk, then a short AIS slice (the 15-min
        default is adsb:840 + ais:60), plus a DroneRF slice only if it's re-enabled.
        """
        if (os.getenv("SDR_CYCLE_SLICES") or "").strip():
            return None
        ais_on = self.ais is not None
        acars_on = self.acars is not None
        drone_on = os.getenv("DRONE_RF_ENABLED", "false").strip().lower() in ("true", "1", "yes", "on")
        if not ais_on and not drone_on and not acars_on:
            return None  # ADS-B-only — readsb keeps the dongle, no cycle needed
        # ACARS is NOT a cycle slice — it's preemption-driven (request_band_window on a
        # >30s-held contact) — but its presence means we still run the coordinator so
        # those windows get serviced; hence a derived ADS-B slice even when acars-only.
        slices = [("adsb", int(os.getenv("ADSB_SLICE_SECONDS", "840")))]
        if ais_on:
            slices.append(("ais", int(os.getenv("AIS_SLICE_SECONDS", "60"))))
        if drone_on:
            slices.append(("drone_rf", int(os.getenv("DRONE_RF_SLICE_SECONDS", "30"))))
        return slices

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
            asyncio.create_task(so._poll_ais_loop(), name="poll-ais"),
            asyncio.create_task(so._poll_acars_loop(), name="poll-acars"),
            asyncio.create_task(so._poll_remote_id_loop(), name="poll-remoteid"),
            asyncio.create_task(so._health_banner_loop(), name="health-banner"),
            asyncio.create_task(so._watchdog_loop(), name="watchdog"),
        ]
        # Rolling-baseline adaptation sweep (P3) — only when a fixed engine opts in
        # via ADAPTATION_POSTURE; off (the default) starts no task.
        if so._adaptation_sweep_enabled():
            tasks.append(asyncio.create_task(so._adaptation_sweep_loop(), name="adaptation-sweep"))
        # Nightly sighting rollup — only when opted in via ENTITY_ROLLUP_ENABLED;
        # folds aged observation rows into per-device state off the poll path.
        if so._rollup_enabled():
            tasks.append(asyncio.create_task(so._rollup_loop(), name="sighting-rollup"))
        # Recon-pair store-and-forward sync (mobile side) — only when this node has a
        # fixed-node URL configured; the loop no-ops otherwise. Off the poll hot path.
        if so.survey.sync_configured:
            tasks.append(asyncio.create_task(so.survey.sync_loop(), name="survey-sync"))
        if self._sdr_coordinator_active:
            tasks.append(asyncio.create_task(self.sdr_coordinator._coordinator_loop(), name="sdr-coordinator"))
        await self._stop.wait()
        for task in tasks:
            task.cancel()
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for task, result in zip(tasks, results):
            if isinstance(result, Exception) and not isinstance(result, asyncio.CancelledError):
                logger.error("Task %s raised %s: %s", task.get_name(), type(result).__name__, result)

    async def _connect_kismet_with_retry(self) -> None:
        """Connect to Kismet at startup, retrying across the boot-readiness window.

        Kismet's REST API starts accepting connections several seconds after its
        systemd unit reports "active", so a single connect at startup races that
        window and silently disables WiFi/BT (and Remote ID, which shares the same
        endpoint) for the whole session — the boot race that greys out WiFi after a
        reboot until someone manually restarts the service. Retry a bounded number
        of times so a cold boot recovers on its own. Defaults span ~70s, over the
        ~60s a full stack needs to come up. A 401 means a genuinely bad API key,
        which retrying cannot fix, so stop immediately. connect() closes its own
        session on every failure path, so retrying does not leak sessions.
        """
        retries = max(1, int(os.getenv("KISMET_CONNECT_RETRIES", "15")))
        interval = float(os.getenv("KISMET_CONNECT_RETRY_INTERVAL_SECONDS", "5"))
        for attempt in range(1, retries + 1):
            try:
                await self.kismet.connect()
                self._kismet_active = True
                if attempt > 1:
                    logger.info("Kismet: connected on attempt %d/%d", attempt, retries)
                return
            except Exception as exc:
                if "401" in str(exc):
                    logger.warning("Kismet: %s — WiFi/BT capture disabled", exc)
                    return
                if attempt < retries:
                    logger.info(
                        "Kismet not ready yet (%s) — retry %d/%d in %.0fs",
                        exc, attempt, retries, interval,
                    )
                    await asyncio.sleep(interval)
        logger.warning(
            "Kismet: unavailable after %d attempts — WiFi/BT capture disabled", retries
        )

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
            _gps_deadline = _time.monotonic() + gps_timeout
            _got_fix = False
            while _time.monotonic() < _gps_deadline:
                try:
                    # Dedicated GPS pool + hard timeout, so a silent gpsd can't
                    # wedge the startup wait beyond a single read interval.
                    fix = await self.sensor_orchestrator._run_gps_call(self.gps.get_fix)
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

        await self._connect_kismet_with_retry()

        try:
            await self.remote_id.connect()
            self._remote_id_active = True
        except Exception as exc:
            logger.warning("RemoteID: unavailable (%s) — Remote ID detection disabled", exc)

        if self.ble_scanner is not None:
            # Bounded SAFE retry: set-bt-up.sh (ExecStartPre) raises a merely-down
            # controller before we start, but it can enumerate slightly late, so
            # retry the connect a few times with a short pause. Deliberately NO
            # usbreset / unbind-rebind — those kernel-oopsed a wedged Realtek
            # controller (2026-06-20); a wedged radio needs a physical replug/reboot,
            # which the startup health alert below surfaces.
            ok = False
            for attempt in range(int(os.getenv("BLE_CONNECT_RETRIES", "3"))):
                try:
                    ok = await self.ble_scanner.connect()
                    if ok:
                        break
                except Exception as exc:
                    logger.warning("BLE scanner connect attempt %d failed (%s)", attempt + 1, exc)
                await asyncio.sleep(2)
            self._modules_active["ble"] = ok
            if ok:
                logger.info("BLE scanner: passive advertisement capture active")
            else:
                logger.warning("BLE scanner: unavailable after retries — BLE capture disabled")

        sdr_env = os.getenv("SDR_MODE", "auto")
        _valid_sdr_modes = {"auto", "shared", "dedicated"}
        if sdr_env.strip().lower() not in _valid_sdr_modes:
            logger.warning("SDR_MODE=%r not recognised — defaulting to auto", sdr_env)
        _loop = asyncio.get_running_loop()
        sdr_count = await _loop.run_in_executor(None, detect_sdr_count)
        self.sdr_mode = resolve_sdr_mode(sdr_env, sdr_count)
        self.sensor_orchestrator.sdr_mode = self.sdr_mode

        drone_enabled = os.getenv("DRONE_RF_ENABLED", "false").strip().lower() in ("true", "1", "yes", "on")
        ais_enabled = self.ais is not None

        acars_enabled = self.acars is not None

        # The AIS/ACARS UDP listeners bind independently of the dongle schedule — they
        # just receive nothing while their decoder service is stopped (between slices).
        if ais_enabled:
            try:
                await self.ais.connect()
                self._ais_active = True
                logger.info("AIS: listener active (best-effort — VHF; off by default)")
            except Exception as exc:
                logger.warning("AIS: listener unavailable (%s) — AIS disabled", exc)
                self._ais_active = False
        if acars_enabled:
            try:
                await self.acars.connect()
                self._acars_active = True
                logger.info("ACARS: listener active (best-effort — VHF; off by default)")
            except Exception as exc:
                logger.warning("ACARS: listener unavailable (%s) — ACARS disabled", exc)
                self._acars_active = False

        # Bands beyond ADS-B that need the coordinator running. AIS/DroneRF take cycle
        # slices; ACARS doesn't (preemption-only) but still needs the loop alive to
        # service its windows. With none of them, readsb keeps the dongle full-time.
        extra_bands = ais_enabled or drone_enabled or acars_enabled

        if sdr_count == 0:
            logger.warning("SDR: no dongle detected — ADS-B/AIS/DroneRF disabled")
        elif self.sdr_mode == SDRMode.DEDICATED:
            logger.info("SDR mode: DEDICATED (%d dongles) — bands run simultaneously on their own dongles", sdr_count)
            try:
                await self.adsb.connect()
                self._adsb_active = True
            except Exception as exc:
                logger.warning("readsb: unavailable (%s) — ADS-B tracking disabled", exc)
            if ais_enabled:
                try:
                    await self.sdr_coordinator.start_decoder_service(_AIS_SERVICE)
                    logger.info("AIS-catcher service started (dedicated dongle)")
                except Exception as exc:
                    logger.warning("AIS-catcher: not started (%s)", exc)
            if acars_enabled:
                try:
                    await self.sdr_coordinator.start_decoder_service(_ACARS_SERVICE)
                    logger.info("ACARS decoder service started (dedicated dongle)")
                except Exception as exc:
                    logger.warning("ACARS decoder: not started (%s)", exc)
            if drone_enabled:
                try:
                    await self.drone_rf.start_scan()
                    self._drone_active = bool(self.drone_rf._scan_task and not self.drone_rf._scan_task.done())
                except Exception as exc:
                    logger.warning("DroneRF: scan not started (%s)", exc)
        elif not extra_bands:
            # SHARED, ADS-B only — readsb keeps the dongle full-time, no time-share.
            logger.info("SDR mode: SHARED (1 dongle) — ADS-B only, no time-share")
            try:
                await self.adsb.connect()
                self._adsb_active = True
            except Exception as exc:
                logger.warning("readsb: unavailable (%s) — ADS-B disabled", exc)
        else:
            # SHARED, ≥2 bands — the coordinator time-shares the dongle on the cycle.
            logger.info("SDR mode: SHARED (1 dongle) — time-share cycle: %s",
                        ", ".join(f"{b}:{s}s" for b, s in self.sdr_coordinator.slices))
            # ADS-B is enabled; the coordinator brings readsb up during its slices.
            # The startup connect() races that, so a failure must NOT grey the chiclet
            # permanently — keep adsb active and let sensor_health track liveness.
            self._adsb_active = True
            try:
                await self.adsb.connect()
            except Exception as exc:
                logger.warning("readsb: not reachable at startup (%s) — ADS-B will "
                               "come up via the SDR coordinator", exc)
            if drone_enabled:
                self.drone_rf.can_scan = False
                self._drone_active = True
            await self.sdr_coordinator.start()
            self._sdr_coordinator_active = True
            logger.info("SDR coordinator started — time-sharing active (P1 hardened)")

        if self.gui_server is not None:
            self.gui_server.start()

        self._validate_config()
        self._log_startup_banner()

        # Graceful-startup preflight: assess the radios we tried to bring up and fire
        # an operator alert for any that came up DOWN (instead of a silent warning).
        # adsb/drone_rf are "expected" whenever an SDR is configured (DRONE_RF_ENABLED)
        # OR one was detected — so a DROPPED SDR (sdr_count 0 but expected) still
        # alerts, the 2026-06-20 case. ADS-B in SHARED mode is optimistically active
        # at startup, so it only alerts if it later stays down (watchdog re-check).
        expected = {"gps", "kismet", "remote_id"}
        if self.ble_scanner is not None:
            expected.add("ble")
        if drone_enabled or sdr_count > 0:
            expected.add("adsb")
        if drone_enabled:
            expected.add("drone_rf")
        # AIS/ACARS auto-disable flips their active flag mid-run; being in the
        # expected set is what turns that flip into an operator alert.
        if self.ais is not None:
            expected.add("ais")
        if self.acars is not None:
            expected.add("acars")
        self.sensor_orchestrator.startup_health_report(expected)

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
        if self._sdr_coordinator_active:
            try:
                await self.sdr_coordinator.stop()
            except Exception as exc:
                logger.debug("SDR coordinator stop error: %s", exc)
        elif self._drone_active:
            try:
                await self.drone_rf.stop_scan()
            except Exception as exc:
                logger.debug("DroneRF stop error: %s", exc)
        if self.ais is not None:
            # In DEDICATED the AIS service runs continuously (no coordinator to stop
            # it); in SHARED the coordinator.stop() above already released the band.
            if self.sdr_mode == SDRMode.DEDICATED and self._ais_active:
                try:
                    await self.sdr_coordinator.stop_decoder_service(_AIS_SERVICE)
                except Exception as exc:
                    logger.debug("AIS-catcher stop error: %s", exc)
            try:
                await self.ais.close()
            except Exception as exc:
                logger.debug("AIS close error: %s", exc)
        if self.acars is not None:
            if self.sdr_mode == SDRMode.DEDICATED and self._acars_active:
                try:
                    await self.sdr_coordinator.stop_decoder_service(_ACARS_SERVICE)
                except Exception as exc:
                    logger.debug("ACARS decoder stop error: %s", exc)
            try:
                await self.acars.close()
            except Exception as exc:
                logger.debug("ACARS close error: %s", exc)
        if self.aircraft_registry is not None:
            try:
                self.aircraft_registry.close()
            except Exception as exc:
                logger.debug("aircraft registry close error: %s", exc)
        if self.entity_store is not None:
            try:
                # Drains the off-loop writer (if enabled) and truncates the WAL, so
                # queued observations are durable and no large WAL persists to the
                # next start (where replaying it stalls the first open).
                self.entity_store.close()
            except Exception as exc:
                logger.debug("entity store close error: %s", exc)
        if self.ble_scanner is not None:
            try:
                await self.ble_scanner.close()
            except Exception as exc:
                logger.debug("BLE scanner stop error: %s", exc)
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
            # Route the close through the dedicated GPS pool under a hard timeout
            # so a wedged gpsd socket can't hang shutdown; fall back to a direct
            # close if the orchestrator helper is unavailable.
            await self.sensor_orchestrator._run_gps_call(self.gps.close)
        except Exception as exc:
            logger.debug("GPS close error: %s", exc)
        try:
            self.sensor_orchestrator._gps_executor.shutdown(wait=False)
        except Exception as exc:
            logger.debug("GPS executor shutdown error: %s", exc)
        try:
            self.sensor_orchestrator._alert_executor.shutdown(wait=False)
        except Exception as exc:
            logger.debug("Alert executor shutdown error: %s", exc)

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
        summary_tmp = self._session_dir / "summary.json.tmp"
        try:
            # Atomic (tmp + replace), matching the incremental writer — a crash
            # mid-shutdown must not corrupt the summary the incremental path
            # kept intact all session.
            with open(summary_tmp, "w", encoding="utf-8") as fh:
                json.dump(summary, fh, indent=2, default=str)
            os.replace(summary_tmp, summary_path)
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
                data = json.loads(summary_path.read_text(encoding="utf-8"))
                data["kml_path"] = kml_path
                with open(summary_tmp, "w", encoding="utf-8") as fh:
                    json.dump(data, fh, indent=2, default=str)
                os.replace(summary_tmp, summary_path)
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
        active_parts = []
        if self._gps_active:
            active_parts.append("GPS")
        if self._kismet_active:
            active_parts.append("Kismet")
        if self._adsb_active:
            active_parts.append("ADS-B")
        if self._drone_active:
            active_parts.append("DroneRF (active)")
        if self._sdr_coordinator_active:
            active_parts.append("SDR-Coordinator (hardened)")
        if self._remote_id_active:
            active_parts.append("RemoteID")
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
    import argparse

    parser = argparse.ArgumentParser(description="Passive Vigilance sensor orchestrator")
    parser.add_argument(
        "--mode",
        choices=_VALID_NODE_MODES,
        default=None,
        help="Node scoring mode (fixed|mobile). NODE_MODE in .env takes precedence.",
    )
    args = parser.parse_args()
    orchestrator = PassiveVigilance(cli_mode=args.mode)
    asyncio.run(orchestrator.run())
