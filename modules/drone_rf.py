'''Drone RF scanner — listens for known drone control frequencies.

The RTL-SDR sampling runs in an **isolated child process** (#63). The Osmocom
librtlsdr / libusb stack can segfault during a USB transfer; in a single process
that native SIGSEGV kills the whole orchestrator, and systemd restarts it into a
crash loop. Behind a process boundary a native crash takes down only the child:
the parent survives, records the crash, and after too many crashes in a window
**disables** the scanner (the node stays up, drone scan off) instead of looping.

Detections flow child → parent over a multiprocessing queue; the parent enriches
them with the current GPS fix (GPS lives in the parent, not the crash-prone child).
'''

import asyncio
import os
import queue as _queue
import subprocess
import threading
import time
from datetime import datetime, timezone
from typing import Optional

from dotenv import load_dotenv

from core.logging import get_logger

load_dotenv()

logger = get_logger(__name__)

_DRONE_FREQUENCIES_MHZ = [433.0, 868.0, 915.0, 2400.0, 5800.0]
_MAX_RTL_SDR_FREQ_MHZ = 1750.0
DRONE_POWER_THRESHOLD_DB = float(os.getenv("DRONE_POWER_THRESHOLD_DB", "-20"))
_RTL_SDR_USB_IDS = frozenset({"0bda:2832", "0bda:2838", "0bda:2813"})
_SAMPLE_COUNT = 256 * 1024
_SAMPLE_RATE_HZ = 2.048e6


def _power_db(samples) -> float:
    '''Mean IQ-sample power in dB. Pure helper — unit-testable, runs in the child.'''
    import numpy as np
    return float(10 * np.log10(np.mean(np.abs(samples) ** 2) + 1e-12))


def _cpu_temp() -> Optional[float]:
    try:
        with open("/sys/class/thermal/thermal_zone0/temp", "r") as fh:
            return float(fh.read().strip()) / 1000.0
    except Exception:
        return None


def _scan_worker(detections_q, stop_evt) -> None:
    '''CHILD-PROCESS entry point: open the RTL-SDR and scan the drone bands,
    putting detection dicts (``freq_mhz``, ``power_db``, ``timestamp``) on
    *detections_q*.

    A native librtlsdr/libusb segfault during ``read_samples`` kills ONLY this
    process — the parent's monitor observes the exit and decides whether to
    respawn or disable. There is no GPS here (the parent enriches on drain).
    Returns when *stop_evt* is set.
    '''
    try:
        from rtlsdr import RtlSdr
    except Exception:
        return
    rest_seconds = int(os.getenv("DRONE_RF_REST_SECONDS", "20"))
    max_temp_c = float(os.getenv("DRONE_RF_MAX_TEMP_C", "75"))
    freqs = [f for f in _DRONE_FREQUENCIES_MHZ if f <= _MAX_RTL_SDR_FREQ_MHZ]
    sdr = None
    try:
        sdr = RtlSdr()
        sdr.sample_rate = _SAMPLE_RATE_HZ
        sdr.gain = 40
        while not stop_evt.is_set():
            for freq_mhz in freqs:
                if stop_evt.is_set():
                    break
                sdr.center_freq = freq_mhz * 1e6
                samples = sdr.read_samples(_SAMPLE_COUNT)
                power = _power_db(samples)
                if power >= DRONE_POWER_THRESHOLD_DB:
                    detections_q.put({
                        "freq_mhz": freq_mhz, "power_db": power,
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                    })
            rest = rest_seconds
            temp = _cpu_temp()
            if temp is not None and temp > max_temp_c:
                rest = rest_seconds * 2
            stop_evt.wait(rest if rest > 0 else 0.1)
    except Exception:
        # A clean Python error in the child: just exit. The parent treats any
        # unexpected exit the same as a crash (respawn/disable policy).
        pass
    finally:
        if sdr is not None:
            try:
                sdr.close()
            except Exception:
                pass


class DroneRFModule:
    '''Passive RF scanner with the SDR sampling isolated in a child process (#63).'''

    def __init__(self, gps_module=None) -> None:
        self._gps = gps_module
        self._detections: list = []
        self._lock = threading.Lock()
        self.can_scan: bool = True
        # Scan worker (child process) + parent-side monitor. ``_scan_task`` is the
        # monitor asyncio.Task — kept under that name because main.py and the SDR
        # coordinator check ``_scan_task and not _scan_task.done()`` for "running".
        self._proc = None
        self._detections_q = None
        self._stop_evt = None
        self._scan_task: Optional[asyncio.Task] = None
        # Crash-loop guard: more than _max_crashes worker deaths within
        # _crash_window_s disables the scanner rather than respawning forever.
        self._crash_times: list = []
        self._max_crashes = int(os.getenv("DRONE_RF_MAX_CRASHES", "5"))
        self._crash_window_s = float(os.getenv("DRONE_RF_CRASH_WINDOW_S", "300"))
        self._monitor_interval = float(os.getenv("DRONE_RF_MONITOR_INTERVAL_S", "1"))

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start_scan(self) -> None:
        if not self.is_hardware_present():
            logger.warning("No RTL-SDR hardware detected — drone RF scan disabled")
            return
        if not self.can_scan:
            logger.warning("Drone RF scan disabled (crash guard) — not starting")
            return
        if self._scan_task is not None and not self._scan_task.done():
            return
        self._spawn_worker()
        self._scan_task = asyncio.create_task(self._monitor_loop())
        logger.info("Drone RF scan started (isolated subprocess pid=%s)",
                    getattr(self._proc, "pid", None))

    async def stop_scan(self) -> None:
        if self._stop_evt is not None:
            try:
                self._stop_evt.set()
            except Exception:
                pass
        if self._scan_task is not None and not self._scan_task.done():
            self._scan_task.cancel()
            try:
                await self._scan_task
            except asyncio.CancelledError:
                pass
        self._scan_task = None
        self._terminate_worker()
        logger.info("Drone RF scan stopped")

    # ------------------------------------------------------------------
    # Worker-process management
    # ------------------------------------------------------------------

    def _spawn_worker(self) -> None:
        import multiprocessing as mp
        # "spawn" gives the child a clean slate — no inherited SDR/USB/asyncio state.
        ctx = mp.get_context("spawn")
        self._detections_q = ctx.Queue(maxsize=1000)
        self._stop_evt = ctx.Event()
        self._proc = ctx.Process(
            target=_scan_worker, args=(self._detections_q, self._stop_evt),
            name="dronerf-sdr", daemon=True,
        )
        self._proc.start()

    def _terminate_worker(self) -> None:
        proc = self._proc
        if proc is None:
            return
        try:
            proc.join(timeout=5)
            if proc.is_alive():
                proc.terminate()
                proc.join(timeout=2)
        except Exception:
            pass
        self._proc = None

    # ------------------------------------------------------------------
    # Monitor (parent side)
    # ------------------------------------------------------------------

    async def _monitor_loop(self) -> None:
        while True:
            status = self._monitor_tick()
            if status in ("disabled", "stopped"):
                return
            if status == "respawn":
                await asyncio.sleep(self._respawn_backoff())
                if self.can_scan and not self._stop_requested():
                    self._spawn_worker()
            await asyncio.sleep(self._monitor_interval)

    def _monitor_tick(self) -> str:
        '''One monitor iteration: drain detections, then inspect the worker.

        Returns ``"running"`` | ``"respawn"`` | ``"disabled"`` | ``"stopped"``.
        '''
        self._drain_queue()
        proc = self._proc
        if proc is None or proc.is_alive():
            return "running"
        if self._stop_requested():
            return "stopped"
        decision = self._register_crash_and_decide()
        logger.error("DroneRF worker exited unexpectedly (exitcode=%s) — %s",
                     getattr(proc, "exitcode", None), decision)
        if decision == "disable":
            self.can_scan = False
            logger.error("DroneRF disabled after %d crashes in %.0fs — orchestrator "
                         "stays up, drone scan off (see #63)",
                         self._max_crashes, self._crash_window_s)
            return "disabled"
        return "respawn"

    def _register_crash_and_decide(self) -> str:
        '''Record a worker crash and decide whether to keep trying. Pure policy:
        drop crashes outside the window, then disable once the count reaches the
        threshold.'''
        now = time.monotonic()
        self._crash_times = [t for t in self._crash_times if now - t < self._crash_window_s]
        self._crash_times.append(now)
        return "disable" if len(self._crash_times) >= self._max_crashes else "respawn"

    def _respawn_backoff(self) -> float:
        return min(2.0 ** max(0, len(self._crash_times) - 1), 30.0)

    def _stop_requested(self) -> bool:
        return self._stop_evt is not None and self._stop_evt.is_set()

    def _drain_queue(self) -> None:
        '''Move detections from the worker queue into ``_detections``, enriching
        each with the parent's current GPS fix.'''
        q = self._detections_q
        if q is None:
            return
        drained = []
        while True:
            try:
                drained.append(q.get_nowait())
            except _queue.Empty:
                break
            except Exception:
                break
        if not drained:
            return
        gps_fix = None
        if self._gps is not None:
            try:
                gps_fix = self._gps.get_fix()
            except Exception:
                gps_fix = None
        for d in drained:
            d["gps_lat"] = gps_fix["lat"] if gps_fix else None
            d["gps_lon"] = gps_fix["lon"] if gps_fix else None
        with self._lock:
            self._detections.extend(drained)

    # ------------------------------------------------------------------
    # Public helpers (unchanged surface)
    # ------------------------------------------------------------------

    def is_hardware_present(self) -> bool:
        try:
            result = subprocess.run(["lsusb"], capture_output=True, text=True, timeout=5)
            for line in result.stdout.splitlines():
                for part in line.lower().split():
                    if part in _RTL_SDR_USB_IDS:
                        return True
        except Exception:
            pass
        return False

    def drain_detections(self) -> list[dict]:
        '''Atomically return and clear the detection buffer (orchestrator API).'''
        with self._lock:
            events = self._detections.copy()
            self._detections.clear()
            logger.debug("drain_detections: %d events", len(events))
            return events
