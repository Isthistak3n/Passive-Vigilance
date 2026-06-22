"""ACARS module — passive aviation datalink decode via an acarsdec/dumpvdl2 JSON feed.

OPTIONAL / best-effort. ACARS is aviation VHF (legacy ~131 MHz via acarsdec; modern
VDL Mode 2 ~136 MHz via dumpvdl2) and will not receive on a 1090 MHz ADS-B antenna,
so it is OFF by default (``ACARS_ENABLED``). **ACARS is plaintext — this DECODES it,
it does not "decrypt".** It is also a SHARED broadcast channel: you receive every
aircraft in range, not a chosen target; the orchestrator correlates a decoded
message back to a live ADS-B contact by tail number / flight-id.

The decoder runs as a systemd service, invoked by the SDR coordinator's
``request_band_window("acars", …)`` when an ADS-B contact has been held >30 s (on a
single dongle), or continuously on a dedicated VHF dongle. This module just listens
on a localhost UDP socket for the decoder's line-delimited JSON and buffers parsed
messages for the orchestrator to drain — the same contract AIS/DroneRF/ADS-B use.
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

ACARS_UDP_HOST = os.getenv("ACARS_UDP_HOST", "127.0.0.1")
ACARS_UDP_PORT = int(os.getenv("ACARS_UDP_PORT", "5555"))


class _ACARSDatagramProtocol(asyncio.DatagramProtocol):
    def __init__(self, on_line) -> None:
        self._on_line = on_line

    def datagram_received(self, data: bytes, addr) -> None:
        for line in data.decode("utf-8", "ignore").splitlines():
            line = line.strip()
            if line:
                self._on_line(line)

    def error_received(self, exc) -> None:  # pragma: no cover - transport noise
        logger.debug("ACARS UDP error: %s", exc)


class ACARSModule:
    """Consume acarsdec/dumpvdl2 JSON over UDP and buffer parsed datalink messages."""

    def __init__(self, gps_module=None) -> None:
        self._gps = gps_module
        self._host = ACARS_UDP_HOST
        self._port = ACARS_UDP_PORT
        self._transport: Optional[asyncio.BaseTransport] = None
        self._buffer: list[dict] = []
        # Mirror the other SDR bands so the coordinator/GUI treat ACARS uniformly.
        self.can_scan: bool = True
        self.auto_disabled: bool = False

    def is_hardware_present(self) -> bool:
        return is_rtl_sdr_present()

    async def connect(self) -> None:
        loop = asyncio.get_running_loop()
        self._transport, _ = await loop.create_datagram_endpoint(
            lambda: _ACARSDatagramProtocol(self._ingest),
            local_addr=(self._host, self._port),
        )
        logger.info("ACARSModule listening for decoder JSON on %s:%d",
                    self._host, self._port)

    async def close(self) -> None:
        if self._transport is not None:
            self._transport.close()
            self._transport = None
        logger.info("ACARSModule closed")

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
        """Normalize an acarsdec OR dumpvdl2 JSON object → a datalink message.

        Both decoders nest the ACARS payload differently:
        - acarsdec: flat-ish, keys include ``tail``/``reg``, ``flight``, ``label``, ``text``.
        - dumpvdl2: ``{"vdl2": {"avlc": {"acars": {"reg","flight","label","msg_text"}}}}``.
        We extract a tail, flight-id, label and free text from whichever is present.
        """
        acars = msg
        # dumpvdl2 nesting → dig down to the inner acars block if present.
        if "vdl2" in msg:
            acars = (((msg.get("vdl2") or {}).get("avlc") or {}).get("acars") or {})
        if not isinstance(acars, dict) or not acars:
            return None

        tail = (acars.get("tail") or acars.get("reg") or "").strip() or None
        flight = (acars.get("flight") or acars.get("flight_id") or "").strip() or None
        label = (acars.get("label") or "").strip() or None
        text = acars.get("text")
        if text is None:
            text = acars.get("msg_text")
        if isinstance(text, str):
            text = text.strip() or None
        # A message with no identity AND no content is noise — drop it.
        if tail is None and flight is None and text is None:
            return None
        return {
            "tail": tail,
            "flight_id": flight,
            "label": label,
            "text": text,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

    def drain_detections(self) -> list:
        """Atomically return and clear the buffered datalink messages."""
        out = self._buffer
        self._buffer = []
        return out
