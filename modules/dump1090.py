"""ADS-B module — reads SBS-1 (BaseStation) messages from dump1090."""

import logging

logger = logging.getLogger(__name__)


class ADSBModule:
    """TCP client for dump1090's SBS-1 output stream."""

    def connect(self) -> None:
        """Open a TCP connection to dump1090."""
        raise NotImplementedError

    def poll_aircraft(self) -> list:
        """Return a list of recently seen aircraft (dicts) parsed from SBS-1 messages."""
        raise NotImplementedError

    def close(self) -> None:
        """Close the dump1090 TCP connection."""
        raise NotImplementedError
