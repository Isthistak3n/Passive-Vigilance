'''Drone RF scanner — listens for known drone control frequencies.

Scans common drone bands (433/868/915 MHz and higher) using RTL-SDR.
Includes P1 improvements: exponential backoff recovery and health
signaling so the SDR coordinator knows when this module is struggling.
'''

import asyncio
import logging
import os
import subprocess
from datetime import datetime, timezone
from typing import Optional

from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

_DRONE_FREQUENCIES_MHZ = [433.0, 868.0, 915.0, 2400.0, 5800.0]
_MAX_RTL_SDR_FREQ_MHZ = 1750.0
DRONE_POWER_THRESHOLD_DB = float(os.getenv("DRONE_POWER_THRESHOLD_DB", "-20"))
_RTL_SDR_USB_IDS = frozenset({"0bda:2832", "0bda:2838", "0bda:2813"})
_SAMPLE_COUNT = 256 * 1024
_SAMPLE_RATE_HZ = 2.048e6


class DroneRFModule:
    """Passive RF scanner (P1: improved recovery + health signaling)."""

    def __init__(self, gps_module=None) -> None:
        self._gps = gps_module
        self._scan_task: Optional[asyncio.Task] = None
        self._detections: list = []
        self._sdr = None
        self.can_scan: bool = True
        self._recovery_backoff: float = 1.0

    def _open_sdr(self) -> bool:
        try:
            from rtlsdr import RtlSdr
            self._sdr = RtlSdr()
            self._sdr.sample_rate = _SAMPLE_RATE_HZ
            self._sdr.gain = 40
            self._recovery_backoff = 1.0
            logger.debug("SDR device opened")
            return True
        except Exception as exc:
            logger.warning("SDR open failed: %s", exc)
            self._sdr = None
            return False

    def _close_sdr(self) -> None:
        if self._sdr is not None:
            try:
                self._sdr.close()
            except Exception:
                pass
            self._sdr = None
            logger.debug("SDR device closed")

    async def start_scan(self) -> None:
        if not self.is_hardware_present():
            logger.warning("No RTL-SDR hardware detected — drone RF scan disabled")
            return
        if self._scan_task and not self._scan_task.done():
            return
        if not self._open_sdr():
            logger.warning("Failed to open RTL-SDR device — drone RF scan disabled")
            return
        self._scan_task = asyncio.create_task(self._scan_loop())
        logger.info("Drone RF scan started")

    async def stop_scan(self) -> None:
        if self._scan_task and not self._scan_task.done():
            self._scan_task.cancel()
            try:
                await self._scan_task
            except asyncio.CancelledError:
                pass
        self._scan_task = None
        self._close_sdr()
        logger.info("Drone RF scan stopped")

    async def _scan_loop(self) -> None:
        loop = asyncio.get_event_loop()
        rest_seconds = int(os.getenv("DRONE_RF_REST_SECONDS", "20"))
        max_temp_c = float(os.getenv("DRONE_RF_MAX_TEMP_C", "75"))

        while True:
            if not self.can_scan:
                await asyncio.sleep(1)
                continue

            for freq_mhz in _DRONE_FREQUENCIES_MHZ:
                if freq_mhz > _MAX_RTL_SDR_FREQ_MHZ:
                    continue
                try:
                    detection = await loop.run_in_executor(None, self._sample_frequency, freq_mhz)
                    if detection is not None:
                        self._detections.append(detection)
                        logger.info("Drone RF detection: %.1f MHz  %.1f dB", detection["freq_mhz"], detection["power_db"])
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.warning("SDR error at %.1f MHz: %s — attempting recovery (backoff %.1fs)", freq_mhz, exc, self._recovery_backoff)
                    self._close_sdr()
                    await asyncio.sleep(self._recovery_backoff)
                    self._recovery_backoff = min(self._recovery_backoff * 2, 30.0)
                    if not self._open_sdr():
                        self.can_scan = False
                        logger.error("SDR recovery failed — disabling drone RF scan")
                    break

            if rest_seconds > 0:
                actual_rest = rest_seconds
                temp = self._check_cpu_temp()
                if temp is not None and temp > max_temp_c:
                    actual_rest = rest_seconds * 2
                    logger.warning("DroneRF throttling: CPU temp %.1f°C > %.1f°C — rest extended to %ds", temp, max_temp_c, actual_rest)
                logger.info("DroneRF resting %ds", actual_rest)
                await asyncio.sleep(actual_rest)
                logger.info("DroneRF resuming scan")
            else:
                await asyncio.sleep(0.1)

    def _process_fft_samples(self, samples):
        import numpy as np
        return float(10 * np.log10(np.mean(np.abs(samples) ** 2) + 1e-12))

    def _detect_drone_signature(self, power_db: float, freq_mhz: float) -> bool:
        return power_db >= DRONE_POWER_THRESHOLD_DB

    def _enrich_and_alert(self, power_db: float, freq_mhz: float, gps_fix) -> dict:
        return {
            "freq_mhz": freq_mhz,
            "power_db": power_db,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "gps_lat": gps_fix["lat"] if gps_fix else None,
            "gps_lon": gps_fix["lon"] if gps_fix else None,
        }

    def _sample_frequency(self, freq_mhz: float):
        if self._sdr is None:
            return None
        self._sdr.center_freq = freq_mhz * 1e6
        samples = self._sdr.read_samples(_SAMPLE_COUNT)
        power_db = self._process_fft_samples(samples)
        if not self._detect_drone_signature(power_db, freq_mhz):
            return None
        gps_fix = None
        if self._gps is not None:
            try:
                gps_fix = self._gps.get_fix()
            except Exception:
                pass
        return self._enrich_and_alert(power_db, freq_mhz, gps_fix)

    def _check_cpu_temp(self):
        try:
            with open("/sys/class/thermal/thermal_zone0/temp", "r") as fh:
                return float(fh.read().strip()) / 1000.0
        except Exception:
            return None

    def is_hardware_present(self) -> bool:
        try:
            result = subprocess.run(["lsusb"], capture_output=True, text=True, timeout=5)
            for line in result.stdout.splitlines():
                parts = line.lower().split()
                for part in parts:
                    if part in _RTL_SDR_USB_IDS:
                        return True
        except Exception:
            pass
        return False
