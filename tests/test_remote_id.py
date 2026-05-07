"""Tests for modules/remote_id.py — RemoteIDModule (ASTM F3411-22a)."""

import struct
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from modules.remote_id import (
    RemoteIDModule,
    _REMOTE_ID_OUI,
    _REMOTE_ID_VENDOR_TYPE,
    _IE_TAG_VENDOR_SPECIFIC,
    _decode_altitude,
    _decode_lat_lon,
)


# ---------------------------------------------------------------------------
# Helpers — build synthetic ASTM F3411-22a wire payloads
# ---------------------------------------------------------------------------


def _build_basic_id_msg(
    ua_type: int = 2, id_type: int = 1, uas_id: str = "TEST-SERIAL-001",
) -> bytes:
    """Build a 25-byte Basic ID message unit (type 0)."""
    msg = bytearray(25)
    msg[0] = 0x00  # msg_type=0, protocol_version=0
    msg[1] = (id_type << 4) | ua_type
    id_bytes = uas_id.encode("ascii")[:20].ljust(20, b"\x00")
    msg[2:22] = id_bytes
    return bytes(msg)


def _build_location_msg(
    status: int = 2,
    lat_deg: float = 51.5,
    lon_deg: float = -0.1,
    alt_m: float = 100.0,
    speed_mps: float = 5.0,
) -> bytes:
    """Build a 25-byte Location/Vector message unit (type 1)."""
    msg = bytearray(25)
    msg[0] = 0x10  # msg_type=1, protocol_version=0
    msg[1] = (status << 4)
    msg[2] = 90  # heading ~126 deg
    msg[3] = int(speed_mps / 0.25) & 0xFF
    struct.pack_into("b", msg, 4, 0)  # vert speed = 0
    lat_raw = int(lat_deg * 1e7)
    lon_raw = int(lon_deg * 1e7)
    struct.pack_into("<i", msg, 5, lat_raw)
    struct.pack_into("<i", msg, 9, lon_raw)
    # alt: raw = (alt_m + 1000) * 2
    alt_raw = int((alt_m + 1000.0) * 2.0)
    struct.pack_into("<H", msg, 13, alt_raw)  # baro
    struct.pack_into("<H", msg, 15, alt_raw)  # geodetic
    struct.pack_into("<H", msg, 17, alt_raw)  # height
    return bytes(msg)


def _build_system_msg(op_lat: float = 51.4, op_lon: float = -0.2) -> bytes:
    """Build a 25-byte System message unit (type 4)."""
    msg = bytearray(25)
    msg[0] = 0x40  # msg_type=4
    lat_raw = int(op_lat * 1e7)
    lon_raw = int(op_lon * 1e7)
    struct.pack_into("<i", msg, 2, lat_raw)
    struct.pack_into("<i", msg, 6, lon_raw)
    return bytes(msg)


def _build_operator_id_msg(operator_id: str = "GBR-OP-001") -> bytes:
    """Build a 25-byte Operator ID message unit (type 5)."""
    msg = bytearray(25)
    msg[0] = 0x50  # msg_type=5
    op_bytes = operator_id.encode("ascii")[:20].ljust(20, b"\x00")
    msg[2:22] = op_bytes
    return bytes(msg)


def _build_rid_ie(messages: list[bytes]) -> bytes:
    """Wrap one or more 25-byte message units in a Vendor Specific IE.

    Returns the complete IE bytes including tag+length header.
    """
    body = _REMOTE_ID_OUI + bytes([_REMOTE_ID_VENDOR_TYPE, 0x01])  # counter=1
    for msg in messages:
        body += msg
    return bytes([_IE_TAG_VENDOR_SPECIFIC, len(body)]) + body


def _build_kismet_device(
    mac: str = "FA:0B:BC:01:02:03",
    phy: str = "IEEE802.11",
    rssi: int = -65,
    ie_bytes: bytes = b"",
) -> dict:
    """Build a minimal Kismet device dict with IE tag data."""
    tag_list = [_IE_TAG_VENDOR_SPECIFIC] if ie_bytes else []
    return {
        "kismet.device.base.macaddr": mac,
        "kismet.device.base.phyname": phy,
        "kismet.device.base.signal/last_signal": rssi,
        "dot11.device.last_beaconed_ssid_record/dot11.advertisedssid.ie_tag_list": tag_list,
        "dot11.device.last_beaconed_ssid_record/dot11.advertisedssid.ie_tag_content": (
            ie_bytes.hex() if ie_bytes else None
        ),
    }


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def module():
    m = RemoteIDModule(gps_module=None)
    return m


@pytest.fixture()
def module_with_gps():
    gps = MagicMock()
    gps.get_fix.return_value = {"lat": 51.5, "lon": -0.1}
    return RemoteIDModule(gps_module=gps)


# ---------------------------------------------------------------------------
# connect()
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_connect_raises_when_api_key_absent(module):
    with patch.dict("os.environ", {}, clear=True):
        import modules.remote_id as rid_mod
        original = rid_mod._KISMET_API_KEY
        rid_mod._KISMET_API_KEY = ""
        try:
            with pytest.raises(ConnectionError, match="KISMET_API_KEY"):
                await module.connect()
        finally:
            rid_mod._KISMET_API_KEY = original


@pytest.mark.asyncio
async def test_connect_raises_on_401(module):
    import modules.remote_id as rid_mod
    original = rid_mod._KISMET_API_KEY
    rid_mod._KISMET_API_KEY = "bad-key"
    try:
        mock_resp = AsyncMock()
        mock_resp.status = 401
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)
        mock_session = MagicMock()
        mock_session.get.return_value = mock_resp
        mock_session.close = AsyncMock()
        with patch("modules.remote_id.aiohttp.ClientSession", return_value=mock_session):
            with pytest.raises(ConnectionError, match="rejected"):
                await module.connect()
    finally:
        rid_mod._KISMET_API_KEY = original


@pytest.mark.asyncio
async def test_connect_succeeds_on_200(module):
    import modules.remote_id as rid_mod
    original = rid_mod._KISMET_API_KEY
    rid_mod._KISMET_API_KEY = "good-key"
    try:
        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)
        mock_session = MagicMock()
        mock_session.get.return_value = mock_resp
        with patch("modules.remote_id.aiohttp.ClientSession", return_value=mock_session):
            await module.connect()
        assert module._active is True
    finally:
        rid_mod._KISMET_API_KEY = original


# ---------------------------------------------------------------------------
# poll()
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_poll_returns_empty_when_session_is_none(module):
    assert module._session is None
    result = await module.poll()
    assert result == []


@pytest.mark.asyncio
async def test_poll_returns_empty_on_non_200(module):
    mock_resp = AsyncMock()
    mock_resp.status = 503
    mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_resp.__aexit__ = AsyncMock(return_value=False)
    mock_session = MagicMock()
    mock_session.post.return_value = mock_resp
    module._session = mock_session

    result = await module.poll()
    assert result == []


@pytest.mark.asyncio
async def test_poll_returns_empty_on_network_error(module):
    import aiohttp
    mock_session = MagicMock()
    mock_session.post.side_effect = aiohttp.ClientError("connection refused")
    module._session = mock_session

    result = await module.poll()
    assert result == []


@pytest.mark.asyncio
async def test_poll_advances_last_poll_epoch(module):
    """After a successful poll, _last_poll_epoch must be updated."""
    mock_resp = AsyncMock()
    mock_resp.status = 200
    mock_resp.json = AsyncMock(return_value=[])
    mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_resp.__aexit__ = AsyncMock(return_value=False)
    mock_session = MagicMock()
    mock_session.post.return_value = mock_resp
    module._session = mock_session

    initial_epoch = module._last_poll_epoch
    await module.poll()
    assert module._last_poll_epoch > initial_epoch


@pytest.mark.asyncio
async def test_poll_returns_detection_for_wifi_device(module):
    """A device with a valid Remote ID IE should produce one detection dict."""
    msgs = _build_basic_id_msg() + _build_location_msg()
    ie_bytes = _build_rid_ie([_build_basic_id_msg(), _build_location_msg()])
    device = _build_kismet_device(ie_bytes=ie_bytes)

    mock_resp = AsyncMock()
    mock_resp.status = 200
    mock_resp.json = AsyncMock(return_value=[device])
    mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_resp.__aexit__ = AsyncMock(return_value=False)
    mock_session = MagicMock()
    mock_session.post.return_value = mock_resp
    module._session = mock_session

    results = await module.poll()
    assert len(results) == 1
    d = results[0]
    assert d["event_type"] == "remote_id"
    assert d["source_mac"] == "FA:0B:BC:01:02:03"
    assert d["uas_id"] == "TEST-SERIAL-001"
    assert d["ua_type"] == "helicopter_or_multirotor"
    assert d["id_type"] == "serial_number"
    assert d["status"] == "airborne"
    assert abs(d["drone_lat"] - 51.5) < 0.01
    assert abs(d["drone_lon"] - (-0.1)) < 0.01
    assert d["drone_alt_m"] is not None


@pytest.mark.asyncio
async def test_poll_stamps_observer_gps(module_with_gps):
    """Detection must carry observer lat/lon from the GPS module."""
    ie_bytes = _build_rid_ie([_build_basic_id_msg()])
    device = _build_kismet_device(ie_bytes=ie_bytes)

    mock_resp = AsyncMock()
    mock_resp.status = 200
    mock_resp.json = AsyncMock(return_value=[device])
    mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_resp.__aexit__ = AsyncMock(return_value=False)
    mock_session = MagicMock()
    mock_session.post.return_value = mock_resp
    module_with_gps._session = mock_session

    results = await module_with_gps.poll()
    assert len(results) == 1
    assert results[0]["gps_lat"] == pytest.approx(51.5)
    assert results[0]["gps_lon"] == pytest.approx(-0.1)


@pytest.mark.asyncio
async def test_poll_handles_missing_operator_location(module):
    """Operator lat/lon should be None when no System message is present."""
    ie_bytes = _build_rid_ie([_build_basic_id_msg()])
    device = _build_kismet_device(ie_bytes=ie_bytes)

    mock_resp = AsyncMock()
    mock_resp.status = 200
    mock_resp.json = AsyncMock(return_value=[device])
    mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_resp.__aexit__ = AsyncMock(return_value=False)
    mock_session = MagicMock()
    mock_session.post.return_value = mock_resp
    module._session = mock_session

    results = await module.poll()
    assert len(results) == 1
    assert results[0]["operator_lat"] is None
    assert results[0]["operator_lon"] is None


# ---------------------------------------------------------------------------
# IE tag parsing
# ---------------------------------------------------------------------------


def test_parse_ie_tags_finds_rid_ie():
    ie_bytes = _build_rid_ie([_build_basic_id_msg()])
    payloads = RemoteIDModule._parse_ie_tags(ie_bytes)
    assert len(payloads) == 1


def test_parse_ie_tags_ignores_wrong_oui():
    # Wrong OUI
    body = bytes([0x00, 0x50, 0xC2, _REMOTE_ID_VENDOR_TYPE, 0x01]) + b"\x00" * 25
    ie_bytes = bytes([_IE_TAG_VENDOR_SPECIFIC, len(body)]) + body
    payloads = RemoteIDModule._parse_ie_tags(ie_bytes)
    assert payloads == []


def test_parse_ie_tags_ignores_no_vendor_specific():
    # SSID tag only
    ssid = b"test-network"
    ie_bytes = bytes([0x00, len(ssid)]) + ssid
    payloads = RemoteIDModule._parse_ie_tags(ie_bytes)
    assert payloads == []


def test_decode_ie_content_hex():
    raw = b"\xFA\x0B\xBC"
    assert RemoteIDModule._decode_ie_content(raw.hex()) == raw


def test_decode_ie_content_bytes():
    raw = b"\xFA\x0B\xBC"
    assert RemoteIDModule._decode_ie_content(raw) == raw


# ---------------------------------------------------------------------------
# Field mapping helpers
# ---------------------------------------------------------------------------


def test_decode_altitude_valid():
    # (alt_m + 1000) * 2 → alt_m = 100, raw = 2200
    assert _decode_altitude(2200) == pytest.approx(100.0)


def test_decode_altitude_invalid_returns_none():
    assert _decode_altitude(0xFFFF) is None


def test_decode_lat_lon_valid():
    # lat = 51.5 deg → raw = 515000000
    assert _decode_lat_lon(515000000) == pytest.approx(51.5)


def test_decode_lat_lon_invalid_returns_none():
    assert _decode_lat_lon(0x7FFFFFFF) is None


# ---------------------------------------------------------------------------
# System + Operator ID messages
# ---------------------------------------------------------------------------


def test_poll_parses_system_message(module):
    ie_bytes = _build_rid_ie([_build_basic_id_msg(), _build_system_msg(51.4, -0.2)])
    device = _build_kismet_device(ie_bytes=ie_bytes)
    detections = module._parse_device(device)
    assert len(detections) == 1
    assert abs(detections[0]["operator_lat"] - 51.4) < 0.01
    assert abs(detections[0]["operator_lon"] - (-0.2)) < 0.01


def test_poll_parses_operator_id_message(module):
    ie_bytes = _build_rid_ie([_build_basic_id_msg(), _build_operator_id_msg("GBR-OP-001")])
    device = _build_kismet_device(ie_bytes=ie_bytes)
    detections = module._parse_device(device)
    assert len(detections) == 1
    assert detections[0]["operator_id"] == "GBR-OP-001"


# ---------------------------------------------------------------------------
# Graceful degradation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_module_returns_empty_list_not_exception_on_error(module):
    """poll() must return [] on error, never raise."""
    module._session = MagicMock()
    module._session.post.side_effect = RuntimeError("unexpected")

    result = await module.poll()
    assert result == []
