"""Unit tests for modules/dump1090.py — all HTTP calls and subprocess mocked."""

import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import modules.dump1090  # noqa: F401 — load before @patch resolves targets


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def _mock_response(status: int, json_data=None):
    resp = AsyncMock()
    resp.status = status
    resp.json = AsyncMock(return_value=json_data if json_data is not None else {})
    cm = AsyncMock()
    cm.__aenter__ = AsyncMock(return_value=resp)
    cm.__aexit__ = AsyncMock(return_value=False)
    return cm


def _mock_session(get_status=200, get_json=None):
    session = MagicMock()
    session.closed = False
    session.close = AsyncMock()
    session.get = MagicMock(return_value=_mock_response(get_status, get_json))
    return session


_SAMPLE_AIRCRAFT_JSON = {
    "now": 1700000000.0,
    "messages": 12345,
    "aircraft": [
        {
            "hex": "4b1614",
            "flight": "BAW123  ",
            "alt_baro": 35000,
            "gs": 450.3,
            "track": 123.4,
            "lat": 51.5,
            "lon": -0.1,
            "squawk": "1234",
            "emergency": "none",
            "seen": 0.4,
            "rssi": -15.3,
        }
    ],
}

_SAMPLE_ENRICH_RESPONSE = {
    "ac": [
        {
            "hex": "4b1614",
            "r": "G-EUOE",
            "t": "A319",
            "ownOp": "British Airways",
            "dbFlags": 0,
        }
    ],
    "total": 1,
}


# ---------------------------------------------------------------------------
# connect()
# ---------------------------------------------------------------------------

class TestADSBModuleConnect(unittest.TestCase):

    @patch("modules.dump1090.aiohttp.ClientSession")
    def test_connect_succeeds_when_readsb_responding(self, MockSession):
        """connect() should succeed when readsb returns 200."""
        from modules.dump1090 import ADSBModule

        MockSession.return_value = _mock_session(
            get_status=200, get_json=_SAMPLE_AIRCRAFT_JSON
        )
        m = ADSBModule()
        _run(m.connect())
        self.assertIsNotNone(m._session)
        _run(m.close())

    @patch("modules.dump1090.aiohttp.ClientSession")
    def test_connect_raises_when_readsb_unreachable(self, MockSession):
        """connect() should raise ConnectionError when readsb is not reachable."""
        import aiohttp
        from modules.dump1090 import ADSBModule

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

        m = ADSBModule()
        with self.assertRaises(ConnectionError):
            _run(m.connect())

    @patch("modules.dump1090.aiohttp.ClientSession")
    def test_connect_warns_when_no_aircraft(self, MockSession):
        """connect() should succeed (not raise) when readsb reports no aircraft."""
        from modules.dump1090 import ADSBModule

        MockSession.return_value = _mock_session(
            get_status=200, get_json={"now": 1700000000.0, "messages": 0, "aircraft": []}
        )
        m = ADSBModule()
        _run(m.connect())  # must not raise
        self.assertIsNotNone(m._session)
        _run(m.close())


# ---------------------------------------------------------------------------
# poll_aircraft()
# ---------------------------------------------------------------------------

class TestADSBModulePollAircraft(unittest.TestCase):

    def _connected_module(self, MockSession, get_json=None, gps_fix=None):
        from modules.dump1090 import ADSBModule

        gps = MagicMock()
        gps.get_fix = MagicMock(return_value=gps_fix)

        json_data = get_json if get_json is not None else _SAMPLE_AIRCRAFT_JSON
        MockSession.return_value = _mock_session(get_status=200, get_json=json_data)
        m = ADSBModule(gps_module=gps)
        _run(m.connect())
        return m

    @patch("modules.dump1090.aiohttp.ClientSession")
    def test_poll_aircraft_returns_list(self, MockSession):
        m = self._connected_module(MockSession)
        result = _run(m.poll_aircraft())
        self.assertIsInstance(result, list)
        _run(m.close())

    @patch("modules.dump1090.aiohttp.ClientSession")
    def test_poll_aircraft_required_fields(self, MockSession):
        """Each aircraft record must contain all required fields."""
        gps_fix = {"lat": 51.5, "lon": -0.1, "utc": "2024-01-15T12:00:00Z"}
        m = self._connected_module(MockSession, gps_fix=gps_fix)
        result = _run(m.poll_aircraft())

        self.assertEqual(len(result), 1)
        ac = result[0]
        for field in ("icao", "callsign", "lat", "lon", "altitude", "speed",
                      "track", "squawk", "seen", "rssi", "emergency",
                      "gps_lat", "gps_lon", "gps_utc"):
            self.assertIn(field, ac, f"missing field: {field}")
        _run(m.close())

    @patch("modules.dump1090.aiohttp.ClientSession")
    def test_poll_aircraft_gps_stamp(self, MockSession):
        """poll_aircraft() should stamp each record with GPS fix."""
        gps_fix = {"lat": 51.5, "lon": -0.1, "utc": "2024-01-15T12:00:00Z"}
        m = self._connected_module(MockSession, gps_fix=gps_fix)
        result = _run(m.poll_aircraft())

        self.assertEqual(result[0]["gps_lat"], 51.5)
        self.assertEqual(result[0]["gps_lon"], -0.1)
        _run(m.close())

    @patch("modules.dump1090.aiohttp.ClientSession")
    def test_poll_aircraft_returns_empty_when_no_aircraft(self, MockSession):
        """poll_aircraft() should return [] when no aircraft in view."""
        m = self._connected_module(
            MockSession,
            get_json={"now": 1700000000.0, "messages": 0, "aircraft": []}
        )
        result = _run(m.poll_aircraft())
        self.assertEqual(result, [])
        _run(m.close())

    @patch("modules.dump1090.aiohttp.ClientSession")
    def test_poll_aircraft_returns_empty_before_connect(self, MockSession):
        """poll_aircraft() should return [] gracefully before connect()."""
        from modules.dump1090 import ADSBModule
        m = ADSBModule()
        result = _run(m.poll_aircraft())
        self.assertEqual(result, [])


# ---------------------------------------------------------------------------
# enrich_aircraft()
# ---------------------------------------------------------------------------

class TestADSBModuleEnrich(unittest.TestCase):

    @patch("modules.dump1090.ADSBXLOL_API_KEY", "test-api-key")
    @patch("modules.dump1090.aiohttp.ClientSession")
    def test_enrich_returns_dict_when_api_available(self, MockSession):
        """enrich_aircraft() should return registration/type/operator when API responds."""
        from modules.dump1090 import ADSBModule

        session = MagicMock()
        session.closed = False
        session.close = AsyncMock()
        # connect call
        session.get = MagicMock(
            side_effect=[
                _mock_response(200, _SAMPLE_AIRCRAFT_JSON),   # connect
                _mock_response(200, _SAMPLE_ENRICH_RESPONSE), # enrich
            ]
        )
        MockSession.return_value = session

        m = ADSBModule()
        _run(m.connect())
        result = _run(m.enrich_aircraft("4b1614"))

        self.assertIn("registration", result)
        self.assertIn("aircraft_type", result)
        self.assertIn("operator", result)
        self.assertIn("military", result)
        self.assertEqual(result["registration"], "G-EUOE")
        self.assertEqual(result["aircraft_type"], "A319")
        self.assertFalse(result["military"])
        _run(m.close())

    @patch("modules.dump1090.ADSBXLOL_API_KEY", "")
    @patch("modules.dump1090.aiohttp.ClientSession")
    def test_enrich_returns_empty_dict_when_api_key_not_set(self, MockSession):
        """enrich_aircraft() should return {} when ADSBXLOL_API_KEY is empty."""
        from modules.dump1090 import ADSBModule

        MockSession.return_value = _mock_session(
            get_status=200, get_json=_SAMPLE_AIRCRAFT_JSON
        )
        m = ADSBModule()
        _run(m.connect())
        result = _run(m.enrich_aircraft("4b1614"))
        self.assertEqual(result, {})
        _run(m.close())


# ---------------------------------------------------------------------------
# is_hardware_present()
# ---------------------------------------------------------------------------

class TestADSBModuleHardware(unittest.TestCase):

    @patch("modules.dump1090.subprocess.run")
    def test_hardware_present_with_known_usb_id(self, mock_run):
        """is_hardware_present() should return True when RTL-SDR IDs appear in lsusb."""
        from modules.dump1090 import ADSBModule

        mock_run.return_value = MagicMock(
            stdout="Bus 001 Device 003: ID 0bda:2838 Realtek RTL2838UHIDIR\n",
            returncode=0,
        )
        m = ADSBModule()
        self.assertTrue(m.is_hardware_present())

    @patch("modules.dump1090.subprocess.run")
    def test_hardware_not_present_without_rtlsdr(self, mock_run):
        """is_hardware_present() should return False when no RTL-SDR in lsusb."""
        from modules.dump1090 import ADSBModule

        mock_run.return_value = MagicMock(
            stdout="Bus 001 Device 001: ID 1d6b:0002 Linux Foundation 2.0 root hub\n",
            returncode=0,
        )
        m = ADSBModule()
        self.assertFalse(m.is_hardware_present())


if __name__ == "__main__":
    unittest.main()
