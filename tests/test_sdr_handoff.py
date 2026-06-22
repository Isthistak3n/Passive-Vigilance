"""Integration tests for SDRCoordinator handoff logic (generic N-band cycle).

Verifies lock acquisition, owner/health state transitions, retry behaviour,
the settle barrier, the unavailable-band skip, cycle parsing, and the
request_band_window preemption. No real subprocess or hardware calls are made.
"""

import asyncio
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from modules.sdr_coordinator import SDRCoordinator, _DecoderServiceOwner


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


@pytest.fixture()
def drone_rf():
    m = MagicMock()
    m.can_scan = False
    m.auto_disabled = False
    m._scan_task = None
    m.stop_scan = AsyncMock()
    m.start_scan = AsyncMock()
    return m


@pytest.fixture()
def coordinator(drone_rf):
    with patch.dict(os.environ, {
        "ADSB_SLICE_SECONDS": "1",
        "DRONE_RF_SLICE_SECONDS": "1",
        "SDR_HANDOFF_SETTLE_SECONDS": "0",  # keep tests fast/deterministic
    }):
        return SDRCoordinator(drone_rf)


def _fake_owner(name, available=True):
    o = MagicMock()
    o.name = name
    o.is_available = available
    o.acquire = AsyncMock(return_value=True)
    o.release = AsyncMock()
    return o


# ---------------------------------------------------------------------------
# Lock / owner / health
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handoff_acquires_lock(coordinator):
    """_handoff_to must hold the exclusive lock while executing the body."""
    lock_was_held = []

    async def spy_handshake(*args, **kwargs):
        lock_was_held.append(coordinator._lock.locked())
        return True

    with patch.object(coordinator, "_start_readsb_with_handshake", spy_handshake):
        await coordinator._handoff_to("adsb")

    assert lock_was_held and lock_was_held[0] is True


@pytest.mark.asyncio
async def test_concurrent_handoff_is_blocked(coordinator):
    """A second handoff must wait until the first releases the lock."""
    results = []

    async def slow_handshake(*args, **kwargs):
        results.append("handshake_start")
        await asyncio.sleep(0)
        results.append("handshake_end")
        return True

    with patch.object(coordinator, "_start_readsb_with_handshake", slow_handshake):
        async with coordinator._lock:
            task = asyncio.create_task(coordinator._handoff_to("adsb"))
            await asyncio.sleep(0)
            assert not results, "handoff body ran while lock was held externally"
        await task

    assert results == ["handshake_start", "handshake_end"]


@pytest.mark.asyncio
async def test_successful_handoff_sets_owner_and_healthy(coordinator):
    with patch.object(coordinator, "_start_readsb_with_handshake", AsyncMock(return_value=True)):
        await coordinator._handoff_to("adsb")
    assert coordinator.current_owner == "adsb"
    assert coordinator.healthy is True


@pytest.mark.asyncio
async def test_failed_handoff_marks_unhealthy(coordinator):
    with patch.object(coordinator, "_start_readsb_with_handshake", AsyncMock(return_value=False)):
        await coordinator._handoff_to("adsb")
    assert coordinator.healthy is False
    assert coordinator.current_owner != "adsb"


@pytest.mark.asyncio
async def test_handshake_retries_on_failure(coordinator):
    """_start_readsb_with_handshake must call _start_readsb once per attempt."""
    mock_start = AsyncMock()
    with (
        patch.object(coordinator, "_start_readsb", mock_start),
        patch.object(coordinator, "_is_readsb_active", AsyncMock(return_value=False)),
        patch("asyncio.sleep", AsyncMock()),
    ):
        result = await coordinator._start_readsb_with_handshake(max_attempts=3)
    assert mock_start.await_count == 3
    assert result is True  # CI-safe fallback


@pytest.mark.asyncio
async def test_stop_restores_readsb(coordinator, drone_rf):
    """stop() disables DroneRF scanning, restarts readsb, and resets state."""
    coordinator._current_owner = "drone_rf"
    with patch.object(coordinator, "_start_readsb", AsyncMock()) as mock_start, \
         patch.object(coordinator, "_stop_readsb_with_handshake", AsyncMock(return_value=True)):
        await coordinator.stop()
    assert mock_start.await_count >= 1
    assert coordinator.current_owner == "none"
    assert coordinator.healthy is True
    assert drone_rf.can_scan is False
    drone_rf.stop_scan.assert_awaited()


# ---------------------------------------------------------------------------
# Settle barrier + unavailable-band skip
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_settle_barrier_runs_before_each_handoff(coordinator):
    """Each handoff settles the dongle before opening it for the next owner."""
    with (
        patch.object(coordinator, "_settle_sdr", AsyncMock()) as settle,
        patch.object(coordinator, "_start_readsb_with_handshake", AsyncMock(return_value=True)),
        patch.object(coordinator, "_stop_readsb_with_handshake", AsyncMock(return_value=True)),
    ):
        await coordinator._handoff_to("adsb")       # none -> adsb
        await coordinator._handoff_to("drone_rf")   # adsb -> drone (releases readsb)
    assert settle.await_count == 2


@pytest.mark.asyncio
async def test_unavailable_band_keeps_readsb(coordinator, drone_rf):
    """An auto-disabled band's handoff is refused; the dongle stays with readsb."""
    coordinator._current_owner = "adsb"
    drone_rf.auto_disabled = True
    with patch.object(coordinator, "_stop_readsb_with_handshake", AsyncMock(return_value=True)) as stop:
        await coordinator._handoff_to("drone_rf")
    stop.assert_not_awaited()             # readsb never released
    drone_rf.start_scan.assert_not_awaited()
    assert coordinator.current_owner == "adsb"


@pytest.mark.asyncio
async def test_settle_skips_usbreset_when_disabled(coordinator):
    coordinator._handoff_settle = 0
    coordinator._usb_reset = False
    with patch.object(coordinator, "_reset_sdr", AsyncMock()) as reset:
        await coordinator._settle_sdr()
    reset.assert_not_awaited()


@pytest.mark.asyncio
async def test_settle_invokes_usbreset_when_enabled(coordinator):
    coordinator._handoff_settle = 0
    coordinator._usb_reset = True
    with patch.object(coordinator, "_reset_sdr", AsyncMock()) as reset:
        await coordinator._settle_sdr()
    reset.assert_awaited_once()


# ---------------------------------------------------------------------------
# N-slice cycle parsing + decoder owners + preemption window
# ---------------------------------------------------------------------------


def test_parse_slices_from_env():
    with patch.dict(os.environ, {"SDR_CYCLE_SLICES": "adsb:840,ais:60", "SDR_HANDOFF_SETTLE_SECONDS": "0"}):
        c = SDRCoordinator()
    assert c.slices == [("adsb", 840), ("ais", 60)]


def test_parse_slices_ignores_malformed_and_defaults():
    with patch.dict(os.environ, {"SDR_CYCLE_SLICES": "garbage,,adsb:30", "ADSB_SLICE_SECONDS": "30"}):
        c = SDRCoordinator()
    assert c.slices == [("adsb", 30)]
    with patch.dict(os.environ, {"ADSB_SLICE_SECONDS": "45"}, clear=False):
        os.environ.pop("SDR_CYCLE_SLICES", None)
        c2 = SDRCoordinator()
    assert c2.slices == [("adsb", 45)]


@pytest.mark.asyncio
async def test_explicit_slices_and_owner_registration():
    c = SDRCoordinator(cycle_slices=[("adsb", 800), ("ais", 60)])
    ais = _fake_owner("ais")
    c.register_owner(ais)
    c._handoff_settle = 0
    with patch.object(c, "_start_readsb_with_handshake", AsyncMock(return_value=True)), \
         patch.object(c, "_stop_readsb_with_handshake", AsyncMock(return_value=True)):
        await c._handoff_to("adsb")
        await c._handoff_to("ais")
    ais.acquire.assert_awaited_once()
    assert c.current_owner == "ais"


@pytest.mark.asyncio
async def test_decoder_owner_uses_service_handshake():
    c = SDRCoordinator()
    mod = MagicMock(auto_disabled=False)
    owner = _DecoderServiceOwner("ais", "ais-catcher", c, module=mod)
    c.register_owner(owner)
    c._handoff_settle = 0
    with patch.object(c, "_start_service_with_handshake", AsyncMock(return_value=True)) as start:
        await c._handoff_to("ais")
    start.assert_awaited_once_with("ais-catcher")
    assert mod.can_scan is True
    assert c.current_owner == "ais"


@pytest.mark.asyncio
async def test_request_band_window_validates():
    c = SDRCoordinator()
    assert c.request_band_window("acars", 20) is False        # unknown band
    c.register_owner(_fake_owner("acars"))
    assert c.request_band_window("acars", 20) is True         # queued
    assert c.request_band_window("acars", 20) is False        # already pending


@pytest.mark.asyncio
async def test_service_window_preempts_then_restores():
    c = SDRCoordinator(cycle_slices=[("adsb", 800)])
    acars = _fake_owner("acars")
    c.register_owner(acars)
    c._current_owner = "adsb"
    c._handoff_settle = 0
    with (
        patch.object(c, "_start_readsb_with_handshake", AsyncMock(return_value=True)),
        patch.object(c, "_stop_readsb_with_handshake", AsyncMock(return_value=True)),
        patch("asyncio.sleep", AsyncMock()),
    ):
        await c._service_window("acars", 20)
    acars.acquire.assert_awaited_once()
    acars.release.assert_awaited_once()
    assert c.current_owner == "adsb"   # handed back


@pytest.mark.asyncio
async def test_run_slice_services_pending_window():
    c = SDRCoordinator(cycle_slices=[("adsb", 800)])
    c.register_owner(_fake_owner("acars"))
    c._pending_window = ("acars", 20)
    with patch.object(c, "_service_window", AsyncMock()) as sw, \
         patch("asyncio.sleep", AsyncMock()):
        await c._run_slice(1)
    sw.assert_awaited_once_with("acars", 20)
    assert c._pending_window is None
