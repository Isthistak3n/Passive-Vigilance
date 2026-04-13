"""Kismet module — polls the Kismet REST API for Wi-Fi / Bluetooth devices."""

import logging

logger = logging.getLogger(__name__)


class KismetModule:
    """Client for the Kismet REST API."""

    def connect(self) -> None:
        """Authenticate and verify connectivity to the Kismet server."""
        raise NotImplementedError

    def poll_devices(self) -> list:
        """Return a list of recently seen devices (dicts) from Kismet."""
        raise NotImplementedError

    def close(self) -> None:
        """Tear down the Kismet session."""
        raise NotImplementedError
