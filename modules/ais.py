"""AIS module — passive marine vessel tracking via an AIS-catcher JSON feed.

OPTIONAL / best-effort. AIS is marine VHF (~161.975 / 162.025 MHz); it will not
receive on a 1090 MHz ADS-B antenna and degrades rapidly inland, so it is OFF by
default (``AIS_ENABLED``). The AIS-catcher decoder runs as a systemd service —
started/stopped by the SDR coordinator on the AIS slice (single dongle), or
continuously on a dedicated VHF dongle. This module just listens on a localhost
UDP socket for the line-delimited JSON AIS-catcher emits and buffers parsed vessel
reports for the orchestrator to drain — the same contract DroneRF/ADS-B use.

It performs no GPS reads (vessel position comes from the AIS message itself); the
``can_scan`` / ``auto_disabled`` flags mirror DroneRF so the coordinator and the
GUI status chiclet treat AIS like the other SDR bands.
"""

import asyncio
import json
import logging
import os
from datetime import datetime, timezone
from typing import Optional

from dotenv import load_dotenv

from modules.sdr_utils import is_rtl_sdr_present

load_dotenv()

logger = logging.getLogger(__name__)

AIS_UDP_HOST = os.getenv("AIS_UDP_HOST", "127.0.0.1")
AIS_UDP_PORT = int(os.getenv("AIS_UDP_PORT", "10110"))

# AIS "not available" sentinels for position fields (per ITU-R M.1371).
_LAT_NA = 91.0
_LON_NA = 181.0


class _AISDatagramProtocol(asyncio.DatagramProtocol):
    """Hands each newline-delimited datagram line to the module's ingest callback."""

    def __init__(self, on_line) -> None:
        self._on_line = on_line

    def datagram_received(self, data: bytes, addr) -> None:
        for line in data.decode("utf-8", "ignore").splitlines():
            line = line.strip()
            if line:
                self._on_line(line)

    def error_received(self, exc) -> None:  # pragma: no cover - transport noise
        logger.debug("AIS UDP error: %s", exc)


class AISModule:
    """Consume AIS-catcher JSON over UDP and buffer parsed vessel reports."""

    def __init__(self, gps_module=None) -> None:
        # Accepted for construction symmetry; AIS carries vessel position itself.
        self._gps = gps_module
        self._host = AIS_UDP_HOST
        self._port = AIS_UDP_PORT
        self._transport: Optional[asyncio.BaseTransport] = None
        self._buffer: list[dict] = []
        # Mirror the DroneRF flags so the coordinator/GUI treat AIS as an SDR band.
        self.can_scan: bool = True
        self.auto_disabled: bool = False

    def is_hardware_present(self) -> bool:
        return is_rtl_sdr_present()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def connect(self) -> None:
        """Bind the localhost UDP socket AIS-catcher sends JSON to.

        The socket stays open for the node's lifetime; it simply receives nothing
        while the AIS-catcher service is stopped (e.g. between AIS slices).
        """
        loop = asyncio.get_running_loop()
        self._transport, _ = await loop.create_datagram_endpoint(
            lambda: _AISDatagramProtocol(self._ingest),
            local_addr=(self._host, self._port),
        )
        logger.info("AISModule listening for AIS-catcher JSON on %s:%d",
                    self._host, self._port)

    async def close(self) -> None:
        if self._transport is not None:
            self._transport.close()
            self._transport = None
        logger.info("AISModule closed")

    # ------------------------------------------------------------------
    # Ingest / parse
    # ------------------------------------------------------------------

    def _ingest(self, line: str) -> None:
        try:
            msg = json.loads(line)
        except (ValueError, TypeError):
            return
        if not isinstance(msg, dict):
            return
        det = self._parse(msg)
        if det is not None:
            self._buffer.append(det)

    @staticmethod
    def _parse(msg: dict) -> Optional[dict]:
        """Normalize one AIS-catcher JSON object → a vessel detection, or None.

        Accepts both position reports (carry lat/lon) and static reports (carry
        name/shiptype); the orchestrator dedups by MMSI and merges the two.
        """
        mmsi = msg.get("mmsi", msg.get("MMSI"))
        if mmsi is None:
            return None
        try:
            mmsi = int(mmsi)
        except (TypeError, ValueError):
            return None

        lat = msg.get("lat")
        lon = msg.get("lon")
        try:
            lat = float(lat) if lat is not None else None
            lon = float(lon) if lon is not None else None
        except (TypeError, ValueError):
            lat = lon = None
        if lat == _LAT_NA or lon == _LON_NA:  # AIS "position not available"
            lat = lon = None

        name = (msg.get("shipname") or msg.get("name") or "")
        name = name.strip() or None
        ship_type = msg.get("shiptype", msg.get("ship_type"))

        return {
            "mmsi": mmsi,
            "lat": lat,
            "lon": lon,
            "name": name,
            "ship_type": ship_type,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

    def drain_detections(self) -> list:
        """Atomically return and clear the buffered vessel reports."""
        out = self._buffer
        self._buffer = []
        return out
