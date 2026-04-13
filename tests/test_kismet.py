"""Unit tests for modules/kismet.py — aiohttp responses are fully mocked."""

import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import modules.kismet  # noqa: F401 — ensure module loaded for @patch resolution


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def _mock_response(status: int, json_data=None):
    """Return a mock aiohttp response context manager."""
    resp = AsyncMock()
    resp.status = status
    resp.json = AsyncMock(return_value=json_data if json_data is not None else {})
    cm = AsyncMock()
    cm.__aenter__ = AsyncMock(return_value=resp)
    cm.__aexit__ = AsyncMock(return_value=False)
    return cm


def _mock_session(get_status=200, get_json=None, post_status=200, post_json=None):
    """Return a mock aiohttp.ClientSession."""
    session = MagicMock()
    session.closed = False
    session.get = MagicMock(return_value=_mock_response(get_status, get_json))
    session.post = MagicMock(return_value=_mock_response(post_status, post_json))
    session.close = AsyncMock()
    return session


# ---------------------------------------------------------------------------
# connect()
# ---------------------------------------------------------------------------

class TestKismetModuleConnect(unittest.TestCase):

    @patch("modules.kismet.KISMET_API_KEY", "valid-api-key")
    @patch("modules.kismet.aiohttp.ClientSession")
    def test_connect_succeeds_with_valid_api_key(self, MockSession):
        """connect() should succeed when Kismet returns 200."""
        from modules.kismet import KismetModule

        MockSession.return_value = _mock_session(get_status=200)

        km = KismetModule()
        _run(km.connect())

        self.assertIsNotNone(km._session)
        _run(km.close())

    @patch("modules.kismet.KISMET_API_KEY", "")
    def test_connect_raises_when_api_key_missing(self):
        """connect() should raise ConnectionError when KISMET_API_KEY is empty."""
        from modules.kismet import KismetModule

        km = KismetModule()
        with self.assertRaises(ConnectionError):
            _run(km.connect())

    @patch("modules.kismet.KISMET_API_KEY", "bad-key")
    @patch("modules.kismet.aiohttp.ClientSession")
    def test_connect_raises_on_401(self, MockSession):
        """connect() should raise ConnectionError when Kismet returns 401."""
        from modules.kismet import KismetModule

        MockSession.return_value = _mock_session(get_status=401)

        km = KismetModule()
        with self.assertRaises(ConnectionError):
            _run(km.connect())

    @patch("modules.kismet.KISMET_API_KEY", "valid-api-key")
    @patch("modules.kismet.aiohttp.ClientSession")
    def test_connect_raises_when_kismet_unreachable(self, MockSession):
        """connect() should raise ConnectionError when Kismet is not reachable."""
        import aiohttp
        from modules.kismet import KismetModule

        session = MagicMock()
        session.closed = False
        session.close = AsyncMock()
        cm = AsyncMock()
        cm.__aenter__ = AsyncMock(
            side_effect=aiohttp.ClientConnectorError(
                MagicMock(), OSError("connection refused")
            )
        )
        cm.__aexit__ = AsyncMock(return_value=False)
        session.get = MagicMock(return_value=cm)
        MockSession.return_value = session

        km = KismetModule()
        with self.assertRaises(ConnectionError):
            _run(km.connect())


# ---------------------------------------------------------------------------
# poll_devices()
# ---------------------------------------------------------------------------

_SAMPLE_DEVICES = [
    {
        "kismet.device.base.macaddr": "AA:BB:CC:DD:EE:FF",
        "kismet.device.base.type": "Wi-Fi Device",
        "kismet.device.base.name": "TestDevice",
        "kismet.device.base.manuf": "Apple",
        "kismet.device.base.phyname": "IEEE802.11",
        "kismet.device.base.first_time": 1700000000,
        "kismet.device.base.last_time": 1700000060,
        "kismet.device.base.signal/last_signal": -72,
    }
]


class TestKismetModulePollDevices(unittest.TestCase):

    def _connected_km(self, MockSession, post_status=200, post_json=None, gps_fix=None):
        """Helper: return a connected KismetModule with mocked session."""
        from modules.kismet import KismetModule

        gps = MagicMock()
        gps.get_fix = MagicMock(return_value=gps_fix)

        with patch("modules.kismet.KISMET_API_KEY", "valid-key"):
            MockSession.return_value = _mock_session(
                get_status=200,
                post_status=post_status,
                post_json=post_json if post_json is not None else [],
            )
            km = KismetModule(gps_module=gps)
            _run(km.connect())
        return km

    @patch("modules.kismet.aiohttp.ClientSession")
    def test_poll_devices_returns_list(self, MockSession):
        """poll_devices() should return a list."""
        km = self._connected_km(MockSession, post_json=_SAMPLE_DEVICES)
        result = _run(km.poll_devices())
        self.assertIsInstance(result, list)
        _run(km.close())

    @patch("modules.kismet.aiohttp.ClientSession")
    def test_poll_devices_returns_correct_structure(self, MockSession):
        """poll_devices() should return dicts with all required fields."""
        gps_fix = {"lat": 51.5, "lon": -0.1, "utc": "2024-01-15T12:00:00Z"}
        km = self._connected_km(MockSession, post_json=_SAMPLE_DEVICES, gps_fix=gps_fix)
        result = _run(km.poll_devices())

        self.assertEqual(len(result), 1)
        device = result[0]
        for field in ("macaddr", "type", "name", "manuf", "phyname",
                      "first_time", "last_time", "last_signal",
                      "gps_lat", "gps_lon", "gps_utc"):
            self.assertIn(field, device, f"missing field: {field}")
        _run(km.close())

    @patch("modules.kismet.aiohttp.ClientSession")
    def test_poll_devices_gps_stamp(self, MockSession):
        """poll_devices() should stamp each record with GPS lat/lon/utc."""
        gps_fix = {"lat": 51.5, "lon": -0.1, "utc": "2024-01-15T12:00:00Z"}
        km = self._connected_km(MockSession, post_json=_SAMPLE_DEVICES, gps_fix=gps_fix)
        result = _run(km.poll_devices())

        self.assertEqual(result[0]["gps_lat"], 51.5)
        self.assertEqual(result[0]["gps_lon"], -0.1)
        self.assertEqual(result[0]["gps_utc"], "2024-01-15T12:00:00Z")
        _run(km.close())

    @patch("modules.kismet.aiohttp.ClientSession")
    def test_poll_devices_gps_none_when_no_fix(self, MockSession):
        """poll_devices() should set gps_* fields to None when GPS has no fix."""
        km = self._connected_km(MockSession, post_json=_SAMPLE_DEVICES, gps_fix=None)
        result = _run(km.poll_devices())

        self.assertIsNone(result[0]["gps_lat"])
        self.assertIsNone(result[0]["gps_lon"])
        self.assertIsNone(result[0]["gps_utc"])
        _run(km.close())

    @patch("modules.kismet.aiohttp.ClientSession")
    def test_poll_devices_empty_when_no_devices(self, MockSession):
        """poll_devices() should return an empty list when Kismet has no devices."""
        km = self._connected_km(MockSession, post_json=[])
        result = _run(km.poll_devices())
        self.assertEqual(result, [])
        _run(km.close())

    @patch("modules.kismet.aiohttp.ClientSession")
    def test_poll_devices_returns_empty_before_connect(self, MockSession):
        """poll_devices() should return [] gracefully if called before connect()."""
        from modules.kismet import KismetModule

        km = KismetModule()
        result = _run(km.poll_devices())
        self.assertEqual(result, [])


if __name__ == "__main__":
    unittest.main()
