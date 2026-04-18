"""GPS module — wraps gpsd (python3-gps) to provide position fixes for all sensor events."""

import logging
import os
from math import isfinite
from typing import Optional

from gps import gps as GpsSession
from gps import MODE_2D, MODE_3D, MODE_NO_FIX, WATCH_ENABLE, WATCH_NEWSTYLE
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

_GPS_CANDIDATES = [
    "/dev/ttyUSB0",
    "/dev/ttyUSB1",
    "/dev/ttyACM0",
    "/dev/ttyACM1",
]


def _resolve_gps_device() -> str:
    """Return the GPS device path to use.

    Tries GPS_DEVICE from .env first; if that path does not exist, scans
    _GPS_CANDIDATES and returns the first one that exists.  Logs a warning
    if nothing is found and returns the configured value anyway so gpsd can
    emit its own error.
    """
    configured = os.getenv("GPS_DEVICE", "/dev/ttyUSB0")
    if os.path.exists(configured):
        return configured
    for candidate in _GPS_CANDIDATES:
        if os.path.exists(candidate):
            logger.warning(
                "GPS_DEVICE %s not found — using %s instead", configured, candidate
            )
            return candidate
    logger.warning(
        "GPS_DEVICE %s not found and no candidates detected — gpsd may fail", configured
    )
    return configured


GPS_DEVICE = _resolve_gps_device()


class GPSModule:
    """Interface to a gpsd-connected GPS receiver.

    Every detection event in the platform is stamped with lat, lon, altitude,
    and UTC time obtained from this module.  Uses the python3-gps streaming
    client; call connect() once at startup, then poll get_fix() each loop tick.
    """

    def __init__(self) -> None:
        self._session: Optional[GpsSession] = None

    # ------------------------------------------------------------------
    # Connection lifecycle
    # ------------------------------------------------------------------

    def connect(self) -> None:
        """Open a streaming connection to gpsd.

        Raises:
            ConnectionError: if gpsd is not reachable.
        """
        try:
            self._session = GpsSession(mode=WATCH_ENABLE | WATCH_NEWSTYLE)
            logger.info("Connected to gpsd (device: %s)", GPS_DEVICE)
        except Exception as exc:
            self._session = None
            raise ConnectionError(
                f"Could not connect to gpsd — is it running? ({exc})"
            ) from exc

    def close(self) -> None:
        """Close the gpsd streaming connection."""
        if self._session is not None:
            try:
                self._session.close()
            except Exception:
                pass
            self._session = None
        logger.info("Disconnected from gpsd")

    # ------------------------------------------------------------------
    # Fix data
    # ------------------------------------------------------------------

    def get_fix(self) -> Optional[dict]:
        """Return the current GPS fix or *None* if no fix is available yet.

        Drains one pending report from gpsd before sampling the fix, so the
        data stays fresh when called in a polling loop.

        Returns a dict with the following keys:
            lat          (float)  — latitude in decimal degrees
            lon          (float)  — longitude in decimal degrees
            alt          (float)  — altitude HAE in metres (NaN on a 2-D fix)
            speed        (float)  — ground speed in m/s
            track        (float)  — true heading in degrees
            utc          (str)    — ISO-8601 UTC timestamp from the receiver
            fix_quality  (int)    — 0 = no fix, 2 = 2-D fix, 3 = 3-D fix
        """
        if self._session is None:
            logger.warning("get_fix() called before connect()")
            return None

        try:
            self._session.read()
        except Exception as exc:
            logger.debug("gpsd read error: %s", exc)
            return None

        fix = self._session.fix
        mode = getattr(fix, "mode", MODE_NO_FIX)

        if mode < MODE_2D:
            logger.debug("No GPS fix yet (mode=%d)", mode)
            return None

        alt = fix.altHAE if (mode == MODE_3D and isfinite(fix.altHAE)) else float("nan")

        result = {
            "lat": fix.latitude,
            "lon": fix.longitude,
            "alt": alt,
            "speed": fix.speed,
            "track": fix.track,
            "utc": self._session.utc,
            "fix_quality": mode,
        }
        logger.debug("GPS fix: %s", result)
        return result

    def is_fixed(self) -> bool:
        """Return True if a 2-D or 3-D fix is currently available."""
        fix = self.get_fix()
        if fix is None:
            return False
        return fix["fix_quality"] >= MODE_2D
