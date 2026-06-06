"""Unit tests for modules/kismet.py — aiohttp responses are fully mocked."""

import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import modules.kismet  # noqa: F401 — ensure module loaded for @patch resolution


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _run(coro):
    return asyncio.run(coro)


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
        "kismet.common.signal.last_signal": -72,
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
                      "probe_ssids", "probe_fingerprint", "num_probed_ssids",
                      "gps_lat", "gps_lon", "gps_utc"):
            self.assertIn(field, device, f"missing field: {field}")
        # Guard #51: signal must be read from the real nested leaf key,
        # not dropped to None. Mock mirrors Kismet's actual field path.
        self.assertEqual(device["last_signal"], -72)
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


# ---------------------------------------------------------------------------
# Probe-SSID + fingerprint extraction
# Mocks mirror the EXACT live Kismet nesting: probed_ssid_map is a LIST of
# records, each SSID at dot11.probedssid.ssid; the "" entry is the wildcard.
# ---------------------------------------------------------------------------


def _probe_device(probed_map, fingerprint=None, num=None, mac="11:22:33:44:55:66"):
    d = {
        "kismet.device.base.macaddr": mac,
        "kismet.device.base.type": "Wi-Fi Client",
        "kismet.device.base.name": "",
        "kismet.device.base.manuf": "Acme",
        "kismet.device.base.phyname": "IEEE802.11",
        "kismet.device.base.first_time": 1700000000,
        "kismet.device.base.last_time": 1700000060,
        "kismet.common.signal.last_signal": -55,
    }
    if probed_map is not None:
        d["dot11.device.probed_ssid_map"] = probed_map
    if fingerprint is not None:
        d["dot11.device.probe_fingerprint"] = fingerprint
    if num is not None:
        d["dot11.device.num_probed_ssids"] = num
    return d


def _rec(ssid):
    return {"dot11.probedssid.ssid": ssid, "dot11.probedssid.ssidlen": len(ssid),
            "dot11.probedssid.first_time": 1700000000, "dot11.probedssid.last_time": 1700000060}


class TestKismetProbeExtraction(unittest.TestCase):

    def _poll(self, MockSession, devices):
        from modules.kismet import KismetModule
        gps = MagicMock(); gps.get_fix = MagicMock(return_value=None)
        with patch("modules.kismet.KISMET_API_KEY", "valid-key"):
            MockSession.return_value = _mock_session(get_status=200, post_status=200, post_json=devices)
            km = KismetModule(gps_module=gps)
            _run(km.connect())
        result = _run(km.poll_devices())
        _run(km.close())
        return result

    @patch("modules.kismet.aiohttp.ClientSession")
    def test_wildcard_excluded_named_preserved_in_order(self, MockSession):
        dev = _probe_device([_rec(""), _rec("NETGEAR13"), _rec("HomeWiFi")],
                            fingerprint=1585625513, num=3)
        r = self._poll(MockSession, [dev])[0]
        self.assertEqual(r["probe_ssids"], ["NETGEAR13", "HomeWiFi"])
        self.assertEqual(r["probe_fingerprint"], 1585625513)
        self.assertEqual(r["num_probed_ssids"], 3)

    @patch("modules.kismet.aiohttp.ClientSession")
    def test_only_wildcard_yields_empty(self, MockSession):
        r = self._poll(MockSession, [_probe_device([_rec("")], fingerprint=42, num=1)])[0]
        self.assertEqual(r["probe_ssids"], [])

    @patch("modules.kismet.aiohttp.ClientSession")
    def test_absent_map_yields_empty_none_zero(self, MockSession):
        r = self._poll(MockSession, [_probe_device(None)])[0]
        self.assertEqual(r["probe_ssids"], [])
        self.assertIsNone(r["probe_fingerprint"])
        self.assertEqual(r["num_probed_ssids"], 0)

    @patch("modules.kismet.aiohttp.ClientSession")
    def test_duplicate_named_ssids_deduplicated(self, MockSession):
        dev = _probe_device([_rec("HomeWiFi"), _rec("HomeWiFi"), _rec("Cafe")])
        r = self._poll(MockSession, [dev])[0]
        self.assertEqual(r["probe_ssids"], ["HomeWiFi", "Cafe"])

    @patch("modules.kismet.aiohttp.ClientSession")
    def test_whitespace_only_ssids_excluded(self, MockSession):
        dev = _probe_device([_rec(""), _rec("   "), _rec("\t"), _rec("Real")])
        r = self._poll(MockSession, [dev])[0]
        self.assertEqual(r["probe_ssids"], ["Real"])

    @patch("modules.kismet.aiohttp.ClientSession")
    def test_fingerprint_and_count_read_as_integers(self, MockSession):
        dev = _probe_device([_rec("X")], fingerprint=1585625513, num=2)
        r = self._poll(MockSession, [dev])[0]
        self.assertIsInstance(r["probe_fingerprint"], int)
        self.assertEqual(r["probe_fingerprint"], 1585625513)
        self.assertIsInstance(r["num_probed_ssids"], int)
        self.assertEqual(r["num_probed_ssids"], 2)


if __name__ == "__main__":
    unittest.main()
