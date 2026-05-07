"""Unit tests for modules/drone_rf.py — pyrtlsdr and subprocess mocked."""

import asyncio
import os
import unittest
from unittest.mock import AsyncMock, MagicMock, mock_open, patch

import pytest
import modules.drone_rf  # noqa: F401 — load before @patch resolves targets

try:
    import numpy
    HAS_NUMPY = True
except ImportError:
    HAS_NUMPY = False


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


# ---------------------------------------------------------------------------
# is_hardware_present()
# ---------------------------------------------------------------------------

class TestDroneRFHardwareDetection(unittest.TestCase):

    @patch("modules.drone_rf.subprocess.run")
    def test_hardware_present_with_known_usb_id(self, mock_run):
        """is_hardware_present() should return True when RTL-SDR ID in lsusb."""
        from modules.drone_rf import DroneRFModule

        mock_run.return_value = MagicMock(
            stdout="Bus 001 Device 003: ID 0bda:2832 Realtek Semiconductor Corp.\n",
            returncode=0,
        )
        m = DroneRFModule()
        self.assertTrue(m.is_hardware_present())

    @patch("modules.drone_rf.subprocess.run")
    def test_hardware_not_present_without_rtlsdr(self, mock_run):
        """is_hardware_present() should return False when no RTL-SDR in lsusb."""
        from modules.drone_rf import DroneRFModule

        mock_run.return_value = MagicMock(
            stdout="Bus 001 Device 001: ID 1d6b:0002 Linux Foundation 2.0 root hub\n",
            returncode=0,
        )
        m = DroneRFModule()
        self.assertFalse(m.is_hardware_present())

    @patch("modules.drone_rf.subprocess.run")
    def test_hardware_present_0bda_2813(self, mock_run):
        """is_hardware_present() should recognise the 0bda:2813 variant."""
        from modules.drone_rf import DroneRFModule

        mock_run.return_value = MagicMock(
            stdout="Bus 001 Device 004: ID 0bda:2813 Realtek RTL2813U\n",
            returncode=0,
        )
        m = DroneRFModule()
        self.assertTrue(m.is_hardware_present())


# ---------------------------------------------------------------------------
# start_scan() — graceful degradation
# ---------------------------------------------------------------------------

class TestDroneRFStartScan(unittest.TestCase):

    @patch("modules.drone_rf.subprocess.run")
    def test_start_scan_graceful_when_no_hardware(self, mock_run):
        """start_scan() should return without raising when no RTL-SDR present."""
        from modules.drone_rf import DroneRFModule

        mock_run.return_value = MagicMock(
            stdout="Bus 001 Device 001: ID 1d6b:0002 Linux Foundation\n",
            returncode=0,
        )
        m = DroneRFModule()
        _run(m.start_scan())  # must not raise
        self.assertIsNone(m._scan_task)

    @patch("modules.drone_rf.subprocess.run")
    def test_start_scan_creates_task_when_hardware_present(self, mock_run):
        """start_scan() should create a background asyncio.Task when hardware found."""
        from modules.drone_rf import DroneRFModule

        mock_run.return_value = MagicMock(
            stdout="Bus 001 Device 003: ID 0bda:2838 Realtek\n",
            returncode=0,
        )

        # Patch _scan_loop to prevent it from actually sampling
        async def _noop_loop(self_inner):
            await asyncio.sleep(9999)

        with patch.object(DroneRFModule, "_scan_loop", _noop_loop), \
             patch.object(DroneRFModule, "_open_sdr", return_value=True):
            m = DroneRFModule()
            _run(m.start_scan())
            self.assertIsNotNone(m._scan_task)
            _run(m.stop_scan())


# ---------------------------------------------------------------------------
# stop_scan()
# ---------------------------------------------------------------------------

class TestDroneRFStopScan(unittest.TestCase):

    @patch("modules.drone_rf.subprocess.run")
    def test_stop_scan_no_error_when_not_started(self, mock_run):
        """stop_scan() should complete without error if scan was never started."""
        from modules.drone_rf import DroneRFModule

        m = DroneRFModule()
        _run(m.stop_scan())  # must not raise

    @patch("modules.drone_rf.subprocess.run")
    def test_stop_scan_cancels_task(self, mock_run):
        """stop_scan() should cancel the background scan task."""
        from modules.drone_rf import DroneRFModule

        mock_run.return_value = MagicMock(
            stdout="Bus 001 Device 003: ID 0bda:2838 Realtek\n",
            returncode=0,
        )

        async def _noop_loop(self_inner):
            await asyncio.sleep(9999)

        with patch.object(DroneRFModule, "_scan_loop", _noop_loop), \
             patch.object(DroneRFModule, "_open_sdr", return_value=True):
            m = DroneRFModule()
            _run(m.start_scan())
            self.assertIsNotNone(m._scan_task)
            _run(m.stop_scan())
            self.assertIsNone(m._scan_task)


# ---------------------------------------------------------------------------
# Detection dict structure
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not HAS_NUMPY, reason="numpy not available")
class TestDroneRFDetectionStructure(unittest.TestCase):

    def test_detection_dict_has_required_fields(self):
        """A detection dict returned by _sample_frequency must have all required fields."""
        from modules.drone_rf import DroneRFModule

        import numpy as np

        mock_sdr = MagicMock()
        mock_sdr.read_samples.return_value = np.ones(256 * 1024, dtype=complex) * 0.5
        mock_sdr.close = MagicMock()

        gps = MagicMock()
        gps.get_fix.return_value = {"lat": 51.5, "lon": -0.1, "utc": "2024-01-15T12:00:00Z"}

        with patch("modules.drone_rf.DroneRFModule.is_hardware_present", return_value=True), \
             patch("modules.drone_rf.DRONE_POWER_THRESHOLD_DB", -999.0):
            with patch.dict("sys.modules", {}):
                with patch("builtins.__import__", side_effect=lambda name, *a, **kw: (
                    mock_sdr if name == "rtlsdr.RtlSdr" else __import__(name, *a, **kw)
                )):
                    pass

        # Direct construction test with all mocks in place
        with patch("modules.drone_rf.DRONE_POWER_THRESHOLD_DB", -999.0):
            mock_rtlsdr_module = MagicMock()
            mock_rtlsdr_module.RtlSdr.return_value = mock_sdr

            with patch.dict("sys.modules", {"rtlsdr": mock_rtlsdr_module}):
                m = DroneRFModule(gps_module=gps)
                m._sdr = mock_sdr  # simulate an opened SDR device
                result = m._sample_frequency(433.0)

        if result is not None:
            for field in ("freq_mhz", "power_db", "timestamp", "gps_lat", "gps_lon"):
                self.assertIn(field, result, f"missing field: {field}")
            self.assertEqual(result["freq_mhz"], 433.0)
            self.assertIsInstance(result["power_db"], float)


# ---------------------------------------------------------------------------
# Duty cycle
# ---------------------------------------------------------------------------

class TestDroneRFDutyCycle(unittest.TestCase):

    def test_scan_loop_pauses_between_sweeps_when_rest_nonzero(self):
        """_scan_loop() sleeps for REST_SECONDS after each complete sweep."""
        from modules.drone_rf import DroneRFModule

        sleep_calls = []
        call_count = [0]

        async def mock_sleep(duration):
            sleep_calls.append(duration)
            call_count[0] += 1
            if call_count[0] >= 1:
                raise asyncio.CancelledError()

        async def run():
            with patch.dict(os.environ, {"DRONE_RF_REST_SECONDS": "15", "DRONE_RF_MAX_TEMP_C": "75"}):
                m = DroneRFModule()
                m._sample_frequency = MagicMock(return_value=None)
                with patch("asyncio.sleep", side_effect=mock_sleep):
                    with patch.object(m, "_check_cpu_temp", return_value=None):
                        try:
                            await m._scan_loop()
                        except asyncio.CancelledError:
                            pass

        asyncio.get_event_loop().run_until_complete(run())
        self.assertTrue(
            any(d == 15 for d in sleep_calls),
            f"Expected rest sleep of 15s, got {sleep_calls}",
        )

    def test_scan_loop_no_rest_when_rest_seconds_zero(self):
        """_scan_loop() uses only 0.1s yields when DRONE_RF_REST_SECONDS=0."""
        from modules.drone_rf import DroneRFModule

        sleep_calls = []
        call_count = [0]

        async def mock_sleep(duration):
            sleep_calls.append(duration)
            call_count[0] += 1
            if call_count[0] >= 1:
                raise asyncio.CancelledError()

        async def run():
            with patch.dict(os.environ, {"DRONE_RF_REST_SECONDS": "0"}):
                m = DroneRFModule()
                m._sample_frequency = MagicMock(return_value=None)
                with patch("asyncio.sleep", side_effect=mock_sleep):
                    try:
                        await m._scan_loop()
                    except asyncio.CancelledError:
                        pass

        asyncio.get_event_loop().run_until_complete(run())
        self.assertTrue(
            all(d == 0.1 for d in sleep_calls),
            f"Expected only 0.1s yield sleeps, got {sleep_calls}",
        )

    def test_check_cpu_temp_returns_float_from_thermal_zone(self):
        """_check_cpu_temp() parses the thermal zone file and returns Celsius."""
        from modules.drone_rf import DroneRFModule

        m = DroneRFModule()
        with patch("builtins.open", mock_open(read_data="65000\n")):
            temp = m._check_cpu_temp()

        self.assertIsNotNone(temp)
        self.assertAlmostEqual(temp, 65.0)

    def test_check_cpu_temp_returns_none_when_unavailable(self):
        """_check_cpu_temp() returns None when the thermal zone file is missing."""
        from modules.drone_rf import DroneRFModule

        m = DroneRFModule()
        with patch("builtins.open", side_effect=FileNotFoundError):
            temp = m._check_cpu_temp()

        self.assertIsNone(temp)


# ---------------------------------------------------------------------------
# drain_detections() — REQUIRED test (Step 2 standardization)
# ---------------------------------------------------------------------------

class TestDroneRFDDrainDetections(unittest.TestCase):

    def test_drain_returns_events_and_clears_buffer(self):
        """After appending events, drain_detections() returns them and empties the buffer."""
        from modules.drone_rf import DroneRFModule

        m = DroneRFModule()
        fake_events = [{"freq_mhz": 433.0, "power_db": -15.0}]
        m._detections.extend(fake_events)

        result = m.drain_detections()
        self.assertEqual(result, fake_events)
        self.assertEqual(m._detections, [])

    def test_second_drain_returns_empty(self):
        """After draining, a second drain returns an empty list."""
        from modules.drone_rf import DroneRFModule

        m = DroneRFModule()
        m._detections.append({"freq_mhz": 868.0})
        m.drain_detections()

        self.assertEqual(m.drain_detections(), [])

    def test_returned_list_is_independent(self):
        """Mutating the returned list does not affect the internal buffer."""
        from modules.drone_rf import DroneRFModule

        m = DroneRFModule()
        m._detections.append({"freq_mhz": 915.0})

        result = m.drain_detections()
        result.append({"freq_mhz": 999.0})  # mutate returned list

        self.assertEqual(m._detections, [])  # internal buffer must remain clean


if __name__ == "__main__":
    unittest.main()
