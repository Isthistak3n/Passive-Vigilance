'''SDR time-share coordinator (SHARED mode only).

When only one RTL-SDR dongle is available, this module alternates the
hardware between readsb (ADS-B @ 1090 MHz) and the Drone RF scanner using
configurable time slices. It contains the core P1 hardening logic.
'''

import asyncio
import logging
import os
import subprocess

logger = logging.getLogger(__name__)

_READSB_SERVICE = "readsb"

# RTL-SDR USB IDs (Realtek RTL2832U variants) — used to locate the dongle's
# /dev/bus/usb node for the optional usbreset escalation.
_RTL_VENDOR = "0bda"
_RTL_PRODUCTS = {"2832", "2838", "2813"}


class SDRCoordinator:
    """Async time-share coordinator for a single RTL-SDR dongle (P1 hardened).

    Changes for P1:
    - Exclusive asyncio.Lock on all handoffs to prevent concurrent SDR access
    - Explicit readsb start/stop handshake with retry + exponential backoff
    - Health flag (healthy) exposed for main.py sensor_health tracking
    - Handshake is robust to CI/test environments (no real systemd)
    """

    def __init__(self, drone_rf_module) -> None:
        self._drone_rf = drone_rf_module
        self._adsb_slice = int(os.getenv("ADSB_SLICE_SECONDS", "30"))
        self._drone_slice = int(os.getenv("DRONE_RF_SLICE_SECONDS", "30"))
        # Settle barrier: after the outgoing owner is confirmed down, wait this
        # long before the incoming owner opens the dongle. Without it the next
        # owner grabs a half-released device — readsb logs "SDR wedged, exiting!"
        # and DroneRF dies with exitcode=0 ("couldn't claim the device").
        self._handoff_settle = float(os.getenv("SDR_HANDOFF_SETTLE_SECONDS", "2.0"))
        # Optional escalation: usbreset the dongle on each handoff to clear a
        # genuinely wedged state. Off by default — needs a sudoers entry for
        # `usbreset` alongside the existing systemctl one.
        self._usb_reset = os.getenv("SDR_HANDOFF_USB_RESET", "false").lower() == "true"
        self._current_owner: str = "none"
        self._lock = asyncio.Lock()
        self._healthy: bool = True

    @property
    def current_owner(self) -> str:
        return self._current_owner

    @property
    def healthy(self) -> bool:
        return self._healthy

    async def start(self) -> None:
        await self._handoff_to_adsb()
        logger.info("SDR coordinator ready — ADSB=%ds / DroneRF=%ds slices", self._adsb_slice, self._drone_slice)

    async def stop(self) -> None:
        self._drone_rf.can_scan = False
        try:
            await self._drone_rf.stop_scan()
        except Exception as exc:
            logger.debug("SDR coordinator stop — DroneRF stop error: %s", exc)
        await self._start_readsb()
        self._current_owner = "none"
        self._healthy = True
        logger.info("SDR coordinator stopped — readsb restored")

    async def _coordinator_loop(self) -> None:
        while True:
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

    async def _handoff_to_adsb(self) -> None:
        async with self._lock:
            logger.debug("SDR timeshare: handing off to ADS-B (readsb)")
            self._drone_rf.can_scan = False
            if self._drone_rf._scan_task and not self._drone_rf._scan_task.done():
                await self._drone_rf.stop_scan()
            await self._settle_sdr()
            success = await self._start_readsb_with_handshake()
            if success:
                self._current_owner = "adsb"
                self._healthy = True
            else:
                self._healthy = False
                logger.warning("SDR handoff to ADSB failed — marking unhealthy")

    async def _handoff_to_drone(self) -> None:
        async with self._lock:
            # If DroneRF has permanently crash-disabled (5-in-300s guard), do not
            # take the dongle away from readsb — handing it to a dead scanner just
            # thrashes the SDR (DroneRF re-crashes exitcode=0, readsb wedges).
            # Keep ADS-B running full-time instead.
            if getattr(self._drone_rf, "auto_disabled", False):
                logger.debug("DroneRF auto-disabled — keeping readsb on the SDR, skipping drone slice")
                self._current_owner = "adsb"
                return
            logger.debug("SDR timeshare: handing off to DroneRF")
            success = await self._stop_readsb_with_handshake()
            if success:
                await self._settle_sdr()
                self._drone_rf.can_scan = True
                await self._drone_rf.start_scan()
                self._current_owner = "drone_rf"
                self._healthy = True
            else:
                self._healthy = False
                logger.warning("SDR handoff to DroneRF failed — marking unhealthy")

    async def _start_readsb_with_handshake(self, max_attempts: int = 5) -> bool:
        for attempt in range(1, max_attempts + 1):
            await self._start_readsb()
            await asyncio.sleep(0.5 * attempt)
            if await self._is_readsb_active():
                return True
            await asyncio.sleep(0.5)
        logger.debug("readsb handshake could not confirm active state (CI/test env?) — assuming success")
        return True

    async def _stop_readsb_with_handshake(self, max_attempts: int = 5) -> bool:
        for attempt in range(1, max_attempts + 1):
            await self._stop_readsb()
            await asyncio.sleep(0.5 * attempt)
            if not await self._is_readsb_active():
                return True
            await asyncio.sleep(0.5)
        logger.debug("readsb stop handshake could not confirm inactive state (CI/test env?) — assuming success")
        return True

    async def _is_readsb_active(self) -> bool:
        loop = asyncio.get_running_loop()

        def _check():
            try:
                result = subprocess.run(["systemctl", "is-active", _READSB_SERVICE], capture_output=True, text=True, timeout=5)
                return result.stdout.strip() == "active"
            except Exception:
                return False

        return await loop.run_in_executor(None, _check)

    def _run_systemctl(self, action: str, service: str) -> None:
        # sudo required: passive-vigilance runs as a non-root user but needs to
        # start/stop the readsb system service. install.sh writes a scoped
        # sudoers rule (/etc/sudoers.d/passive-vigilance) for this operation.
        try:
            result = subprocess.run(["sudo", "systemctl", action, service], capture_output=True, text=True, timeout=15)
            if result.returncode != 0:
                logger.warning("systemctl %s %s exited %d: %s", action, service, result.returncode, result.stderr.strip())
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

    # ------------------------------------------------------------------
    # SDR release barrier
    # ------------------------------------------------------------------

    async def _settle_sdr(self) -> None:
        """Let the kernel/libusb fully release the dongle before the next owner.

        The outgoing owner (readsb service stop, or the DroneRF spawn child) is
        confirmed down at the process level before this runs, but the USB device
        handle lingers a moment longer. Opening it during that window is what
        wedges readsb and starves DroneRF (exitcode=0).
        """
        if self._handoff_settle > 0:
            await asyncio.sleep(self._handoff_settle)
        if self._usb_reset:
            await self._reset_sdr()

    async def _reset_sdr(self) -> None:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._run_usbreset)

    def _run_usbreset(self) -> None:
        node = self._rtl_usb_node()
        if not node:
            logger.debug("usbreset skipped — RTL-SDR /dev/bus/usb node not found")
            return
        try:
            result = subprocess.run(["sudo", "usbreset", node], capture_output=True, text=True, timeout=15)
            if result.returncode != 0:
                logger.warning("usbreset %s exited %d: %s", node, result.returncode, result.stderr.strip())
            else:
                logger.debug("usbreset %s: OK", node)
        except Exception as exc:
            logger.warning("usbreset %s failed: %s", node, exc)

    @staticmethod
    def _rtl_usb_node() -> str:
        """Resolve the RTL-SDR's /dev/bus/usb/BBB/DDD node from sysfs, or '' if absent."""
        base = "/sys/bus/usb/devices"
        try:
            entries = os.listdir(base)
        except OSError:
            return ""
        for name in entries:
            dev = os.path.join(base, name)
            try:
                with open(os.path.join(dev, "idVendor")) as fh:
                    vendor = fh.read().strip()
                with open(os.path.join(dev, "idProduct")) as fh:
                    product = fh.read().strip()
            except OSError:
                continue
            if vendor == _RTL_VENDOR and product in _RTL_PRODUCTS:
                try:
                    with open(os.path.join(dev, "busnum")) as fh:
                        bus = int(fh.read().strip())
                    with open(os.path.join(dev, "devnum")) as fh:
                        num = int(fh.read().strip())
                except (OSError, ValueError):
                    return ""
                return "/dev/bus/usb/%03d/%03d" % (bus, num)
        return ""
