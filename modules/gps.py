"""GPS module — wraps gpsd to provide position fixes."""

import logging

logger = logging.getLogger(__name__)


class GPSModule:
    """Interface to a gpsd-connected GPS receiver."""

    def connect(self) -> None:
        """Open connection to gpsd."""
        raise NotImplementedError

    def get_fix(self) -> dict:
        """Return the current GPS fix as a dict with lat, lon, alt, speed, time."""
        raise NotImplementedError

    def close(self) -> None:
        """Close the gpsd connection."""
        raise NotImplementedError
