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
        """Helper: return a connected KismetModule with mocked session.

        ``gps_fix`` is stashed on the module so each test can pass it through to
        ``poll_devices(gps_fix=...)``; the module no longer reads GPS itself.
        """
        from modules.kismet import KismetModule

        with patch("modules.kismet.KISMET_API_KEY", "valid-key"):
            MockSession.return_value = _mock_session(
                get_status=200,
                post_status=post_status,
                post_json=post_json if post_json is not None else [],
            )
            km = KismetModule()
            _run(km.connect())
        km._test_gps_fix = gps_fix
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
        result = _run(km.poll_devices(gps_fix=km._test_gps_fix))

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
        result = _run(km.poll_devices(gps_fix=km._test_gps_fix))

        self.assertEqual(result[0]["gps_lat"], 51.5)
        self.assertEqual(result[0]["gps_lon"], -0.1)
        self.assertEqual(result[0]["gps_utc"], "2024-01-15T12:00:00Z")
        _run(km.close())

    @patch("modules.kismet.aiohttp.ClientSession")
    def test_poll_devices_gps_none_when_no_fix(self, MockSession):
        """poll_devices() should set gps_* fields to None when GPS has no fix."""
        km = self._connected_km(MockSession, post_json=_SAMPLE_DEVICES, gps_fix=None)
        result = _run(km.poll_devices(gps_fix=km._test_gps_fix))

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

    @patch("modules.kismet.aiohttp.ClientSession")
    def test_poll_devices_no_gps_module_required(self, MockSession):
        """The module no longer needs a gps_module and never reads gpsd itself.

        It is constructed without a GPSModule, and stamps location only from the
        fix the orchestrator passes in — proving the decoupling that stops the
        shared-socket wedge.
        """
        from modules.kismet import KismetModule

        with patch("modules.kismet.KISMET_API_KEY", "valid-key"):
            MockSession.return_value = _mock_session(
                get_status=200, post_status=200, post_json=_SAMPLE_DEVICES,
            )
            km = KismetModule()  # no gps_module
            _run(km.connect())

        gps_fix = {"lat": 51.5, "lon": -0.1, "utc": "2024-01-15T12:00:00Z"}
        result = _run(km.poll_devices(gps_fix=gps_fix))
        self.assertEqual(result[0]["gps_lat"], 51.5)
        self.assertEqual(result[0]["gps_lon"], -0.1)
        self.assertEqual(result[0]["gps_utc"], "2024-01-15T12:00:00Z")
        _run(km.close())


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


def _ap_device(ssid="HomeWiFi", channel="6", mac="ff:ee:dd:cc:bb:aa", wps=None):
    """A beaconing AP. Kismet carries the beaconed SSID in base.name (the nested
    advertisedssid.ssid field returns a placeholder, confirmed against the live API).

    ``wps`` is an optional dict of WPS leaf-short names (manuf/model/model_number/
    serial/device_name) -> value, embedded in an advertised-SSID record the way the
    live daemon returns them (leaf keys dot11.advertisedssid.wps_*)."""
    dev = {
        "kismet.device.base.macaddr": mac,
        "kismet.device.base.type": "Wi-Fi AP",
        "kismet.device.base.name": ssid,
        "kismet.device.base.manuf": "Netgear",
        "kismet.device.base.phyname": "IEEE802.11",
        "kismet.device.base.first_time": 1700000000,
        "kismet.device.base.last_time": 1700000060,
        "kismet.common.signal.last_signal": -42,
        "kismet.device.base.channel": channel,
    }
    if wps is not None:
        leaf = {"manuf": "dot11.advertisedssid.wps_manuf",
                "model": "dot11.advertisedssid.wps_model_name",
                "model_number": "dot11.advertisedssid.wps_model_number",
                "serial": "dot11.advertisedssid.wps_serial_number",
                "device_name": "dot11.advertisedssid.wps_device_name"}
        rec = {"dot11.advertisedssid.ssid": ssid, "dot11.advertisedssid.wps_version": 16}
        for k, v in wps.items():
            rec[leaf[k]] = v
        dev["dot11.device.advertised_ssid_map"] = [rec]
    return dev


class TestKismetProbeExtraction(unittest.TestCase):

    def _poll(self, MockSession, devices):
        from modules.kismet import KismetModule
        with patch("modules.kismet.KISMET_API_KEY", "valid-key"):
            MockSession.return_value = _mock_session(get_status=200, post_status=200, post_json=devices)
            km = KismetModule()
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
    def test_ap_beaconed_ssid_from_base_name(self, MockSession):
        r = self._poll(MockSession, [_ap_device(ssid="HomeWiFi", channel="6")])[0]
        self.assertTrue(r["is_ap"])
        self.assertEqual(r["beaconed_ssid"], "HomeWiFi")
        self.assertEqual(r["beacon_channel"], "6")

    @patch("modules.kismet.aiohttp.ClientSession")
    def test_hidden_ap_named_by_mac_yields_no_ssid(self, MockSession):
        # A hidden AP names itself by its MAC; that's not a real SSID, so it's dropped.
        r = self._poll(MockSession, [_ap_device(ssid="aa:bb:cc:dd:ee:ff", mac="aa:bb:cc:dd:ee:ff")])[0]
        self.assertTrue(r["is_ap"])
        self.assertEqual(r["beaconed_ssid"], "")

    @patch("modules.kismet.aiohttp.ClientSession")
    def test_client_has_no_beaconed_ssid(self, MockSession):
        r = self._poll(MockSession, [_probe_device([_rec("HomeWiFi")])])[0]
        self.assertFalse(r["is_ap"])
        self.assertEqual(r["beaconed_ssid"], "")

    @patch("modules.kismet.aiohttp.ClientSession")
    def test_ap_wps_identity_extracted_and_fingerprinted(self, MockSession):
        wps = {"manuf": "Technicolor", "model": "TC8717T",
               "model_number": "123456", "serial": "0000001",
               "device_name": "TechnicolorAP"}
        r = self._poll(MockSession, [_ap_device(ssid="Net", wps=wps)])[0]
        self.assertEqual(r["wps"]["manuf"], "Technicolor")
        self.assertEqual(r["wps"]["serial"], "0000001")
        self.assertEqual(r["wps"]["device_name"], "TechnicolorAP")
        self.assertTrue(r["wps_fingerprint"].startswith("wps-fp:"))

    @patch("modules.kismet.aiohttp.ClientSession")
    def test_ap_without_wps_has_empty_identity(self, MockSession):
        r = self._poll(MockSession, [_ap_device(ssid="Net")])[0]
        self.assertEqual(r["wps"], {})
        self.assertEqual(r["wps_fingerprint"], "")

    @patch("modules.kismet.aiohttp.ClientSession")
    def test_ap_with_only_manufacturer_wps_gets_no_fingerprint(self, MockSession):
        # Bare manufacturer can't discriminate distinct APs -> no fingerprint (but the
        # attribute is still captured for the label).
        r = self._poll(MockSession, [_ap_device(ssid="Net", wps={"manuf": "Technicolor"})])[0]
        self.assertEqual(r["wps"]["manuf"], "Technicolor")
        self.assertEqual(r["wps_fingerprint"], "")

    @patch("modules.kismet.aiohttp.ClientSession")
    def test_client_never_gets_wps(self, MockSession):
        r = self._poll(MockSession, [_probe_device([_rec("HomeWiFi")])])[0]
        self.assertEqual(r["wps"], {})
        self.assertEqual(r["wps_fingerprint"], "")

    @patch("modules.kismet.aiohttp.ClientSession")
    def test_fingerprint_and_count_read_as_integers(self, MockSession):
        dev = _probe_device([_rec("X")], fingerprint=1585625513, num=2)
        r = self._poll(MockSession, [dev])[0]
        self.assertIsInstance(r["probe_fingerprint"], int)
        self.assertEqual(r["probe_fingerprint"], 1585625513)
        self.assertIsInstance(r["num_probed_ssids"], int)
        self.assertEqual(r["num_probed_ssids"], 2)


# ---------------------------------------------------------------------------
# KISMET_ACTIVE_WINDOW_SECONDS filtering
# Kismet's device list is permanent — it retains every device heard in the
# session. On a mobile node, a device passed 10 min ago still appears in
# every poll; the persistence engine stamps it with the node's current GPS
# position, creating spurious "following" clusters. The active-window filter
# drops devices whose last_time is older than the configured threshold so
# only currently-in-range devices reach the scoring engine.
# ---------------------------------------------------------------------------

def _device_entry(mac="AA:BB:CC:DD:EE:FF", last_time=None):
    """Minimal Kismet device entry with a controllable last_time."""
    return {
        "kismet.device.base.macaddr": mac,
        "kismet.device.base.type": "Wi-Fi Device",
        "kismet.device.base.name": "",
        "kismet.device.base.manuf": "Acme",
        "kismet.device.base.phyname": "IEEE802.11",
        "kismet.device.base.first_time": 1700000000,
        "kismet.device.base.last_time": last_time if last_time is not None else 1700000000,
        "kismet.common.signal.last_signal": -60,
    }


class TestKismetActiveWindow(unittest.TestCase):
    """KISMET_ACTIVE_WINDOW_SECONDS controls stale-device filtering."""

    def _poll(self, MockSession, devices, env_override=None):
        import os
        from modules.kismet import KismetModule

        with patch("modules.kismet.KISMET_API_KEY", "valid-key"):
            MockSession.return_value = _mock_session(
                get_status=200, post_status=200, post_json=devices,
            )
            km = KismetModule()
            _run(km.connect())

        env = env_override or {}
        with patch.dict(os.environ, env):
            result = _run(km.poll_devices())
        _run(km.close())
        return result

    @patch("modules.kismet.aiohttp.ClientSession")
    def test_window_disabled_by_default(self, MockSession):
        """Default (0) keeps all devices regardless of last_time."""
        import time
        stale_time = int(time.time()) - 3600  # 1 hour ago
        devices = [
            _device_entry("AA:BB:CC:DD:EE:01", last_time=stale_time),
            _device_entry("AA:BB:CC:DD:EE:02", last_time=stale_time),
        ]
        result = self._poll(MockSession, devices)
        self.assertEqual(len(result), 2)

    @patch("modules.kismet.aiohttp.ClientSession")
    def test_fresh_device_kept(self, MockSession):
        """A device heard within the window is returned."""
        import time
        fresh_time = int(time.time()) - 10  # 10 s ago
        devices = [_device_entry("AA:BB:CC:DD:EE:01", last_time=fresh_time)]
        result = self._poll(
            MockSession, devices,
            env_override={"KISMET_ACTIVE_WINDOW_SECONDS": "90"},
        )
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["macaddr"], "AA:BB:CC:DD:EE:01")

    @patch("modules.kismet.aiohttp.ClientSession")
    def test_stale_device_dropped(self, MockSession):
        """A device last heard outside the window is excluded."""
        import time
        stale_time = int(time.time()) - 300  # 5 min ago — outside 90 s window
        devices = [_device_entry("AA:BB:CC:DD:EE:FF", last_time=stale_time)]
        result = self._poll(
            MockSession, devices,
            env_override={"KISMET_ACTIVE_WINDOW_SECONDS": "90"},
        )
        self.assertEqual(result, [])

    @patch("modules.kismet.aiohttp.ClientSession")
    def test_mixed_fresh_and_stale(self, MockSession):
        """Only devices within the window survive; stale ones are dropped."""
        import time
        now = int(time.time())
        devices = [
            _device_entry("AA:BB:CC:DD:EE:01", last_time=now - 30),   # fresh
            _device_entry("AA:BB:CC:DD:EE:02", last_time=now - 200),  # stale
            _device_entry("AA:BB:CC:DD:EE:03", last_time=now - 5),    # fresh
        ]
        result = self._poll(
            MockSession, devices,
            env_override={"KISMET_ACTIVE_WINDOW_SECONDS": "90"},
        )
        macs = [r["macaddr"] for r in result]
        self.assertIn("AA:BB:CC:DD:EE:01", macs)
        self.assertNotIn("AA:BB:CC:DD:EE:02", macs)
        self.assertIn("AA:BB:CC:DD:EE:03", macs)

    @patch("modules.kismet.aiohttp.ClientSession")
    def test_zero_last_time_kept_when_window_set(self, MockSession):
        """Devices with last_time=0 (field absent) bypass the filter — they
        can't be compared reliably and should not be silently dropped."""
        devices = [_device_entry("AA:BB:CC:DD:EE:FF", last_time=0)]
        result = self._poll(
            MockSession, devices,
            env_override={"KISMET_ACTIVE_WINDOW_SECONDS": "90"},
        )
        self.assertEqual(len(result), 1)


# ---------------------------------------------------------------------------
# connect() must not block the event loop on the `iw dev` subprocess
# (get_interface_status shells out; connect() runs on the asyncio loop and is
# retried at startup / awaited on reconnect while the watchdog heartbeat is live)
# ---------------------------------------------------------------------------


class TestKismetConnectOffloadsInterfaceCheck(unittest.TestCase):

    @patch("modules.kismet.KISMET_API_KEY", "valid-key")
    @patch("modules.kismet.aiohttp.ClientSession")
    def test_interface_status_runs_off_the_loop_thread(self, MockSession):
        """The blocking interface check must run in a worker thread, not on the
        event loop thread."""
        import threading

        from modules.kismet import KismetModule

        MockSession.return_value = _mock_session(get_status=200)
        km = KismetModule()

        loop_thread = {}
        call_thread = {}

        real_status = km.get_interface_status

        def _record_thread():
            call_thread["id"] = threading.get_ident()
            return {"interface": "wlan1", "mode": "monitor",
                    "phy": "phy0", "is_monitor": True}

        km.get_interface_status = _record_thread

        async def _go():
            loop_thread["id"] = threading.get_ident()
            await km.connect()

        _run(_go())
        self.assertIn("id", call_thread)
        self.assertNotEqual(call_thread["id"], loop_thread["id"])
        _run(km.close())
        del real_status

    @patch("modules.kismet.KISMET_API_KEY", "valid-key")
    @patch("modules.kismet.aiohttp.ClientSession")
    def test_connect_still_warns_when_not_monitor(self, MockSession):
        """Offloading the check must not lose the not-in-monitor warning."""
        from modules.kismet import KismetModule

        MockSession.return_value = _mock_session(get_status=200)
        km = KismetModule()
        km.get_interface_status = lambda: {
            "interface": "wlan1", "mode": "managed",
            "phy": "phy0", "is_monitor": False,
        }

        with self.assertLogs("modules.kismet", level="WARNING") as cm:
            _run(km.connect())
        self.assertTrue(any("not monitor" in m for m in cm.output))
        _run(km.close())


if __name__ == "__main__":
    unittest.main()
