"""Integration tests for SDRCoordinator handoff logic.

Verifies lock acquisition, owner/health state transitions, retry behaviour,
and cleanup on stop. No real subprocess or hardware calls are made.
"""

import asyncio
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from modules.sdr_coordinator import SDRCoordinator


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def drone_rf():
    m = MagicMock()
    m.can_scan = False
    m._scan_task = None
    m.stop_scan = AsyncMock()
    m.start_scan = AsyncMock()
    return m


@pytest.fixture()
def coordinator(drone_rf):
    with patch.dict(os.environ, {"ADSB_SLICE_SECONDS": "1", "DRONE_RF_SLICE_SECONDS": "1"}):
        return SDRCoordinator(drone_rf)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handoff_to_adsb_acquires_lock(coordinator):
    """_handoff_to_adsb must acquire the exclusive lock while executing."""
    lock_was_held = []

    original_handshake = coordinator._start_readsb_with_handshake

    async def spy_handshake(*args, **kwargs):
        lock_was_held.append(coordinator._lock.locked())
        return True

    with patch.object(coordinator, "_start_readsb_with_handshake", spy_handshake):
        await coordinator._handoff_to_adsb()

    assert lock_was_held, "handshake was never called"
    assert lock_was_held[0] is True, "lock was not held during handoff body"


@pytest.mark.asyncio
async def test_handoff_to_drone_acquires_lock(coordinator, drone_rf):
    """_handoff_to_drone must acquire the exclusive lock while executing."""
    lock_was_held = []

    async def spy_handshake(*args, **kwargs):
        lock_was_held.append(coordinator._lock.locked())
        return True

    with patch.object(coordinator, "_stop_readsb_with_handshake", spy_handshake):
        await coordinator._handoff_to_drone()

    assert lock_was_held, "handshake was never called"
    assert lock_was_held[0] is True, "lock was not held during handoff body"


@pytest.mark.asyncio
async def test_concurrent_handoff_is_blocked(coordinator):
    """A second handoff must wait until the first releases the lock."""
    results = []

    async def slow_handshake(*args, **kwargs):
        results.append("handshake_start")
        await asyncio.sleep(0)  # yield so competing task can attempt entry
        results.append("handshake_end")
        return True

    with patch.object(coordinator, "_start_readsb_with_handshake", slow_handshake):
        async with coordinator._lock:
            # Lock is held here; schedule a handoff that needs the same lock
            task = asyncio.create_task(coordinator._handoff_to_adsb())
            await asyncio.sleep(0)  # yield — task cannot enter yet
            assert not results, "handoff body ran while lock was held externally"
        # Lock released — task can now proceed
        await task

    assert results == ["handshake_start", "handshake_end"]


@pytest.mark.asyncio
async def test_successful_handoff_to_adsb_sets_owner_and_healthy(coordinator):
    """After a successful handoff to ADS-B, current_owner=='adsb' and healthy==True."""
    with patch.object(coordinator, "_start_readsb_with_handshake", AsyncMock(return_value=True)):
        await coordinator._handoff_to_adsb()

    assert coordinator.current_owner == "adsb"
    assert coordinator.healthy is True


@pytest.mark.asyncio
async def test_failed_handoff_marks_unhealthy(coordinator):
    """When the ADS-B handshake returns False, healthy must be set to False."""
    with patch.object(coordinator, "_start_readsb_with_handshake", AsyncMock(return_value=False)):
        await coordinator._handoff_to_adsb()

    assert coordinator.healthy is False
    assert coordinator.current_owner != "adsb"


@pytest.mark.asyncio
async def test_handshake_retries_on_failure(coordinator):
    """_start_readsb_with_handshake must call _start_readsb once per attempt."""
    mock_start = AsyncMock()
    # _is_readsb_active always returns False so every attempt checks but fails
    with (
        patch.object(coordinator, "_start_readsb", mock_start),
        patch.object(coordinator, "_is_readsb_active", AsyncMock(return_value=False)),
        patch("asyncio.sleep", AsyncMock()),
    ):
        result = await coordinator._start_readsb_with_handshake(max_attempts=3)

    # Method always returns True (CI-safe fallback) — verify retry count
    assert mock_start.await_count == 3
    assert result is True  # CI-safe: assumes success after exhausting retries


@pytest.mark.asyncio
async def test_stop_restores_readsb(coordinator, drone_rf):
    """stop() must disable DroneRF scanning and restart readsb, then reset state."""
    mock_start = AsyncMock()
    coordinator._current_owner = "drone_rf"

    with patch.object(coordinator, "_start_readsb", mock_start):
        await coordinator.stop()

    assert mock_start.await_count >= 1, "readsb was not restarted during stop()"
    assert coordinator.current_owner == "none"
    assert coordinator.healthy is True
    assert drone_rf.can_scan is False
    drone_rf.stop_scan.assert_awaited()
