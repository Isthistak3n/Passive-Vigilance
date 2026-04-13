"""Drone RF detection module — scans 2.4 / 5.8 GHz bands via RTL-SDR or HackRF."""

import logging

logger = logging.getLogger(__name__)


class DroneRFModule:
    """Passive RF scanner for common drone control / video link frequencies."""

    def start_scan(self) -> None:
        """Begin background RF scanning for drone signatures."""
        raise NotImplementedError

    def stop_scan(self) -> None:
        """Stop the background RF scan and release SDR hardware."""
        raise NotImplementedError
