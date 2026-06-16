"""Unit tests for modules/drone_rf.py — SDR sampling is isolated in a child
process (#63); the subprocess and pyrtlsdr are mocked here."""

import asyncio
import queue
import time
import unittest
from unittest.mock import MagicMock, mock_open, patch

import pytest
import modules.drone_rf  # noqa: F401 — load before @patch resolves targets

try:
    import numpy  # noqa: F401
    HAS_NUMPY = True
except ImportError:
    HAS_NUMPY = False


def _run(coro):
    return asyncio.run(coro)


def _mod():
    from modules.drone_rf import DroneRFModule
    return DroneRFModule()


# ---------------------------------------------------------------------------
# is_hardware_present()
# ---------------------------------------------------------------------------

class TestDroneRFHardwareDetection(unittest.TestCase):

    @patch("modules.sdr_utils.subprocess.run")
    def test_hardware_present_with_known_usb_id(self, mock_run):
        mock_run.return_value = MagicMock(
            stdout="Bus 001 Device 003: ID 0bda:2832 Realtek Semiconductor Corp.\n", returncode=0)
        self.assertTrue(_mod().is_hardware_present())

    @patch("modules.sdr_utils.subprocess.run")
    def test_hardware_not_present_without_rtlsdr(self, mock_run):
        mock_run.return_value = MagicMock(
            stdout="Bus 001 Device 001: ID 1d6b:0002 Linux Foundation 2.0 root hub\n", returncode=0)
        self.assertFalse(_mod().is_hardware_present())

    @patch("modules.sdr_utils.subprocess.run")
    def test_hardware_present_0bda_2813(self, mock_run):
        mock_run.return_value = MagicMock(
            stdout="Bus 001 Device 004: ID 0bda:2813 Realtek RTL2813U\n", returncode=0)
        self.assertTrue(_mod().is_hardware_present())


# ---------------------------------------------------------------------------
# Lifecycle — start/stop manage the worker subprocess + monitor task
# ---------------------------------------------------------------------------

class TestDroneRFLifecycle(unittest.TestCase):

    @patch("modules.sdr_utils.subprocess.run",
           return_value=MagicMock(stdout="ID 1d6b:0002 Linux Foundation\n", returncode=0))
    def test_start_scan_graceful_when_no_hardware(self, _run_mock):
        m = _mod()
        _run(m.start_scan())  # must not raise
        self.assertIsNone(m._scan_task)

    @patch("modules.sdr_utils.subprocess.run",
           return_value=MagicMock(stdout="ID 0bda:2838 Realtek\n", returncode=0))
    def test_start_scan_skipped_when_crash_guard_disabled(self, _run_mock):
        from modules.drone_rf import DroneRFModule
        m = _mod()
        m.can_scan = False
        with patch.object(DroneRFModule, "_spawn_worker") as spawn:
            _run(m.start_scan())
            spawn.assert_not_called()
            self.assertIsNone(m._scan_task)

    @patch("modules.sdr_utils.subprocess.run",
           return_value=MagicMock(stdout="ID 0bda:2838 Realtek\n", returncode=0))
    def test_start_then_stop_manages_worker_and_task(self, _run_mock):
        from modules.drone_rf import DroneRFModule

        def fake_spawn(self_inner):
            self_inner._proc = MagicMock(pid=999)
            self_inner._proc.is_alive.return_value = True
            self_inner._stop_evt = MagicMock()
            self_inner._stop_evt.is_set.return_value = False
            self_inner._detections_q = queue.Queue()

        async def go():
            m = _mod()
            with patch.object(DroneRFModule, "_spawn_worker", fake_spawn):
                await m.start_scan()
                self.assertIsNotNone(m._scan_task)
                self.assertFalse(m._scan_task.done())
                await m.stop_scan()
                self.assertIsNone(m._scan_task)
        _run(go())

    def test_stop_scan_no_error_when_not_started(self):
        _run(_mod().stop_scan())  # must not raise


# ---------------------------------------------------------------------------
# Crash-loop guard + monitor policy (the heart of #63)
# ---------------------------------------------------------------------------

class TestDroneRFCrashGuard(unittest.TestCase):

    def test_crash_policy_disables_at_threshold(self):
        m = _mod()
        m._max_crashes = 3
        self.assertEqual(m._register_crash_and_decide(), "respawn")
        self.assertEqual(m._register_crash_and_decide(), "respawn")
        self.assertEqual(m._register_crash_and_decide(), "disable")

    def test_crash_policy_prunes_crashes_outside_window(self):
        m = _mod()
        m._max_crashes = 3
        m._crash_window_s = 100
        m._crash_times = [time.monotonic() - 250, time.monotonic() - 150]  # both stale
        self.assertEqual(m._register_crash_and_decide(), "respawn")
        self.assertEqual(len(m._crash_times), 1)  # stale ones pruned

    def test_monitor_tick_running_when_worker_alive(self):
        m = _mod()
        m._proc = MagicMock()
        m._proc.is_alive.return_value = True
        self.assertEqual(m._monitor_tick(), "running")

    def test_monitor_tick_stopped_when_intentional(self):
        m = _mod()
        m._proc = MagicMock()
        m._proc.is_alive.return_value = False
        m._stop_evt = MagicMock()
        m._stop_evt.is_set.return_value = True
        self.assertEqual(m._monitor_tick(), "stopped")

    def test_monitor_tick_respawns_on_unexpected_death(self):
        m = _mod()
        m._max_crashes = 5
        m._proc = MagicMock(exitcode=-11)        # SIGSEGV
        m._proc.is_alive.return_value = False
        m._stop_evt = MagicMock()
        m._stop_evt.is_set.return_value = False
        self.assertEqual(m._monitor_tick(), "respawn")
        self.assertEqual(len(m._crash_times), 1)
        self.assertTrue(m.can_scan)

    def test_monitor_tick_disables_after_repeated_crashes(self):
        m = _mod()
        m._max_crashes = 3
        m._proc = MagicMock(exitcode=-11)
        m._proc.is_alive.return_value = False
        m._stop_evt = MagicMock()
        m._stop_evt.is_set.return_value = False
        self.assertFalse(m.auto_disabled)  # not yet
        outcomes = [m._monitor_tick() for _ in range(3)]
        self.assertEqual(outcomes, ["respawn", "respawn", "disabled"])
        self.assertFalse(m.can_scan)  # crash loop broken — node stays up, scan off
        self.assertTrue(m.auto_disabled)  # the unambiguous "gave up" signal for the GUI


# ---------------------------------------------------------------------------
# Queue drain + GPS enrichment (parent side)
# ---------------------------------------------------------------------------

class TestDroneRFDrainQueue(unittest.TestCase):

    def test_drain_queue_enriches_with_gps_fix(self):
        m = _mod()
        m._gps = MagicMock()
        m._gps.get_fix.return_value = {"lat": 51.5, "lon": -0.1}
        m._detections_q = queue.Queue()
        m._detections_q.put({"freq_mhz": 433.0, "power_db": -10.0, "timestamp": "t"})
        m._drain_queue()
        self.assertEqual(len(m._detections), 1)
        self.assertEqual(m._detections[0]["gps_lat"], 51.5)
        self.assertEqual(m._detections[0]["gps_lon"], -0.1)

    def test_drain_queue_null_position_without_gps(self):
        m = _mod()
        m._gps = None
        m._detections_q = queue.Queue()
        m._detections_q.put({"freq_mhz": 868.0, "power_db": -5.0, "timestamp": "t"})
        m._drain_queue()
        self.assertIsNone(m._detections[0]["gps_lat"])
        self.assertIsNone(m._detections[0]["gps_lon"])

    def test_drain_queue_noop_when_empty(self):
        m = _mod()
        m._detections_q = queue.Queue()
        m._drain_queue()
        self.assertEqual(m._detections, [])


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------

class TestDroneRFHelpers(unittest.TestCase):

    @pytest.mark.skipif(not HAS_NUMPY, reason="numpy not available")
    def test_power_db_computes_decibels(self):
        import numpy as np
        from modules.drone_rf import _power_db
        val = _power_db(np.ones(1024, dtype=complex) * 0.5)  # |0.5|^2 = 0.25
        self.assertAlmostEqual(val, 10 * np.log10(0.25), places=1)

    def test_cpu_temp_parses_thermal_zone(self):
        from modules.drone_rf import _cpu_temp
        with patch("builtins.open", mock_open(read_data="65000\n")):
            self.assertAlmostEqual(_cpu_temp(), 65.0)

    def test_cpu_temp_none_when_unavailable(self):
        from modules.drone_rf import _cpu_temp
        with patch("builtins.open", side_effect=FileNotFoundError):
            self.assertIsNone(_cpu_temp())


# ---------------------------------------------------------------------------
# drain_detections() — REQUIRED test (Step 2 standardization)
# ---------------------------------------------------------------------------

class TestDroneRFDrainDetections(unittest.TestCase):

    def test_drain_returns_events_and_clears_buffer(self):
        m = _mod()
        fake_events = [{"freq_mhz": 433.0, "power_db": -15.0}]
        m._detections.extend(fake_events)
        result = m.drain_detections()
        self.assertEqual(result, fake_events)
        self.assertEqual(m._detections, [])

    def test_second_drain_returns_empty(self):
        m = _mod()
        m._detections.append({"freq_mhz": 868.0})
        m.drain_detections()
        self.assertEqual(m.drain_detections(), [])

    def test_returned_list_is_independent(self):
        m = _mod()
        m._detections.append({"freq_mhz": 915.0})
        result = m.drain_detections()
        result.append({"freq_mhz": 999.0})
        self.assertEqual(m._detections, [])


if __name__ == "__main__":
    unittest.main()
