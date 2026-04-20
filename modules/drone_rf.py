"""Drone RF detection module — scans known drone frequencies via RTL-SDR."""

import asyncio
import logging
import os
import subprocess
from datetime import datetime, timezone
from typing import Optional

from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

# Drone command-link and video frequencies in MHz
_DRONE_FREQUENCIES_MHZ = [
    433.0,   # Hobbyist RC (worldwide)
    868.0,   # EU DJI/drone telemetry
    915.0,   # US DJI/drone telemetry
    2400.0,  # DJI OcuSync, FPV video (at edge of most RTL-SDR range)
    5800.0,  # FPV video — beyond RTL-SDR range, best-effort only
]

# Most RTL-SDR dongles (R820T/R820T2) max out around 1750 MHz.
# The E4000 chip reaches ~2200 MHz. Neither reaches 5.8 GHz.
_MAX_RTL_SDR_FREQ_MHZ = 1750.0

DRONE_POWER_THRESHOLD_DB = float(os.getenv("DRONE_POWER_THRESHOLD_DB", "-20"))

# Known RTL-SDR USB vendor:product IDs
_RTL_SDR_USB_IDS = frozenset({"0bda:2832", "0bda:2838", "0bda:2813"})

_SAMPLE_COUNT = 256 * 1024
_SAMPLE_RATE_HZ = 2.048e6


class DroneRFModule:
    """Passive RF scanner for common drone control and video link frequencies.

    Uses pyrtlsdr 0.2.93 (pinned — see requirements.txt for why).
    Sampling runs in a thread-pool executor to avoid blocking the asyncio
    event loop.

    **Hardware note:** readsb (ADS-B decoder) and DroneRFModule both require
    an RTL-SDR dongle.  If only one dongle is available, stop readsb before
    starting a drone scan, or use a second dongle.
    """

    def __init__(self, gps_module=None) -> None:
        self._gps = gps_module
        self._scan_task: Optional[asyncio.Task] = None
        self._detections: list = []
        # Set False by SDRCoordinator in SHARED mode to pause between slices
        self.can_scan: bool = True

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start_scan(self) -> None:
        """Begin background RF scanning for drone signatures.

        Gracefully logs a warning and returns if no RTL-SDR hardware is
        detected rather than raising.
        """
        if not self.is_hardware_present():
            logger.warning(
                "No RTL-SDR hardware detected — drone RF scan disabled"
            )
            return

        if self._scan_task and not self._scan_task.done():
            logger.debug("Drone RF scan already running")
            return

        self._scan_task = asyncio.create_task(self._scan_loop())
        logger.info("Drone RF scan started")

    async def stop_scan(self) -> None:
        """Stop the background RF scan and release SDR hardware."""
        if self._scan_task and not self._scan_task.done():
            self._scan_task.cancel()
            try:
                await self._scan_task
            except asyncio.CancelledError:
                pass
        self._scan_task = None
        logger.info("Drone RF scan stopped")

    # ------------------------------------------------------------------
    # Background scan loop
    # ------------------------------------------------------------------

    async def _scan_loop(self) -> None:
        """Continuously scan drone frequencies and record detections."""
        loop = asyncio.get_event_loop()
        rest_seconds = int(os.getenv("DRONE_RF_REST_SECONDS", "20"))
        max_temp_c = float(os.getenv("DRONE_RF_MAX_TEMP_C", "75"))

        while True:
            # In SHARED mode the SDRCoordinator clears can_scan between slices.
            # Pause here rather than starting a new sweep the hardware doesn't own.
            if not self.can_scan:
                try:
                    await asyncio.sleep(0.5)
                except asyncio.CancelledError:
                    raise
                continue

            for freq_mhz in _DRONE_FREQUENCIES_MHZ:
                if freq_mhz > _MAX_RTL_SDR_FREQ_MHZ:
                    logger.debug(
                        "Skipping %.1f MHz — beyond RTL-SDR hardware range", freq_mhz
                    )
                    continue
                try:
                    detection = await loop.run_in_executor(
                        None, self._sample_frequency, freq_mhz
                    )
                    if detection is not None:
                        self._detections.append(detection)
                        logger.info(
                            "Drone RF detection: %.1f MHz  %.1f dB",
                            detection["freq_mhz"], detection["power_db"],
                        )
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.debug("Scan error at %.1f MHz: %s", freq_mhz, exc)

            if rest_seconds > 0:
                actual_rest = rest_seconds
                temp = self._check_cpu_temp()
                if temp is not None and temp > max_temp_c:
                    actual_rest = rest_seconds * 2
                    logger.warning(
                        "DroneRF throttling: CPU temp %.1f°C > %.1f°C — rest extended to %ds",
                        temp, max_temp_c, actual_rest,
                    )
                logger.info("DroneRF resting %ds", actual_rest)
                await asyncio.sleep(actual_rest)
                logger.info("DroneRF resuming scan")
            else:
                await asyncio.sleep(0.1)

    def _sample_frequency(self, freq_mhz: float) -> Optional[dict]:
        """Tune to *freq_mhz*, sample, compute power; return detection or None.

        Runs in a thread pool executor (blocking pyrtlsdr call).
        """
        try:
            import numpy as np
            from rtlsdr import RtlSdr
        except ImportError as exc:
            logger.error("pyrtlsdr or numpy not available: %s", exc)
            return None

        sdr = None
        try:
            sdr = RtlSdr()
            sdr.sample_rate = _SAMPLE_RATE_HZ
            sdr.center_freq = freq_mhz * 1e6
            sdr.gain = 40

            samples = sdr.read_samples(_SAMPLE_COUNT)
            power_db = float(10 * np.log10(np.mean(np.abs(samples) ** 2) + 1e-12))
        except Exception as exc:
            logger.debug("SDR sample error at %.1f MHz: %s", freq_mhz, exc)
            return None
        finally:
            if sdr is not None:
                try:
                    sdr.close()
                except Exception:
                    pass

        if power_db < DRONE_POWER_THRESHOLD_DB:
            return None

        gps_fix = None
        if self._gps is not None:
            try:
                gps_fix = self._gps.get_fix()
            except Exception:
                pass

        return {
            "freq_mhz":  freq_mhz,
            "power_db":  power_db,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "gps_lat":   gps_fix["lat"] if gps_fix else None,
            "gps_lon":   gps_fix["lon"] if gps_fix else None,
        }

    # ------------------------------------------------------------------
    # CPU temperature
    # ------------------------------------------------------------------

    def _check_cpu_temp(self) -> Optional[float]:
        """Return CPU temperature in Celsius, or None if unavailable."""
        try:
            with open("/sys/class/thermal/thermal_zone0/temp", "r") as fh:
                return float(fh.read().strip()) / 1000.0
        except Exception:
            return None

    # ------------------------------------------------------------------
    # Hardware detection
    # ------------------------------------------------------------------

    def is_hardware_present(self) -> bool:
        """Return True if a known RTL-SDR dongle is detected via lsusb."""
        try:
            result = subprocess.run(
                ["lsusb"], capture_output=True, text=True, timeout=5
            )
            for line in result.stdout.splitlines():
                parts = line.lower().split()
                for part in parts:
                    if part in _RTL_SDR_USB_IDS:
                        logger.debug("RTL-SDR detected: %s", line.strip())
                        return True
        except Exception as exc:
            logger.debug("lsusb check failed: %s", exc)
        return False
