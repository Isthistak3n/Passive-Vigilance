"""Passive BLE advertisement capture (Phase 2 of design-ble-advertisement-capture.md).

Owns the Bluetooth controller (``hci0``) directly through a raw HCI socket and
listens **passively** (LE scan_type=0x00 — no SCAN_REQ, the radio never transmits)
for advertisement reports, producing structured records the orchestrator can
fingerprint and score. This replaces Kismet's ``linuxbluetooth`` source, which
returns empty advertisement fields on this hardware (see the design note) and,
critically, a flat ``0`` for signal strength.

Why raw HCI and not the BlueZ "passive scan" library path: validated on the node
(Edimax ``hci0``, BlueZ 5.82), the controller does not support BlueZ's offloaded
advertisement monitoring, so that path returns nothing. Raw HCI advertising
reports work and carry a real RSSI.

Runtime requirements:
    - Exclusive use of ``hci0`` — Kismet's Bluetooth source must be removed.
    - ``CAP_NET_RAW`` + ``CAP_NET_ADMIN`` on the interpreter (install.sh grants
      this to the capture helper). Without them ``connect()`` degrades gracefully:
      it logs a warning and the module is simply skipped, like any other sensor.

The parsing functions (:func:`parse_advertisement_data`,
:func:`parse_hci_advertising_report`) are pure and unit-tested; the socket plumbing
is integrated with the asyncio loop via ``add_reader`` so capture is non-blocking.
"""
from __future__ import annotations

import asyncio
import logging
import socket
import struct
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Awaitable, Callable, Optional

logger = logging.getLogger(__name__)

# ── HCI / BlueZ constants ────────────────────────────────────────────────────
AF_BLUETOOTH = getattr(socket, "AF_BLUETOOTH", 31)
BTPROTO_HCI = getattr(socket, "BTPROTO_HCI", 1)
SOL_HCI = 0
HCI_FILTER = 2

HCI_EVENT_PKT = 0x04
HCI_EV_LE_META = 0x3E
HCI_SUBEV_LE_ADVERTISING_REPORT = 0x02

OGF_LE = 0x08
OCF_LE_SET_SCAN_PARAMETERS = 0x000B
OCF_LE_SET_SCAN_ENABLE = 0x000C

# AD (advertising data) structure type bytes we care about.
AD_FLAGS = 0x01
AD_INCOMPLETE_UUID16 = 0x02
AD_COMPLETE_UUID16 = 0x03
AD_SHORT_NAME = 0x08
AD_COMPLETE_NAME = 0x09
AD_TX_POWER = 0x0A
AD_SERVICE_DATA_UUID16 = 0x16
AD_APPEARANCE = 0x19
AD_MANUFACTURER_SPECIFIC = 0xFF


@dataclass
class AdvertParse:
    """Fingerprint-relevant fields extracted from one advertisement payload."""

    company_ids: list[int] = field(default_factory=list)
    service_uuids: list[int] = field(default_factory=list)       # 16-bit
    service_data_uuids: list[int] = field(default_factory=list)  # 16-bit
    local_name: str = ""
    appearance: Optional[int] = None
    tx_power: Optional[int] = None


@dataclass
class BLEAdvert:
    """One captured BLE advertisement."""

    address: str
    address_type: int
    rssi: Optional[int]
    company_ids: list[int]
    service_uuids: list[int]
    service_data_uuids: list[int]
    local_name: str
    appearance: Optional[int]
    tx_power: Optional[int]
    timestamp: datetime


def parse_advertisement_data(data: bytes) -> AdvertParse:
    """Walk the length-type-value AD structures of an advertisement payload.

    Tolerant of truncated/malformed structures (a length that runs off the end
    just stops the walk) so a single bad advert never raises.
    """
    out = AdvertParse()
    j = 0
    n = len(data)
    while j < n:
        length = data[j]
        if length == 0:
            break
        ad_type = data[j + 1] if j + 1 < n else 0
        value = data[j + 2:j + 1 + length]
        if ad_type == AD_MANUFACTURER_SPECIFIC and len(value) >= 2:
            out.company_ids.append(value[0] | (value[1] << 8))
        elif ad_type in (AD_INCOMPLETE_UUID16, AD_COMPLETE_UUID16):
            for k in range(0, len(value) - 1, 2):
                out.service_uuids.append(value[k] | (value[k + 1] << 8))
        elif ad_type == AD_SERVICE_DATA_UUID16 and len(value) >= 2:
            out.service_data_uuids.append(value[0] | (value[1] << 8))
        elif ad_type in (AD_SHORT_NAME, AD_COMPLETE_NAME):
            out.local_name = value.decode("utf-8", "replace")
        elif ad_type == AD_TX_POWER and len(value) >= 1:
            out.tx_power = struct.unpack("b", value[:1])[0]
        elif ad_type == AD_APPEARANCE and len(value) >= 2:
            out.appearance = value[0] | (value[1] << 8)
        j += length + 1
    return out


def parse_hci_advertising_report(pkt: bytes) -> Optional[BLEAdvert]:
    """Parse one HCI packet into a :class:`BLEAdvert`, or None if not an adv report.

    Handles the common single-report case. Returns None for any non-advertising
    packet or anything too short to be valid, so callers can filter cheaply.
    """
    if len(pkt) < 12:
        return None
    if pkt[0] != HCI_EVENT_PKT or pkt[1] != HCI_EV_LE_META:
        return None
    if pkt[3] != HCI_SUBEV_LE_ADVERTISING_REPORT:
        return None
    # pkt: [type, evt_code, plen, subevent, num_reports, evt_type, addr_type, addr(6), len, data..., rssi]
    i = 5  # at evt_type of the first report
    addr_type = pkt[i + 1]
    addr = pkt[i + 2:i + 8][::-1]
    dlen = pkt[i + 8]
    data = pkt[i + 9:i + 9 + dlen]
    rssi_off = i + 9 + dlen
    if rssi_off >= len(pkt):
        return None
    rssi = struct.unpack("b", pkt[rssi_off:rssi_off + 1])[0]
    parsed = parse_advertisement_data(data)
    return BLEAdvert(
        address=":".join("%02X" % b for b in addr),
        address_type=addr_type,
        rssi=rssi,
        company_ids=parsed.company_ids,
        service_uuids=parsed.service_uuids,
        service_data_uuids=parsed.service_data_uuids,
        local_name=parsed.local_name,
        appearance=parsed.appearance,
        tx_power=parsed.tx_power,
        timestamp=datetime.now(timezone.utc),
    )


def _hci_command(ocf: int, params: bytes, ogf: int = OGF_LE) -> bytes:
    opcode = (ogf << 10) | ocf
    return b"\x01" + struct.pack("<H", opcode) + bytes([len(params)]) + params


AdvertCallback = Callable[[BLEAdvert], Optional[Awaitable[None]]]


class BLEScanner:
    """Async passive BLE advertisement scanner bound to a single HCI controller."""

    def __init__(
        self,
        hci_dev: int = 0,
        on_advert: Optional[AdvertCallback] = None,
    ) -> None:
        self.hci_dev = hci_dev
        self.on_advert = on_advert
        self._sock: Optional[socket.socket] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self.available = False

    async def connect(self) -> bool:
        """Open the HCI socket and start passive scanning. Returns False (degraded)
        on permission/availability errors rather than raising."""
        try:
            sock = socket.socket(AF_BLUETOOTH, socket.SOCK_RAW, BTPROTO_HCI)
            # hci_filter is 4-byte aligned -> 16 bytes (14 logical + 2 pad), else EINVAL.
            sock.setsockopt(
                SOL_HCI, HCI_FILTER,
                struct.pack("<IIIH2x", 0xFFFFFFFF, 0xFFFFFFFF, 0xFFFFFFFF, 0),
            )
            sock.bind((self.hci_dev,))
            # passive scan: scan_type=0x00, interval/window 0x0010, own addr public, accept-all
            sock.send(_hci_command(OCF_LE_SET_SCAN_PARAMETERS,
                                   struct.pack("<BHHBB", 0x00, 0x0010, 0x0010, 0x00, 0x00)))
            sock.send(_hci_command(OCF_LE_SET_SCAN_ENABLE, struct.pack("<BB", 0x01, 0x00)))
            sock.setblocking(False)
        except PermissionError:
            logger.warning(
                "BLEScanner: no permission for raw HCI on hci%d — needs CAP_NET_RAW+"
                "CAP_NET_ADMIN; BLE capture disabled.", self.hci_dev)
            return False
        except OSError as exc:
            logger.warning(
                "BLEScanner: cannot open hci%d (%s) — is it present and free of "
                "Kismet/bluetoothd? BLE capture disabled.", self.hci_dev, exc)
            return False

        self._sock = sock
        self._loop = asyncio.get_running_loop()
        self._loop.add_reader(sock.fileno(), self._on_readable)
        self.available = True
        logger.info("BLEScanner: passive advertisement capture started on hci%d", self.hci_dev)
        return True

    def _on_readable(self) -> None:
        """Drain all pending HCI packets without blocking the loop."""
        if self._sock is None:
            return
        while True:
            try:
                pkt = self._sock.recv(260)
            except BlockingIOError:
                return
            except OSError:
                return
            advert = parse_hci_advertising_report(pkt)
            if advert is None or self.on_advert is None:
                continue
            try:
                result = self.on_advert(advert)
                if asyncio.iscoroutine(result) and self._loop is not None:
                    self._loop.create_task(result)
            except Exception:  # a bad callback must never kill capture
                logger.exception("BLEScanner: on_advert callback raised")

    async def close(self) -> None:
        """Stop scanning and release the controller."""
        sock = self._sock
        self.available = False
        if sock is None:
            return
        if self._loop is not None:
            try:
                self._loop.remove_reader(sock.fileno())
            except (ValueError, OSError):
                pass
        try:
            sock.send(_hci_command(OCF_LE_SET_SCAN_ENABLE, struct.pack("<BB", 0x00, 0x00)))
        except OSError:
            pass
        try:
            sock.close()
        except OSError:
            pass
        self._sock = None
        logger.info("BLEScanner: stopped")
