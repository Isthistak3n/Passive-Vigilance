"""Unit tests for modules/sdr_manager.py — rtl_test and mode resolution."""

import subprocess
import unittest
from unittest.mock import patch, MagicMock

import pytest

from modules.sdr_manager import SDRMode, detect_sdr_count, resolve_sdr_mode


# ---------------------------------------------------------------------------
# detect_sdr_count()
# ---------------------------------------------------------------------------

class TestDetectSdrCount(unittest.TestCase):

    def _mock_run(self, stdout="", stderr="", returncode=0):
        mock = MagicMock()
        mock.stdout = stdout
        mock.stderr = stderr
        mock.returncode = returncode
        return mock

    @patch("modules.sdr_manager.subprocess.run")
    def test_two_devices_from_stdout(self, mock_run):
        mock_run.return_value = self._mock_run(
            stdout="Found 2 device(s):\n  0:  Generic RTL2832U\n  1:  Generic RTL2832U\n"
        )
        assert detect_sdr_count() == 2

    @patch("modules.sdr_manager.subprocess.run")
    def test_one_device_from_stderr(self, mock_run):
        mock_run.return_value = self._mock_run(
            stderr="Found 1 device(s):\n  0:  Generic RTL2832U\n"
        )
        assert detect_sdr_count() == 1

    @patch("modules.sdr_manager.subprocess.run")
    def test_zero_devices_no_supported(self, mock_run):
        mock_run.return_value = self._mock_run(
            stderr="No supported devices found.\n")
        assert detect_sdr_count() == 0

    @patch("modules.sdr_manager.subprocess.run")
    def test_zero_devices_empty_output(self, mock_run):
        mock_run.return_value = self._mock_run(stdout="", stderr="")
        assert detect_sdr_count() == 0

    @patch("modules.sdr_manager.subprocess.run")
    def test_rtl_test_not_installed(self, mock_run):
        mock_run.side_effect = FileNotFoundError("rtl_test not found")
        assert detect_sdr_count() == 0

    @patch("modules.sdr_manager.subprocess.run")
    def test_rtl_test_timeout(self, mock_run):
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="rtl_test", timeout=10)
        assert detect_sdr_count() == 0

    @patch("modules.sdr_manager.subprocess.run")
    def test_unexpected_exception(self, mock_run):
        mock_run.side_effect = OSError("permission denied")
        assert detect_sdr_count() == 0

    @patch("modules.sdr_manager.subprocess.run")
    def test_case_insensitive_found_line(self, mock_run):
        mock_run.return_value = self._mock_run(stdout="FOUND 3 DEVICE(S):\n")
        assert detect_sdr_count() == 3


# ---------------------------------------------------------------------------
# resolve_sdr_mode()
# ---------------------------------------------------------------------------

class TestResolveSdrMode(unittest.TestCase):

    def test_explicit_shared_ignores_count(self):
        assert resolve_sdr_mode("shared", 0) == SDRMode.SHARED
        assert resolve_sdr_mode("shared", 2) == SDRMode.SHARED

    def test_explicit_dedicated_ignores_count(self):
        assert resolve_sdr_mode("dedicated", 0) == SDRMode.DEDICATED
        assert resolve_sdr_mode("dedicated", 1) == SDRMode.DEDICATED

    def test_auto_zero_dongles_returns_shared(self):
        assert resolve_sdr_mode("auto", 0) == SDRMode.SHARED

    def test_auto_one_dongle_returns_shared(self):
        assert resolve_sdr_mode("auto", 1) == SDRMode.SHARED

    def test_auto_two_dongles_returns_dedicated(self):
        assert resolve_sdr_mode("auto", 2) == SDRMode.DEDICATED

    def test_auto_many_dongles_returns_dedicated(self):
        assert resolve_sdr_mode("auto", 5) == SDRMode.DEDICATED

    def test_uppercase_setting_normalised(self):
        assert resolve_sdr_mode("SHARED", 0) == SDRMode.SHARED
        assert resolve_sdr_mode("DEDICATED", 0) == SDRMode.DEDICATED
        assert resolve_sdr_mode("AUTO", 2) == SDRMode.DEDICATED

    def test_setting_with_whitespace(self):
        assert resolve_sdr_mode("  shared  ", 0) == SDRMode.SHARED

    def test_unknown_setting_falls_through_to_auto(self):
        # Any unrecognised value falls through to the AUTO branch
        assert resolve_sdr_mode("nonsense", 0) == SDRMode.SHARED
        assert resolve_sdr_mode("nonsense", 2) == SDRMode.DEDICATED


# ---------------------------------------------------------------------------
# SDRCoordinator integration tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_coordinator_handoff_to_adsb_sets_owner():
    from unittest.mock import AsyncMock, patch
    from modules.sdr_coordinator import SDRCoordinator

    drone_rf = MagicMock()
    drone_rf._scan_task = None
    drone_rf.stop_scan = AsyncMock()

    coordinator = SDRCoordinator(drone_rf)
    with patch.object(coordinator, "_start_readsb", new_callable=AsyncMock) as mock_start:
        await coordinator._handoff_to_adsb()

    assert coordinator.current_owner == "adsb"
    assert drone_rf.can_scan is False
    # Handshake now retries up to 5 times (P1 hardening) — test only cares that it was called
    mock_start.assert_awaited()


@pytest.mark.asyncio
async def test_coordinator_handoff_to_drone_sets_owner():
    from unittest.mock import AsyncMock
    from modules.sdr_coordinator import SDRCoordinator

    drone_rf = MagicMock()
    drone_rf.start_scan = AsyncMock()

    coordinator = SDRCoordinator(drone_rf)
    with patch.object(coordinator, "_stop_readsb", new_callable=AsyncMock) as mock_stop:
        await coordinator._handoff_to_drone()

    assert coordinator.current_owner == "drone_rf"
    assert drone_rf.can_scan is True
    mock_stop.assert_awaited_once()
    drone_rf.start_scan.assert_awaited_once()


@pytest.mark.asyncio
async def test_coordinator_start_calls_handoff_to_adsb():
    from unittest.mock import AsyncMock
    from modules.sdr_coordinator import SDRCoordinator

    drone_rf = MagicMock()
    drone_rf._scan_task = None
    drone_rf.stop_scan = AsyncMock()

    coordinator = SDRCoordinator(drone_rf)
    with patch.object(coordinator, "_start_readsb", new_callable=AsyncMock):
        await coordinator.start()

    assert coordinator.current_owner == "adsb"


@pytest.mark.asyncio
async def test_coordinator_stop_restores_readsb():
    from unittest.mock import AsyncMock
    from modules.sdr_coordinator import SDRCoordinator

    drone_rf = MagicMock()
    drone_rf.can_scan = True
    drone_rf.stop_scan = AsyncMock()

    coordinator = SDRCoordinator(drone_rf)
    coordinator._current_owner = "drone_rf"

    with patch.object(coordinator, "_start_readsb", new_callable=AsyncMock) as mock_start:
        await coordinator.stop()

    assert drone_rf.can_scan is False
    drone_rf.stop_scan.assert_awaited_once()
    mock_start.assert_awaited_once()
    assert coordinator.current_owner == "none"


@pytest.mark.asyncio
async def test_coordinator_loop_alternates_slices():
    """Loop calls both handoff methods before being cancelled."""
    import asyncio
    from unittest.mock import AsyncMock
    from modules.sdr_coordinator import SDRCoordinator

    drone_rf = MagicMock()
    drone_rf._scan_task = None
    drone_rf.stop_scan = AsyncMock()
    drone_rf.start_scan = AsyncMock()

    coordinator = SDRCoordinator(drone_rf)
    coordinator._adsb_slice = 0
    coordinator._drone_slice = 0

    mock_to_adsb = AsyncMock()
    mock_to_drone = AsyncMock()

    with patch.object(coordinator, "_handoff_to_adsb", mock_to_adsb), \
         patch.object(coordinator, "_handoff_to_drone", mock_to_drone):

        task = asyncio.create_task(coordinator._coordinator_loop())
        # Let the loop run through at least one full cycle
        for _ in range(10):
            await asyncio.sleep(0)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    assert mock_to_drone.await_count >= 1, "expected at least one handoff to DroneRF"
    assert mock_to_adsb.await_count >= 1, "expected at least one handoff to ADS-B"
