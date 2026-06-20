"""Kismet integration — async client for Wi-Fi and Bluetooth device polling.

Talks to the Kismet sensor daemon over its REST API and returns
GPS-stamped device records. Also handles monitor-mode interface checks.
"""

import logging
import os
import subprocess
import time
from typing import Optional

import aiohttp
from dotenv import load_dotenv

from modules.mac_utils import get_mac_type, is_randomized_mac

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
    "kismet.device.base.signal/kismet.common.signal.last_signal",
    "dot11.device/dot11.device.probed_ssid_map",
    "dot11.device/dot11.device.probe_fingerprint",
    "dot11.device/dot11.device.num_probed_ssids",
]


def _extract_probe_ssids(probed_map) -> list:
    """Return the unique, non-empty SSIDs a client is probing for.

    ``probed_map`` is Kismet's ``dot11.device.probed_ssid_map`` value — a list of
    records, each with the SSID string at ``dot11.probedssid.ssid``. The ""
    (and whitespace-only) entry is the broadcast/wildcard probe and is excluded.
    Order of first appearance is preserved; duplicates are dropped. A missing or
    non-list value yields an empty list.
    """
    ssids: list = []
    seen: set = set()
    if not isinstance(probed_map, list):
        return ssids
    for rec in probed_map:
        if not isinstance(rec, dict):
            continue
        ssid = rec.get("dot11.probedssid.ssid")
        if not isinstance(ssid, str) or not ssid.strip():
            continue
        if ssid in seen:
            continue
        seen.add(ssid)
        ssids.append(ssid)
    return ssids


class KismetModule:
    """Async REST client for the Kismet sensor daemon.

    Uses API key authentication (header ``KISMET-API-Key``).  The key is
    generated once via the Kismet web UI:
    http://<pi-ip>:2501 → Settings → API Keys → Create → name: passive-vigilance

    Device records are GPS-stamped by the orchestrator, which passes the
    current fix into :meth:`poll_devices` (``gps_fix=``). The module performs
    no GPS reads of its own — sharing the gpsd socket on the poll loop is what
    coupled the WiFi and ADS-B pollers and let a silent gpsd wedge both at once.

    ``gps_module`` is still accepted at construction for backward compatibility
    with older callers/tests, but it is no longer read.
    """

    def __init__(self, gps_module=None, ignore_list=None) -> None:
        # Retained for backward-compatible construction only; the module no
        # longer reads GPS itself (see poll_devices' gps_fix argument).
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

        # Kismet 2025.09+ requires the API key as a KISMET cookie rather than
        # the KISMET-API-Key header (header auth was removed in 2025.09).
        self._session = aiohttp.ClientSession(
            cookies={"KISMET": KISMET_API_KEY}
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

    async def poll_devices(self, gps_fix: Optional[dict] = None) -> list:
        """Return recently seen devices from Kismet, GPS-stamped.

        Calls ``/devices/views/all/devices.json`` with a field filter to keep
        the payload small.  Each returned dict contains:

        ``macaddr``, ``type``, ``name``, ``manuf``, ``phyname``,
        ``first_time``, ``last_time``, ``last_signal``,
        ``gps_lat``, ``gps_lon``, ``gps_utc``

        Args:
            gps_fix: The current GPS fix dict (as returned by
                :meth:`~modules.gps.GPSModule.get_fix`), or ``None`` when no fix
                is available. Supplied by the orchestrator from its own fresh
                fix — the module performs no GPS read of its own.

        ``KISMET_ACTIVE_WINDOW_SECONDS`` (default 0 = disabled): when set to a
        positive integer, devices whose Kismet ``last_time`` is older than this
        many seconds are excluded from the returned list. Kismet's device list
        is permanent — it retains every device ever heard in the session, so
        on a mobile node a device passed 10 minutes ago is still present and
        will accumulate GPS clusters from the node's subsequent movement,
        making it look like a following device. Setting this to ~90–120 s
        limits the list to devices that are currently in RF range: a device
        not heard within the window has left (or never returned), so it is
        dropped before reaching the persistence engine. Leave at 0 (disabled)
        on fixed nodes where the full historical device list is needed for
        baseline building.
        """
        if self._session is None:
            logger.warning("poll_devices() called before connect()")
            return []

        payload = {"fields": _DEVICE_FIELDS}

        try:
            async with self._session.post(
                f"{_BASE_URL}/devices/views/all/devices.json",
                json=payload,
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                if resp.status != 200:
                    logger.warning("Kismet devices endpoint returned %d", resp.status)
                    return []
                raw = await resp.json(content_type=None)
        except Exception as exc:
            logger.error("Error polling Kismet devices: %s", exc)
            return []

        # Read per-call so patch.dict(os.environ, ...) works in tests without
        # module reload. 0 = disabled (default); positive int = seconds.
        active_window = int(os.getenv("KISMET_ACTIVE_WINDOW_SECONDS", "0"))
        now = time.time()

        devices = []
        ignored = 0
        stale = 0
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

            last_time = entry.get("kismet.device.base.last_time", 0)
            if active_window > 0 and last_time and (now - last_time) > active_window:
                stale += 1
                continue

            record = {
                "macaddr":      mac,
                "type":         entry.get("kismet.device.base.type", ""),
                "name":         ssid,
                "manuf":        entry.get("kismet.device.base.manuf", ""),
                "phyname":      entry.get("kismet.device.base.phyname", ""),
                "first_time":   entry.get("kismet.device.base.first_time", 0),
                "last_time":    last_time,
                # Kismet returns the simplified "a/b" field under its LEAF key,
                # so read kismet.common.signal.last_signal (not the slash-path).
                "last_signal":  entry.get("kismet.common.signal.last_signal", None),
                # Probe behaviour (leaf keys, like last_signal above). The ""
                # wildcard probe is excluded; absent map yields [].
                "probe_ssids":   _extract_probe_ssids(entry.get("dot11.device.probed_ssid_map")),
                "probe_fingerprint": entry.get("dot11.device.probe_fingerprint", None),
                "num_probed_ssids":  entry.get("dot11.device.num_probed_ssids", 0),
                "gps_lat":      gps_fix["lat"]  if gps_fix else None,
                "gps_lon":      gps_fix["lon"]  if gps_fix else None,
                "gps_utc":      gps_fix["utc"]  if gps_fix else None,
                "mac_type":     get_mac_type(mac),
                "is_randomized": is_randomized_mac(mac),
            }
            devices.append(record)

        if ignored:
            logger.debug("Ignored %d devices from Kismet (on ignore list)", ignored)
        if stale:
            logger.debug(
                "Dropped %d stale device(s) outside active window (%ds)",
                stale, active_window,
            )
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
