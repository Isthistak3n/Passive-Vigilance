"""SDR time-share coordinator.

Only active in SHARED mode (single RTL-SDR dongle).  Alternates the
dongle between readsb (ADS-B @ 1090 MHz) and DroneRF (433/868/915 MHz)
using two configurable time slices:

  ADSB slice  — DroneRF idle, readsb running via systemctl
  DRONE slice — readsb stopped via systemctl, DroneRF scanning

The coordinator runs as an asyncio task inside the main event loop.
``start()`` performs the initial handoff (always begins with the ADSB
slice) and should be called once from ``startup()``.  The loop task
itself is created by ``main.py``'s ``event_loop()`` so that cancellation
on shutdown is handled uniformly with all other tasks.  ``stop()`` then
restores hardware to the clean state (DroneRF idle, readsb running).
"""

import asyncio
import logging
import os
import subprocess
from typing import Optional

logger = logging.getLogger(__name__)

_READSB_SERVICE = "readsb"


class SDRCoordinator:
    """Async time-share coordinator for a single RTL-SDR dongle.

    Parameters
    ----------
    drone_rf_module:
        Live :class:`~modules.drone_rf.DroneRFModule` instance.  The
        coordinator sets its ``can_scan`` flag to gate scan cycles and
        calls ``start_scan()`` / ``stop_scan()`` to drive the background
        scan task.
    """

    def __init__(self, drone_rf_module) -> None:
        self._drone_rf = drone_rf_module
        self._adsb_slice = int(os.getenv("ADSB_SLICE_SECONDS", "30"))
        self._drone_slice = int(os.getenv("DRONE_RF_SLICE_SECONDS", "30"))
        self._current_owner: str = "none"

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def current_owner(self) -> str:
        """``"adsb"``, ``"drone_rf"``, or ``"none"``."""
        return self._current_owner

    async def start(self) -> None:
        """Perform the initial handoff to the ADSB slice.

        Call once from ``startup()`` before the coordinator loop task is
        scheduled.  This ensures readsb is running and DroneRF is idle
        before the first polling cycle begins.
        """
        await self._handoff_to_adsb()
        logger.info(
            "SDR coordinator ready — ADSB=%ds / DroneRF=%ds slices",
            self._adsb_slice,
            self._drone_slice,
        )

    async def stop(self) -> None:
        """Restore hardware to clean state: DroneRF stopped, readsb running.

        Called from ``shutdown()`` after the coordinator loop task has
        already been cancelled by ``event_loop()``.
        """
        # Ensure DroneRF is fully stopped and the flag is cleared
        self._drone_rf.can_scan = False
        try:
            await self._drone_rf.stop_scan()
        except Exception as exc:
            logger.debug("SDR coordinator stop — DroneRF stop error: %s", exc)

        # Ensure readsb is running so ADS-B is available after restart
        await self._start_readsb()
        self._current_owner = "none"
        logger.info("SDR coordinator stopped — readsb restored")

    # ------------------------------------------------------------------
    # Coordinator loop (scheduled as a task by main.py event_loop)
    # ------------------------------------------------------------------

    async def _coordinator_loop(self) -> None:
        """Alternate ADSB → DRONE → ADSB indefinitely.

        The ADSB slice is already active when this loop starts (``start()``
        performed the initial handoff).  Each iteration therefore begins by
        sleeping through the remaining ADSB slice, then hands off.
        """
        while True:
            # ADSB slice is already active — sleep through it
            try:
                await asyncio.sleep(self._adsb_slice)
            except asyncio.CancelledError:
                raise

            await self._handoff_to_drone()

            try:
                await asyncio.sleep(self._drone_slice)
            except asyncio.CancelledError:
                raise

            await self._handoff_to_adsb()

    # ------------------------------------------------------------------
    # Slice handoffs
    # ------------------------------------------------------------------

    async def _handoff_to_adsb(self) -> None:
        """Stop DroneRF (gracefully), wait for SDR release, start readsb."""
        logger.info("SDR timeshare: handing off to ADS-B (readsb)")
        # Signal scan loop not to start a new sweep
        self._drone_rf.can_scan = False
        # Wait for any in-flight sample to complete, then cancel the task
        if self._drone_rf._scan_task and not self._drone_rf._scan_task.done():
            await self._drone_rf.stop_scan()
        # Brief settle — gives the USB stack time to release the device
        await asyncio.sleep(1.0)
        await self._start_readsb()
        self._current_owner = "adsb"
        logger.debug("SDR timeshare: ADS-B slice active")

    async def _handoff_to_drone(self) -> None:
        """Stop readsb, wait for SDR release, start DroneRF scanning."""
        logger.info("SDR timeshare: handing off to DroneRF")
        await self._stop_readsb()
        # Brief settle — gives readsb time to close the device
        await asyncio.sleep(1.0)
        self._drone_rf.can_scan = True
        await self._drone_rf.start_scan()
        self._current_owner = "drone_rf"
        logger.debug("SDR timeshare: DroneRF slice active")

    # ------------------------------------------------------------------
    # systemctl helpers (blocking — run in executor)
    # ------------------------------------------------------------------

    def _run_systemctl(self, action: str, service: str) -> None:
        """Run ``systemctl <action> <service>``; log but do not raise on failure."""
        try:
            result = subprocess.run(
                ["systemctl", action, service],
                capture_output=True,
                text=True,
                timeout=15,
            )
            if result.returncode != 0:
                logger.warning(
                    "systemctl %s %s exited %d: %s",
                    action, service, result.returncode, result.stderr.strip(),
                )
            else:
                logger.debug("systemctl %s %s: OK", action, service)
        except Exception as exc:
            logger.warning("systemctl %s %s failed: %s", action, service, exc)

    async def _start_readsb(self) -> None:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._run_systemctl, "start", _READSB_SERVICE)

    async def _stop_readsb(self) -> None:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._run_systemctl, "stop", _READSB_SERVICE)
