"""Kismet module — async REST client for Wi-Fi / Bluetooth device polling."""

import logging
import os
import subprocess
from typing import Optional

import aiohttp
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

KISMET_HOST = os.getenv("KISMET_HOST", "localhost")
KISMET_PORT = int(os.getenv("KISMET_PORT", "2501"))
KISMET_API_KEY = os.getenv("KISMET_API_KEY", "")
WIFI_MONITOR_INTERFACE = os.getenv("WIFI_MONITOR_INTERFACE", "wlan1")

_BASE_URL = f"http://{KISMET_HOST}:{KISMET_PORT}"

# Fields requested from the devices endpoint — minimises response payload.
_DEVICE_FIELDS = [
    "kismet.device.base.macaddr",
    "kismet.device.base.type",
    "kismet.device.base.name",
    "kismet.device.base.manuf",
    "kismet.device.base.phyname",
    "kismet.device.base.first_time",
    "kismet.device.base.last_time",
    "kismet.device.base.signal/last_signal",
]


class KismetModule:
    """Async REST client for the Kismet sensor daemon.

    Uses API key authentication (header ``KISMET-API-Key``).  The key is
    generated once via the Kismet web UI:
    http://<pi-ip>:2501 → Settings → API Keys → Create → name: passive-vigilance

    A :class:`~modules.gps.GPSModule` instance is accepted at construction
    time; every device record returned by :meth:`poll_devices` is stamped with
    the current GPS fix.
    """

    def __init__(self, gps_module=None, ignore_list=None) -> None:
        self._gps = gps_module
        self._ignore = ignore_list
        self._session: Optional[aiohttp.ClientSession] = None

    # ------------------------------------------------------------------
    # Connection lifecycle
    # ------------------------------------------------------------------

    async def connect(self) -> None:
        """Open an aiohttp session and verify the API key against Kismet.

        Logs a warning if the WiFi monitor interface is not in monitor mode.

        Raises:
            ConnectionError: if Kismet is unreachable or the API key is invalid.
        """
        if not KISMET_API_KEY:
            raise ConnectionError(
                "KISMET_API_KEY is not set — generate one in the Kismet web UI"
            )

        # Warn if the capture interface is not in monitor mode
        status = self.get_interface_status()
        if not status["is_monitor"]:
            logger.warning(
                "Interface %s is in '%s' mode, not monitor — "
                "Kismet may not capture packets. "
                "Run: sudo ip link set %s down && "
                "sudo iw %s set monitor none && "
                "sudo ip link set %s up",
                status["interface"], status["mode"],
                status["interface"], status["interface"], status["interface"],
            )

        self._session = aiohttp.ClientSession(
            headers={"KISMET-API-Key": KISMET_API_KEY}
        )

        try:
            async with self._session.get(
                f"{_BASE_URL}/system/status.json", timeout=aiohttp.ClientTimeout(total=5)
            ) as resp:
                if resp.status == 401:
                    await self.close()
                    raise ConnectionError(
                        "Kismet rejected the API key (HTTP 401) — check KISMET_API_KEY"
                    )
                if resp.status != 200:
                    await self.close()
                    raise ConnectionError(
                        f"Kismet returned unexpected status {resp.status}"
                    )
                logger.info(
                    "Connected to Kismet at %s:%d (interface: %s)",
                    KISMET_HOST, KISMET_PORT, WIFI_MONITOR_INTERFACE,
                )
        except aiohttp.ClientConnectorError as exc:
            await self.close()
            raise ConnectionError(
                f"Cannot reach Kismet at {_BASE_URL} — is it running? ({exc})"
            ) from exc

    async def close(self) -> None:
        """Close the aiohttp session."""
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None
        logger.info("Kismet session closed")

    # ------------------------------------------------------------------
    # Device polling
    # ------------------------------------------------------------------

    async def poll_devices(self) -> list:
        """Return recently seen devices from Kismet, GPS-stamped.

        Calls ``/devices/views/all/devices.json`` with a field filter to keep
        the payload small.  Each returned dict contains:

        ``macaddr``, ``type``, ``name``, ``manuf``, ``phyname``,
        ``first_time``, ``last_time``, ``last_signal``,
        ``gps_lat``, ``gps_lon``, ``gps_utc``
        """
        if self._session is None:
            logger.warning("poll_devices() called before connect()")
            return []

        gps_fix = None
        if self._gps is not None:
            try:
                gps_fix = self._gps.get_fix()
            except Exception as exc:
                logger.debug("GPS fix unavailable: %s", exc)

        payload = {"fields": _DEVICE_FIELDS}

        try:
            async with self._session.post(
                f"{_BASE_URL}/devices/views/all/devices.json",
                json=payload,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status != 200:
                    logger.warning("Kismet devices endpoint returned %d", resp.status)
                    return []
                raw = await resp.json(content_type=None)
        except Exception as exc:
            logger.error("Error polling Kismet devices: %s", exc)
            return []

        devices = []
        ignored = 0
        for entry in raw if isinstance(raw, list) else []:
            mac  = entry.get("kismet.device.base.macaddr", "")
            ssid = entry.get("kismet.device.base.name", "")

            if self._ignore is not None:
                if mac and self._ignore.is_ignored_mac(mac):
                    ignored += 1
                    continue
                if ssid and self._ignore.is_ignored_ssid(ssid):
                    ignored += 1
                    continue

            record = {
                "macaddr":     mac,
                "type":        entry.get("kismet.device.base.type", ""),
                "name":        ssid,
                "manuf":       entry.get("kismet.device.base.manuf", ""),
                "phyname":     entry.get("kismet.device.base.phyname", ""),
                "first_time":  entry.get("kismet.device.base.first_time", 0),
                "last_time":   entry.get("kismet.device.base.last_time", 0),
                "last_signal": entry.get("kismet.device.base.signal/last_signal", None),
                "gps_lat":     gps_fix["lat"]  if gps_fix else None,
                "gps_lon":     gps_fix["lon"]  if gps_fix else None,
                "gps_utc":     gps_fix["utc"]  if gps_fix else None,
            }
            devices.append(record)

        if ignored:
            logger.debug("Ignored %d devices from Kismet (on ignore list)", ignored)
        logger.debug("Polled %d devices from Kismet", len(devices))
        return devices

    # ------------------------------------------------------------------
    # Interface status
    # ------------------------------------------------------------------

    def get_interface_status(self) -> dict:
        """Check the WiFi monitor interface mode via ``iw dev``.

        Returns a dict with keys:
        ``interface`` (str), ``mode`` (str), ``phy`` (str), ``is_monitor`` (bool)

        Returns safe defaults with ``is_monitor=False`` if the interface is
        not found or ``iw`` is unavailable.
        """
        result = {
            "interface":  WIFI_MONITOR_INTERFACE,
            "mode":       "unknown",
            "phy":        "unknown",
            "is_monitor": False,
        }

        try:
            proc = subprocess.run(
                ["iw", "dev"],
                capture_output=True, text=True, timeout=5
            )
            output = proc.stdout
        except Exception as exc:
            logger.debug("iw dev failed: %s", exc)
            return result

        # Parse iw dev output — sections delimited by "phy#N" headers
        current_phy = "unknown"
        current_iface = None
        for line in output.splitlines():
            stripped = line.strip()
            if stripped.startswith("phy#"):
                current_phy = stripped
                current_iface = None
            elif stripped.startswith("Interface "):
                current_iface = stripped.split()[1]
            elif current_iface == WIFI_MONITOR_INTERFACE and stripped.startswith("type "):
                mode = stripped.split(None, 1)[1]
                result["mode"] = mode
                result["phy"] = current_phy
                result["is_monitor"] = (mode == "monitor")
                break

        if result["mode"] == "unknown":
            logger.debug("Interface %s not found in iw dev output", WIFI_MONITOR_INTERFACE)

        return result

    # ------------------------------------------------------------------
    # WiGLE export helper
    # ------------------------------------------------------------------

    def get_wigle_csv_path(self) -> Optional[str]:
        """Return the path of the most recent Kismet WiGLE CSV log file.

        Kismet writes WiGLE CSV files to its log directory with the pattern
        ``Kismet-*.wiglecsv``.  Returns ``None`` if no file is found.
        """
        import glob

        log_dir = os.path.expanduser("~")
        patterns = [
            os.path.join(log_dir, "*.wiglecsv"),
            os.path.join(log_dir, "kismet", "*.wiglecsv"),
            "/tmp/*.wiglecsv",
        ]
        candidates = []
        for pattern in patterns:
            candidates.extend(glob.glob(pattern))

        if not candidates:
            logger.debug("No WiGLE CSV files found")
            return None

        latest = max(candidates, key=os.path.getmtime)
        logger.debug("Most recent WiGLE CSV: %s", latest)
        return latest
