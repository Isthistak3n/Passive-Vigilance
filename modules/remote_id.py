"""FAA Remote ID detection (ASTM F3411-22a) via Kismet's device REST API.

Kismet 2025-09-R1 does not have a dedicated Remote ID endpoint — its drone
support is DJI DroneID only.  This module uses the existing
``/devices/last-time/{ts}/devices`` endpoint, requests the raw IE tag bytes,
filters for Vendor Specific IE (tag 221, OUI FA:0B:BC), and parses the
ASTM F3411-22a payload with struct.

Message units are 25 bytes each:
  Byte 0           : msg_type (upper 4 bits) | protocol_version (lower 4 bits)
  Bytes 1-24       : message body (varies by type)

Supported message types:
  0 = Basic ID       (UAS serial / session ID, UA type)
  1 = Location       (lat, lon, altitude, speed, heading, status)
  4 = System         (operator lat/lon, classification)
  5 = Operator ID    (operator registration number)
"""

import asyncio
import logging
import os
import struct
from datetime import datetime, timezone
from typing import Optional

import aiohttp
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

_KISMET_HOST = os.getenv("KISMET_HOST", "localhost")
_KISMET_PORT = int(os.getenv("KISMET_PORT", "2501"))
_KISMET_API_KEY = os.getenv("KISMET_API_KEY", "")
_BASE_URL = f"http://{_KISMET_HOST}:{_KISMET_PORT}"

# ASTM F3411-22a / Open Drone ID WiFi Vendor OUI in the IE body
_REMOTE_ID_OUI = bytes([0xFA, 0x0B, 0xBC])
_REMOTE_ID_VENDOR_TYPE = 0x0D
_IE_TAG_VENDOR_SPECIFIC = 221

# Fields to request from Kismet
_RID_DEVICE_FIELDS = [
    "kismet.device.base.macaddr",
    "kismet.device.base.phyname",
    "kismet.device.base.signal/last_signal",
    "dot11.device.last_beaconed_ssid_record/dot11.advertisedssid.ie_tag_list",
    "dot11.device.last_beaconed_ssid_record/dot11.advertisedssid.ie_tag_content",
]

# Enumeration mappings (ASTM F3411-22a §5.4.1 and §5.4.3)
_ID_TYPE_MAP = {
    0: "none", 1: "serial_number", 2: "caa_assigned",
    3: "utm_assigned", 4: "specific_session",
}
_UA_TYPE_MAP = {
    0: "none", 1: "aeroplane", 2: "helicopter_or_multirotor", 3: "gyroplane",
    4: "hybrid_lift", 5: "ornithopter", 6: "glider", 7: "kite",
    8: "free_balloon", 9: "captive_balloon", 10: "airship",
    11: "free_fall_parachute", 12: "rocket", 13: "tethered_powered_aircraft",
    14: "ground_obstacle", 15: "other",
}
_STATUS_MAP = {
    0: "undeclared", 1: "ground", 2: "airborne", 3: "emergency", 4: "system_failure",
}


def _decode_altitude(raw: int) -> Optional[float]:
    """Decode a 2-byte ASTM F3411 altitude field to metres.  Returns None for invalid."""
    if raw == 0xFFFF:
        return None
    return (raw / 2.0) - 1000.0


def _decode_lat_lon(raw: int) -> Optional[float]:
    """Decode a 4-byte sint32 lat/lon field to decimal degrees.  Returns None for invalid."""
    if raw == 0x7FFFFFFF:
        return None
    return raw * 1e-7


class RemoteIDModule:
    """Polls Kismet for FAA Remote ID beacon frames (ASTM F3411-22a)."""

    def __init__(self, gps_module=None) -> None:
        self._gps = gps_module
        self._session: Optional[aiohttp.ClientSession] = None
        self._last_poll_epoch: int = 0
        self._active: bool = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def connect(self) -> None:
        """Open aiohttp session and verify the Kismet endpoint is reachable.

        Raises:
            ConnectionError: if Kismet is unreachable or KISMET_API_KEY is unset.
        """
        if not _KISMET_API_KEY:
            raise ConnectionError(
                "KISMET_API_KEY is not set — cannot connect RemoteIDModule to Kismet"
            )
        self._session = aiohttp.ClientSession(cookies={"KISMET": _KISMET_API_KEY})
        try:
            url = f"{_BASE_URL}/system/status.json"
            async with self._session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                if resp.status == 401:
                    await self.close()
                    raise ConnectionError("Kismet rejected KISMET_API_KEY — check the key")
                if resp.status != 200:
                    await self.close()
                    raise ConnectionError(
                        f"Kismet returned HTTP {resp.status} — is Kismet running?"
                    )
        except aiohttp.ClientError as exc:
            await self.close()
            raise ConnectionError(f"Cannot reach Kismet at {_BASE_URL}: {exc}") from exc
        self._active = True
        logger.info("RemoteIDModule: connected to Kismet at %s", _BASE_URL)

    async def close(self) -> None:
        """Close the aiohttp session."""
        if self._session is not None:
            try:
                await self._session.close()
            except Exception:
                pass
            self._session = None
        self._active = False

    # ------------------------------------------------------------------
    # Polling
    # ------------------------------------------------------------------

    async def poll(self) -> list[dict]:
        """Return a list of Remote ID detection dicts seen since the last poll.

        Returns [] on any error so callers never raise.
        """
        if self._session is None:
            return []

        url = f"{_BASE_URL}/devices/last-time/{self._last_poll_epoch}/devices.json"
        fields_payload = {"fields": _RID_DEVICE_FIELDS}
        try:
            async with self._session.post(
                url,
                json=fields_payload,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status != 200:
                    logger.debug("RemoteID: Kismet returned HTTP %d", resp.status)
                    return []
                raw_devices: list = await resp.json(content_type=None)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.debug("RemoteID poll error: %s", exc)
            return []

        self._last_poll_epoch = int(datetime.now(timezone.utc).timestamp())

        results: list[dict] = []
        for dev in raw_devices:
            if not isinstance(dev, dict):
                continue
            detections = self._parse_device(dev)
            results.extend(detections)

        return results

    # ------------------------------------------------------------------
    # Parsing
    # ------------------------------------------------------------------

    def _parse_device(self, dev: dict) -> list[dict]:
        """Extract Remote ID detections from a Kismet device dict.

        Returns an empty list if the device carries no Remote ID frames.
        """
        # Pull the IE tag list and content from the nested structure
        ssid_record = dev.get(
            "dot11.device.last_beaconed_ssid_record/dot11.advertisedssid.ie_tag_list"
        )
        ie_content_raw = dev.get(
            "dot11.device.last_beaconed_ssid_record/dot11.advertisedssid.ie_tag_content"
        )

        tag_list = ssid_record if isinstance(ssid_record, list) else []

        if _IE_TAG_VENDOR_SPECIFIC not in tag_list:
            return []
        if not ie_content_raw:
            return []

        # ie_tag_content may be hex string or base64 — try both
        ie_bytes = self._decode_ie_content(ie_content_raw)
        if ie_bytes is None:
            return []

        rid_payloads = self._parse_ie_tags(ie_bytes)
        if not rid_payloads:
            return []

        # GPS stamp from our observer
        gps_lat: Optional[float] = None
        gps_lon: Optional[float] = None
        if self._gps is not None:
            try:
                fix = self._gps.get_fix()
                if fix:
                    gps_lat = fix.get("lat")
                    gps_lon = fix.get("lon")
            except Exception:
                pass

        source_mac = dev.get("kismet.device.base.macaddr", "")
        source_phy = dev.get("kismet.device.base.phyname", "WiFi")
        rssi_raw = dev.get("kismet.device.base.signal/last_signal")
        rssi: Optional[int] = int(rssi_raw) if rssi_raw is not None else None

        results: list[dict] = []
        for payload in rid_payloads:
            detection = self._parse_astm_payload(payload)
            if detection is None:
                continue
            detection.update({
                "source_mac": source_mac,
                "source_phy": source_phy,
                "rssi": rssi,
                "gps_lat": gps_lat,
                "gps_lon": gps_lon,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "event_type": "remote_id",
            })
            results.append(detection)

        return results

    @staticmethod
    def _decode_ie_content(raw) -> Optional[bytes]:
        """Decode IE tag content from hex string, bytes, or base64."""
        if isinstance(raw, (bytes, bytearray)):
            return bytes(raw)
        if isinstance(raw, str):
            # Try hex first (Kismet commonly returns lowercase hex)
            try:
                return bytes.fromhex(raw)
            except ValueError:
                pass
            # Try base64
            try:
                import base64
                return base64.b64decode(raw)
            except Exception:
                pass
        return None

    @staticmethod
    def _parse_ie_tags(ie_bytes: bytes) -> list[bytes]:
        """Walk 802.11 IE tag sequence; return Remote ID vendor IE payloads.

        The OUI check is FA:0B:BC followed by vendor type 0x0D.
        Returns one payload bytes object per matching IE found.
        """
        payloads: list[bytes] = []
        i = 0
        while i + 2 <= len(ie_bytes):
            tag_num = ie_bytes[i]
            tag_len = ie_bytes[i + 1]
            if i + 2 + tag_len > len(ie_bytes):
                break
            body = ie_bytes[i + 2: i + 2 + tag_len]
            if tag_num == _IE_TAG_VENDOR_SPECIFIC and len(body) >= 5:
                if body[:3] == _REMOTE_ID_OUI and body[3] == _REMOTE_ID_VENDOR_TYPE:
                    # body[4] is the counter; message units start at body[5]
                    payloads.append(body[5:])
            i += 2 + tag_len
        return payloads

    def _parse_astm_payload(self, payload: bytes) -> Optional[dict]:
        """Parse one or more 25-byte ASTM F3411-22a message units from a payload.

        Returns a merged detection dict, or None if no recognisable messages found.
        """
        detection: dict = {
            "uas_id": None, "id_type": None, "ua_type": None,
            "operator_id": None, "status": None,
            "drone_lat": None, "drone_lon": None, "drone_alt_m": None,
            "height_m": None, "speed_mps": None, "vert_speed_mps": None,
            "heading_deg": None,
            "operator_lat": None, "operator_lon": None,
        }
        found_any = False

        i = 0
        while i + 25 <= len(payload):
            unit = payload[i: i + 25]
            msg_type = (unit[0] >> 4) & 0x0F
            found_any = True

            if msg_type == 0:  # Basic ID
                self._parse_basic_id(unit, detection)
            elif msg_type == 1:  # Location/Vector
                self._parse_location(unit, detection)
            elif msg_type == 4:  # System
                self._parse_system(unit, detection)
            elif msg_type == 5:  # Operator ID
                self._parse_operator_id(unit, detection)

            i += 25

        return detection if found_any else None

    @staticmethod
    def _parse_basic_id(unit: bytes, detection: dict) -> None:
        id_type_raw = (unit[1] >> 4) & 0x0F
        ua_type_raw = unit[1] & 0x0F
        uas_id_bytes = unit[2:22]
        detection["id_type"] = _ID_TYPE_MAP.get(id_type_raw, "unknown")
        detection["ua_type"] = _UA_TYPE_MAP.get(ua_type_raw, "unknown")
        detection["uas_id"] = uas_id_bytes.rstrip(b"\x00").decode("ascii", errors="replace")

    @staticmethod
    def _parse_location(unit: bytes, detection: dict) -> None:
        status_raw = (unit[1] >> 4) & 0x0F
        detection["status"] = _STATUS_MAP.get(status_raw, "undeclared")
        detection["heading_deg"] = unit[2] * (360.0 / 256.0)
        speed_raw = unit[3]
        detection["speed_mps"] = speed_raw * 0.25 if speed_raw != 0xFF else None
        vert_raw = struct.unpack_from("b", unit, 4)[0]  # signed byte
        detection["vert_speed_mps"] = vert_raw * 0.5
        lat_raw = struct.unpack_from("<i", unit, 5)[0]
        lon_raw = struct.unpack_from("<i", unit, 9)[0]
        detection["drone_lat"] = _decode_lat_lon(lat_raw)
        detection["drone_lon"] = _decode_lat_lon(lon_raw)
        alt_baro_raw = struct.unpack_from("<H", unit, 13)[0]
        alt_geo_raw = struct.unpack_from("<H", unit, 15)[0]
        # Prefer geodetic altitude; fall back to barometric
        geo = _decode_altitude(alt_geo_raw)
        baro = _decode_altitude(alt_baro_raw)
        detection["drone_alt_m"] = geo if geo is not None else baro
        height_raw = struct.unpack_from("<H", unit, 17)[0]
        detection["height_m"] = _decode_altitude(height_raw)

    @staticmethod
    def _parse_system(unit: bytes, detection: dict) -> None:
        lat_raw = struct.unpack_from("<i", unit, 2)[0]
        lon_raw = struct.unpack_from("<i", unit, 6)[0]
        detection["operator_lat"] = _decode_lat_lon(lat_raw)
        detection["operator_lon"] = _decode_lat_lon(lon_raw)

    @staticmethod
    def _parse_operator_id(unit: bytes, detection: dict) -> None:
        op_id_bytes = unit[2:22]
        detection["operator_id"] = op_id_bytes.rstrip(b"\x00").decode("ascii", errors="replace")
