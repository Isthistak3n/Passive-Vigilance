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
import re
from datetime import datetime, timezone
from typing import Optional

from dotenv import load_dotenv

from modules.sdr_utils import is_rtl_sdr_present

load_dotenv()

logger = logging.getLogger(__name__)

ACARS_UDP_HOST = os.getenv("ACARS_UDP_HOST", "127.0.0.1")
ACARS_UDP_PORT = int(os.getenv("ACARS_UDP_PORT", "5555"))

# Origin/destination structured-field aliases across acarsdec / dumpvdl2 / VDL2.
_ORIGIN_KEYS = ("depa", "dep", "origin", "orig")
_DEST_KEYS = ("dsta", "dst", "destination", "dest", "arr")

# Two position encodings that show up in ACARS position-report free text. Both are
# deliberately strict (a decimal point is required, hemispheres explicit) so noise
# text never yields a bogus fix. Anything ambiguous returns no position.
#   decimal:      "N47.1234 W122.4567"
#   degree-minute:"N4712.3W12227.4"  (DDMM.m / DDDMM.m)
_POS_DECIMAL_RE = re.compile(
    r"(?P<lath>[NS])\s*(?P<lat>\d{1,2}\.\d+)\s*[, ]?\s*(?P<lonh>[EW])\s*(?P<lon>\d{1,3}\.\d+)"
)
_POS_DEGMIN_RE = re.compile(
    r"(?P<lath>[NS])(?P<latd>\d{2})(?P<latm>\d{2}\.\d+)(?P<lonh>[EW])(?P<lond>\d{3})(?P<lonm>\d{2}\.\d+)"
)


def _first_str(*sources) -> Optional[str]:
    """First non-empty stripped string among ``(mapping, keys)`` source pairs."""
    for mapping, keys in sources:
        if not isinstance(mapping, dict):
            continue
        for k in keys:
            v = mapping.get(k)
            if isinstance(v, str) and v.strip():
                return v.strip()
    return None


def _num(v) -> Optional[float]:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f


def _extract_position(acars: dict, outer: dict, text: Optional[str]):
    """Return ``(lat, lon)`` for a message that carries a position, else ``(None, None)``.

    Structured numeric lat/lon fields (on the ACARS block or the outer decoder
    object) win; otherwise a strict pattern match against the message text. A
    result is only returned when both are in range, so a partial/garbled match is
    dropped rather than mis-placing a contact.
    """
    for src in (acars, outer):
        if not isinstance(src, dict):
            continue
        lat = _num(src.get("lat", src.get("latitude")))
        lon = _num(src.get("lon", src.get("longitude")))
        if lat is not None and lon is not None and abs(lat) <= 90 and abs(lon) <= 180:
            return lat, lon
    if text:
        m = _POS_DECIMAL_RE.search(text)
        if m:
            lat = float(m.group("lat")) * (1 if m.group("lath") == "N" else -1)
            lon = float(m.group("lon")) * (1 if m.group("lonh") == "E" else -1)
            if abs(lat) <= 90 and abs(lon) <= 180:
                return lat, lon
        m = _POS_DEGMIN_RE.search(text)
        if m:
            lat = (int(m.group("latd")) + float(m.group("latm")) / 60.0) * (
                1 if m.group("lath") == "N" else -1)
            lon = (int(m.group("lond")) + float(m.group("lonm")) / 60.0) * (
                1 if m.group("lonh") == "E" else -1)
            if abs(lat) <= 90 and abs(lon) <= 180:
                return lat, lon
    return None, None


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
        # Enrichment fields (all optional): the origin/destination airports the
        # airframe declares, and a position report when the message carries one.
        # These are what tie a message to a contact beyond the tail/callsign and
        # what fill out the aircraft row.
        origin = _first_str((acars, _ORIGIN_KEYS), (msg, _ORIGIN_KEYS))
        destination = _first_str((acars, _DEST_KEYS), (msg, _DEST_KEYS))
        lat, lon = _extract_position(acars, msg, text)
        return {
            "tail": tail,
            "flight_id": flight,
            "label": label,
            "text": text,
            "origin": origin,
            "destination": destination,
            "lat": lat,
            "lon": lon,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

    def drain_detections(self) -> list:
        """Atomically return and clear the buffered datalink messages."""
        out = self._buffer
        self._buffer = []
        return out
