"""ADS-B module — async client for readsb JSON API with adsb.lol enrichment."""

import logging
import os
import subprocess
from typing import Optional

import aiohttp
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

DUMP1090_HOST = os.getenv("DUMP1090_HOST", "localhost")
DUMP1090_JSON_PORT = 8080  # readsb HTTP JSON port (separate from SBS-1 port 30003)
ADSBXLOL_API_KEY = os.getenv("ADSBXLOL_API_KEY", "")

_AIRCRAFT_URL = f"http://{DUMP1090_HOST}:{DUMP1090_JSON_PORT}/data/aircraft.json"
_ADSB_LOL_URL = "https://adsbexchange-com1.p.rapidapi.com/v2/icao/{icao}/"
_ADSB_LOL_HOST = "adsbexchange-com1.p.rapidapi.com"

# Known RTL-SDR USB vendor:product IDs
_RTL_SDR_USB_IDS = frozenset({"0bda:2832", "0bda:2838", "0bda:2813"})


class ADSBModule:
    """Async client for the readsb (dump1090-fa drop-in) JSON API.

    Fetches live aircraft data from readsb's HTTP JSON endpoint and optionally
    enriches each aircraft record with registration, type, and operator data
    from the adsb.lol / ADSBExchange API.

    A :class:`~modules.gps.GPSModule` instance may be passed at construction;
    every aircraft record returned by :meth:`poll_aircraft` is GPS-stamped.
    """

    def __init__(self, gps_module=None) -> None:
        self._gps = gps_module
        self._session: Optional[aiohttp.ClientSession] = None

    # ------------------------------------------------------------------
    # Connection lifecycle
    # ------------------------------------------------------------------

    async def connect(self) -> None:
        """Open an aiohttp session and verify readsb is reachable.

        Raises:
            ConnectionError: if readsb is not reachable.
        """
        self._session = aiohttp.ClientSession()
        try:
            async with self._session.get(
                _AIRCRAFT_URL, timeout=aiohttp.ClientTimeout(total=5)
            ) as resp:
                if resp.status != 200:
                    await self.close()
                    raise ConnectionError(
                        f"readsb returned HTTP {resp.status} — is it running?"
                    )
                data = await resp.json(content_type=None)
                aircraft_count = len(data.get("aircraft", []))
                if aircraft_count == 0:
                    logger.warning(
                        "readsb reachable but no aircraft seen yet — "
                        "normal if RTL-SDR is not connected or sky is empty"
                    )
                else:
                    logger.info(
                        "Connected to readsb at %s:%d (%d aircraft in view)",
                        DUMP1090_HOST, DUMP1090_JSON_PORT, aircraft_count,
                    )
        except aiohttp.ClientConnectorError as exc:
            await self.close()
            raise ConnectionError(
                f"Cannot reach readsb at {_AIRCRAFT_URL} — is it running? ({exc})"
            ) from exc

    async def close(self) -> None:
        """Close the aiohttp session."""
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None
        logger.info("ADSBModule session closed")

    # ------------------------------------------------------------------
    # Aircraft polling
    # ------------------------------------------------------------------

    async def poll_aircraft(self) -> list:
        """Fetch current aircraft from readsb, GPS-stamped.

        Returns a list of dicts with keys:
        ``icao``, ``callsign``, ``lat``, ``lon``, ``altitude``, ``speed``,
        ``track``, ``squawk``, ``seen``, ``rssi``, ``emergency``,
        ``gps_lat``, ``gps_lon``, ``gps_utc``
        """
        if self._session is None:
            logger.warning("poll_aircraft() called before connect()")
            return []

        gps_fix = None
        if self._gps is not None:
            try:
                gps_fix = self._gps.get_fix()
            except Exception as exc:
                logger.debug("GPS fix unavailable: %s", exc)

        try:
            async with self._session.get(
                _AIRCRAFT_URL, timeout=aiohttp.ClientTimeout(total=5)
            ) as resp:
                if resp.status != 200:
                    logger.warning("readsb aircraft endpoint returned %d", resp.status)
                    return []
                data = await resp.json(content_type=None)
        except Exception as exc:
            logger.error("Error polling readsb: %s", exc)
            return []

        aircraft = []
        for ac in data.get("aircraft", []):
            record = {
                "icao":      ac.get("hex", "").upper(),
                "callsign":  ac.get("flight", "").strip(),
                "lat":       ac.get("lat"),
                "lon":       ac.get("lon"),
                "altitude":  ac.get("alt_baro"),
                "speed":     ac.get("gs"),
                "track":     ac.get("track"),
                "squawk":    ac.get("squawk"),
                "seen":      ac.get("seen"),
                "rssi":      ac.get("rssi"),
                "emergency": ac.get("emergency", "none") not in ("none", None, ""),
                "gps_lat":   gps_fix["lat"] if gps_fix else None,
                "gps_lon":   gps_fix["lon"] if gps_fix else None,
                "gps_utc":   gps_fix["utc"] if gps_fix else None,
            }
            aircraft.append(record)

        logger.debug("Polled %d aircraft from readsb", len(aircraft))
        return aircraft

    # ------------------------------------------------------------------
    # adsb.lol enrichment
    # ------------------------------------------------------------------

    async def enrich_aircraft(self, icao: str) -> dict:
        """Look up registration, type, operator and military flag for an ICAO hex.

        Calls the ADSBExchange / adsb.lol API using ``ADSBXLOL_API_KEY``.
        Returns an empty dict gracefully if the key is not set or the API
        is unavailable.

        Returns a dict with keys:
        ``registration``, ``aircraft_type``, ``operator``, ``military``
        """
        if not ADSBXLOL_API_KEY:
            logger.debug("ADSBXLOL_API_KEY not set — skipping enrichment for %s", icao)
            return {}

        if self._session is None:
            return {}

        url = _ADSB_LOL_URL.format(icao=icao.lower())
        headers = {
            "x-rapidapi-key": ADSBXLOL_API_KEY,
            "x-rapidapi-host": _ADSB_LOL_HOST,
        }

        try:
            async with self._session.get(
                url, headers=headers, timeout=aiohttp.ClientTimeout(total=5)
            ) as resp:
                if resp.status != 200:
                    logger.debug(
                        "adsb.lol API returned %d for ICAO %s", resp.status, icao
                    )
                    return {}
                data = await resp.json(content_type=None)
        except Exception as exc:
            logger.debug("adsb.lol enrichment error for %s: %s", icao, exc)
            return {}

        entries = data.get("ac", [])
        if not entries:
            return {}

        entry = entries[0]
        db_flags = entry.get("dbFlags", 0) or 0
        return {
            "registration":  entry.get("r", ""),
            "aircraft_type": entry.get("t", ""),
            "operator":      entry.get("ownOp", ""),
            "military":      bool(db_flags & 1),
        }

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
