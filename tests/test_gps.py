"""Unit tests for modules/gps.py — gpsd responses are fully mocked."""

import math
import os
import sys
import unittest
from unittest.mock import MagicMock, patch

# Ensure the module is loaded so @patch can resolve "modules.gps.*" targets.
import modules.gps  # noqa: F401


def _make_fix(mode: int, latitude=51.5, longitude=-0.1, altHAE=25.0,
              speed=0.5, track=270.0):
    """Return a mock gps fix object with the given attributes."""
    fix = MagicMock()
    fix.mode = mode
    fix.latitude = latitude
    fix.longitude = longitude
    fix.altHAE = altHAE
    fix.speed = speed
    fix.track = track
    return fix


def _make_session(mode: int, **fix_kwargs):
    """Return a mock GpsSession with a fix of the given mode."""
    session = MagicMock()
    session.fix = _make_fix(mode, **fix_kwargs)
    session.utc = "2024-01-15T12:00:00.000Z"
    session.read.return_value = None
    return session


class TestGPSModuleConnect(unittest.TestCase):

    @patch("modules.gps.GpsSession")
    def test_connect_succeeds(self, MockGpsSession):
        """connect() should succeed when gpsd is reachable."""
        from modules.gps import GPSModule

        MockGpsSession.return_value = _make_session(mode=0)

        gps = GPSModule()
        gps.connect()

        MockGpsSession.assert_called_once()
        self.assertIsNotNone(gps._session)

    @patch("modules.gps.GpsSession")
    def test_connect_raises_connection_error_when_unavailable(self, MockGpsSession):
        """connect() should raise ConnectionError when gpsd cannot be reached."""
        from modules.gps import GPSModule

        MockGpsSession.side_effect = Exception("connection refused")

        gps = GPSModule()
        with self.assertRaises(ConnectionError):
            gps.connect()

        self.assertIsNone(gps._session)


class TestGPSModuleGetFix(unittest.TestCase):

    @patch("modules.gps.GpsSession")
    def test_get_fix_returns_correct_dict_with_3d_fix(self, MockGpsSession):
        """get_fix() should return a correctly structured dict on a 3-D fix."""
        from modules.gps import GPSModule

        MockGpsSession.return_value = _make_session(
            mode=3, latitude=51.5, longitude=-0.1, altHAE=25.0
        )

        gps = GPSModule()
        gps.connect()
        fix = gps.get_fix()

        self.assertIsNotNone(fix)
        for key in ("lat", "lon", "alt", "speed", "track", "utc", "fix_quality"):
            self.assertIn(key, fix)
        self.assertEqual(fix["fix_quality"], 3)
        self.assertEqual(fix["lat"], 51.5)
        self.assertEqual(fix["lon"], -0.1)
        self.assertEqual(fix["alt"], 25.0)

    @patch("modules.gps.GpsSession")
    def test_get_fix_alt_is_nan_on_2d_fix(self, MockGpsSession):
        """get_fix() should return NaN for alt when the fix is 2-D only."""
        from modules.gps import GPSModule

        MockGpsSession.return_value = _make_session(mode=2)

        gps = GPSModule()
        gps.connect()
        fix = gps.get_fix()

        self.assertIsNotNone(fix)
        self.assertEqual(fix["fix_quality"], 2)
        self.assertTrue(math.isnan(fix["alt"]))

    @patch("modules.gps.GpsSession")
    def test_get_fix_returns_none_when_no_fix(self, MockGpsSession):
        """get_fix() should return None when gpsd reports no fix (mode < 2)."""
        from modules.gps import GPSModule

        MockGpsSession.return_value = _make_session(mode=0)

        gps = GPSModule()
        gps.connect()
        fix = gps.get_fix()

        self.assertIsNone(fix)

    def test_get_fix_returns_none_before_connect(self):
        """get_fix() should return None gracefully if called before connect()."""
        from modules.gps import GPSModule

        gps = GPSModule()
        fix = gps.get_fix()

        self.assertIsNone(fix)


class TestGPSModuleIsFixed(unittest.TestCase):

    def _gps_with_mode(self, MockGpsSession, mode: int):
        from modules.gps import GPSModule

        MockGpsSession.return_value = _make_session(mode=mode)
        gps = GPSModule()
        gps.connect()
        return gps

    @patch("modules.gps.GpsSession")
    def test_is_fixed_true_on_2d_fix(self, MockGpsSession):
        gps = self._gps_with_mode(MockGpsSession, mode=2)
        self.assertTrue(gps.is_fixed())

    @patch("modules.gps.GpsSession")
    def test_is_fixed_true_on_3d_fix(self, MockGpsSession):
        gps = self._gps_with_mode(MockGpsSession, mode=3)
        self.assertTrue(gps.is_fixed())

    @patch("modules.gps.GpsSession")
    def test_is_fixed_false_on_no_fix(self, MockGpsSession):
        gps = self._gps_with_mode(MockGpsSession, mode=0)
        self.assertFalse(gps.is_fixed())


class TestGPSQualityFilter(unittest.TestCase):

    @patch("modules.gps.GpsSession")
    def test_get_fix_rejects_high_hdop(self, MockGpsSession):
        """get_fix() returns None when HDOP exceeds GPS_MAX_HDOP."""
        from modules.gps import GPSModule

        session = _make_session(mode=3)
        session.fix.hdop = 10.0  # well above the 5.0 threshold
        MockGpsSession.return_value = session

        with patch.dict(os.environ, {"GPS_MAX_HDOP": "5.0", "GPS_MIN_QUALITY": "2d"}):
            gps = GPSModule()
            gps.connect()
            fix = gps.get_fix()

        self.assertIsNone(fix)

    @patch("modules.gps.GpsSession")
    def test_get_fix_accepts_low_hdop(self, MockGpsSession):
        """get_fix() returns a fix when HDOP is within GPS_MAX_HDOP."""
        from modules.gps import GPSModule

        session = _make_session(mode=3)
        session.fix.hdop = 1.5  # well below the 5.0 threshold
        MockGpsSession.return_value = session

        with patch.dict(os.environ, {"GPS_MAX_HDOP": "5.0", "GPS_MIN_QUALITY": "2d"}):
            gps = GPSModule()
            gps.connect()
            fix = gps.get_fix()

        self.assertIsNotNone(fix)
        self.assertEqual(fix["fix_quality"], 3)

    @patch("modules.gps.GpsSession")
    def test_get_fix_accepts_any_fix_when_quality_set_to_any(self, MockGpsSession):
        """get_fix() accepts a fix with high HDOP when GPS_MIN_QUALITY=any."""
        from modules.gps import GPSModule

        session = _make_session(mode=2)
        session.fix.hdop = 10.0  # would be rejected under 2d/3d quality modes
        MockGpsSession.return_value = session

        with patch.dict(os.environ, {"GPS_MIN_QUALITY": "any", "GPS_MAX_HDOP": "5.0"}):
            gps = GPSModule()
            gps.connect()
            fix = gps.get_fix()

        self.assertIsNotNone(fix)


if __name__ == "__main__":
    unittest.main()
