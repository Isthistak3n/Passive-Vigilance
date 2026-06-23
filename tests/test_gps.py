"""Unit tests for modules/gps.py — gpsd responses are fully mocked.

Reads happen on a background reader thread (see GPSModule._reader_loop), so the
read/timeout behaviour is exercised through the per-read unit `_read_once()`,
while get_fix() is tested as a pure, non-blocking sampler of the snapshot.
"""

import math
import os
import sys
import unittest
from unittest.mock import MagicMock, patch

# Ensure the module is loaded so @patch can resolve "modules.gps.*" targets.
import modules.gps  # noqa: F401


def _make_fix(mode: int, latitude=51.5, longitude=-0.1, altHAE=25.0,
              speed=0.5, track=270.0, hdop=0.8):
    """Return a mock gps fix object with the given attributes."""
    fix = MagicMock()
    fix.mode = mode
    fix.latitude = latitude
    fix.longitude = longitude
    fix.altHAE = altHAE
    fix.speed = speed
    fix.track = track
    fix.hdop = hdop
    return fix


def _make_session(mode: int, **fix_kwargs):
    """Return a mock GpsSession with a fix of the given mode."""
    session = MagicMock()
    session.fix = _make_fix(mode, **fix_kwargs)
    session.utc = "2024-01-15T12:00:00.000Z"
    session.read.return_value = None
    session.sock = MagicMock()
    return session


class TestGPSModuleConnect(unittest.TestCase):

    @patch("modules.gps.GpsSession")
    def test_connect_succeeds(self, MockGpsSession):
        """connect() should succeed when gpsd is reachable."""
        from modules.gps import GPSModule

        MockGpsSession.return_value = _make_session(mode=0)

        gps = GPSModule()
        gps.connect()
        self.addCleanup(gps.close)

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

    @patch("modules.gps.GpsSession")
    def test_close_stops_reader_thread(self, MockGpsSession):
        """close() must stop the background reader thread."""
        from modules.gps import GPSModule

        MockGpsSession.return_value = _make_session(mode=3)
        gps = GPSModule()
        gps.connect()
        self.assertTrue(gps._reader.is_alive())
        gps.close()
        self.assertIsNone(gps._session)
        # _reader is cleared and the thread has been joined.
        self.assertIsNone(gps._reader)


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
        self.addCleanup(gps.close)
        fix = gps.get_fix()

        self.assertIsNotNone(fix)
        for key in ("lat", "lon", "alt", "speed", "track", "utc", "fix_quality"):
            self.assertIn(key, fix)
        self.assertEqual(fix["fix_quality"], 3)
        self.assertEqual(fix["lat"], 51.5)
        self.assertEqual(fix["lon"], -0.1)
        self.assertEqual(fix["alt"], 25.0)
        self.assertEqual(fix["utc"], "2024-01-15T12:00:00.000Z")

    @patch("modules.gps.GpsSession")
    def test_get_fix_alt_is_nan_on_2d_fix(self, MockGpsSession):
        """get_fix() should return NaN for alt when the fix is 2-D only."""
        from modules.gps import GPSModule

        MockGpsSession.return_value = _make_session(mode=2)

        gps = GPSModule()
        gps.connect()
        self.addCleanup(gps.close)
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
        self.addCleanup(gps.close)
        fix = gps.get_fix()

        self.assertIsNone(fix)

    def test_get_fix_returns_none_before_connect(self):
        """get_fix() should return None gracefully if called before connect()."""
        from modules.gps import GPSModule

        gps = GPSModule()
        fix = gps.get_fix()

        self.assertIsNone(fix)

    @patch("modules.gps.GpsSession")
    def test_get_fix_does_no_socket_io(self, MockGpsSession):
        """get_fix() must sample the snapshot only — it must not call read()."""
        from modules.gps import GPSModule

        session = _make_session(mode=3)
        MockGpsSession.return_value = session

        gps = GPSModule()
        gps.connect()
        self.addCleanup(gps.close)
        gps._stop.set()                      # freeze the reader so the count is stable
        gps._reader.join(timeout=2.0)
        reads_before = session.read.call_count
        gps.get_fix()
        gps.get_fix()
        self.assertEqual(session.read.call_count, reads_before)  # no reads from get_fix


class TestReaderTick(unittest.TestCase):
    """The reader unit `_read_once()` carries the read + timeout behaviour that
    used to live inside get_fix()."""

    def _module_with_session(self, session):
        from modules.gps import GPSModule
        gps = GPSModule()
        gps._session = session            # bypass connect() so no thread starts
        return gps

    def test_read_once_snapshots_latest_fix(self):
        session = _make_session(mode=3, latitude=10.0, longitude=20.0)
        gps = self._module_with_session(session)
        gps._read_once()
        self.assertIsNotNone(gps.get_fix())
        self.assertEqual(gps.get_fix()["lat"], 10.0)

    def test_read_once_advances_to_latest_report(self):
        """Each tick must reflect the newest report, so the fix never lags."""
        session = _make_session(mode=3)
        seq = iter([(1.0, "t1"), (2.0, "t2"), (3.0, "t3")])

        def _read():
            lat, utc = next(seq)
            session.fix.latitude = lat
            session.utc = utc

        session.read.side_effect = _read
        gps = self._module_with_session(session)
        gps._read_once()
        gps._read_once()
        gps._read_once()
        fix = gps.get_fix()
        self.assertEqual(fix["lat"], 3.0)
        self.assertEqual(fix["utc"], "t3")

    def test_read_once_reapplies_settimeout_each_call(self):
        """settimeout() must be re-armed before each read, not only at connect."""
        session = _make_session(mode=3)
        gps = self._module_with_session(session)
        gps._read_once()
        gps._read_once()
        self.assertGreaterEqual(session.sock.settimeout.call_count, 2)

    def test_timeout_reapplied_after_socket_recreated(self):
        """A fresh blocking socket appearing between reads must be re-armed."""
        session = _make_session(mode=3)
        gps = self._module_with_session(session)
        second_sock = MagicMock()
        session.sock = second_sock          # client silently re-created the socket
        gps._read_once()
        self.assertGreaterEqual(second_sock.settimeout.call_count, 1)

    def test_read_timeout_leaves_snapshot_untouched(self):
        """A read() that raises socket.timeout is 'no new data', not a crash."""
        import socket
        session = _make_session(mode=3)
        session.read.side_effect = socket.timeout("timed out")
        gps = self._module_with_session(session)
        gps._read_once()                    # raises internally, swallowed
        self.assertIsNone(gps.get_fix())    # no snapshot was taken

    def test_read_once_does_not_hang_on_blocking_read(self):
        """A read() that would block forever is bounded by the socket timeout.

        The fake socket's settimeout 'arms' a flag that makes the blocking read
        raise instead of sleeping, mirroring how a real socket timeout converts a
        forever-blocking recv() into socket.timeout.
        """
        import socket
        session = _make_session(mode=3)
        sock = MagicMock()
        armed = {"timeout": False}

        def _settimeout(_value):
            armed["timeout"] = True

        sock.settimeout.side_effect = _settimeout

        def _read():
            if armed["timeout"]:
                raise socket.timeout("timed out")
            raise AssertionError("read() would have blocked forever (timeout not armed)")

        session.sock = sock
        session.read.side_effect = _read
        gps = self._module_with_session(session)
        gps._read_once()
        self.assertIsNone(gps.get_fix())

    def test_read_timeout_is_configurable(self):
        """GPS_READ_TIMEOUT_SECONDS controls the value passed to settimeout()."""
        with patch.dict(os.environ, {"GPS_READ_TIMEOUT_SECONDS": "4.5"}):
            session = _make_session(mode=3)
            gps = self._module_with_session(session)
            gps._apply_read_timeout()
        session.sock.settimeout.assert_called_with(4.5)


class TestReaderThread(unittest.TestCase):
    """The whole point of the reader thread: the snapshot stays current under a
    flood of reports, with no help from get_fix() — which is what was broken
    (consuming one report per poll let the fix lag further behind every cycle)."""

    @patch("modules.gps.GpsSession")
    def test_reader_thread_keeps_snapshot_current(self, MockGpsSession):
        import time

        session = _make_session(mode=3)
        counter = {"n": 0}

        def _read():
            counter["n"] += 1
            session.fix.latitude = float(counter["n"])  # each report advances

        session.read.side_effect = _read
        MockGpsSession.return_value = session

        from modules.gps import GPSModule
        gps = GPSModule()
        gps.connect()
        self.addCleanup(gps.close)

        first = gps.get_fix()["lat"]
        time.sleep(0.2)                       # let the thread consume more reports
        second = gps.get_fix()["lat"]
        self.assertGreater(second, first)     # advanced with no get_fix-driven read


class TestGPSModuleIsFixed(unittest.TestCase):

    def _gps_with_mode(self, MockGpsSession, mode: int):
        from modules.gps import GPSModule

        MockGpsSession.return_value = _make_session(mode=mode)
        gps = GPSModule()
        gps.connect()
        self.addCleanup(gps.close)
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

        session = _make_session(mode=3, hdop=10.0)  # well above the 5.0 threshold
        MockGpsSession.return_value = session

        with patch.dict(os.environ, {"GPS_MAX_HDOP": "5.0", "GPS_MIN_QUALITY": "2d"}):
            gps = GPSModule()
            gps.connect()
            self.addCleanup(gps.close)
            fix = gps.get_fix()

        self.assertIsNone(fix)

    @patch("modules.gps.GpsSession")
    def test_get_fix_accepts_low_hdop(self, MockGpsSession):
        """get_fix() returns a fix when HDOP is within GPS_MAX_HDOP."""
        from modules.gps import GPSModule

        session = _make_session(mode=3, hdop=1.5)  # well below the 5.0 threshold
        MockGpsSession.return_value = session

        with patch.dict(os.environ, {"GPS_MAX_HDOP": "5.0", "GPS_MIN_QUALITY": "2d"}):
            gps = GPSModule()
            gps.connect()
            self.addCleanup(gps.close)
            fix = gps.get_fix()

        self.assertIsNotNone(fix)
        self.assertEqual(fix["fix_quality"], 3)

    @patch("modules.gps.GpsSession")
    def test_get_fix_accepts_any_fix_when_quality_set_to_any(self, MockGpsSession):
        """get_fix() accepts a fix with high HDOP when GPS_MIN_QUALITY=any."""
        from modules.gps import GPSModule

        session = _make_session(mode=2, hdop=10.0)  # rejected under 2d/3d modes
        MockGpsSession.return_value = session

        with patch.dict(os.environ, {"GPS_MIN_QUALITY": "any", "GPS_MAX_HDOP": "5.0"}):
            gps = GPSModule()
            gps.connect()
            self.addCleanup(gps.close)
            fix = gps.get_fix()

        self.assertIsNotNone(fix)


if __name__ == "__main__":
    unittest.main()
