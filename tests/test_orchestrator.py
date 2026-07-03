"""Tests for the PassiveVigilance asyncio orchestrator (main.py).

All module connections are mocked — no real hardware, network, or filesystem
access required.
"""

import asyncio
import json
import os
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from modules.persistence import DetectionEvent


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_detection_event(**overrides) -> DetectionEvent:
    defaults = dict(
        mac="aa:bb:cc:dd:ee:ff",
        score=0.85,
        score_breakdown={"temporal": 0.35, "location": 0.35, "frequency": 0.10, "signal": 0.05},
        first_seen=datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc),
        last_seen=datetime(2026, 1, 1, 12, 20, 0, tzinfo=timezone.utc),
        locations=[{"lat": 51.5074, "lon": -0.1278, "count": 5}],
        observation_count=10,
        manufacturer="Apple",
        device_type="phone",
        alert_level="likely",
    )
    defaults.update(overrides)
    return DetectionEvent(**defaults)


def _make_aircraft(**overrides) -> dict:
    a = {
        "icao": "ABC123",
        "callsign": "BAW123",
        "lat": 51.5, "lon": -0.1,
        "altitude": 35000,
        "emergency": False,
    }
    a.update(overrides)
    return a


def _make_drone(**overrides) -> dict:
    d = {
        "freq_mhz": 2400.0,
        "power_db": -30.0,
        "gps_lat": 51.5, "gps_lon": -0.1,
        "timestamp": "2026-01-01T12:00:00+00:00",
    }
    d.update(overrides)
    return d


def _make_remote_id(**overrides) -> dict:
    r = {
        "uas_id": "UAS-1",
        "ua_type": "Multirotor",
        "status": "Airborne",
        "operator_id": "OP-1",
        "operator_lat": 51.4, "operator_lon": -0.2,
        "drone_lat": 51.5, "drone_lon": -0.1, "drone_alt_m": 100.0,
        "source_phy": "IEEE802.11", "source_mac": "fa:0b:bc:11:22:33",
        "rssi": -50,
        "timestamp": "2026-01-01T12:00:00+00:00",
    }
    r.update(overrides)
    return r


async def _drain_alerts(orch) -> None:
    """Block until fire-and-forget alert sends have actually run.

    ``_dispatch_alert`` offloads sends to a single-worker executor and does not
    await them, so a backend assertion right after a poll would race the worker
    thread. Submitting a no-op after the sends and awaiting it guarantees (FIFO,
    one worker) that the queued sends completed and the mocked backend was called.
    """
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(orch.sensor_orchestrator._alert_executor, lambda: None)


@pytest.fixture()
def orch(tmp_path):
    """PassiveVigilance instance with all modules mocked and output in tmp_path."""
    env_patch = {
        "SESSION_OUTPUT_DIR": str(tmp_path),
        # Speed up GPS startup wait loop — no real device available in tests
        "GPS_STARTUP_TIMEOUT_SECONDS": "0",
        # NODE_MODE is now required (fail-loud); mobile keeps PersistenceEngine
        # as the injected engine, which these tests patch via main.PersistenceEngine.
        "NODE_MODE": "mobile",
    }

    with (
        patch("main.GPSModule") as mock_gps_cls,
        patch("main.IgnoreList") as mock_il_cls,
        patch("main.KismetModule") as mock_kis_cls,
        patch("main.ADSBModule") as mock_adsb_cls,
        patch("main.DroneRFModule") as mock_drone_cls,
        patch("main.PersistenceEngine") as mock_pe_cls,
        patch("main.ProbeAnalyzer") as mock_pa_cls,
        patch("main.EntityStore") as mock_es_cls,
        patch("main.AlertFactory") as mock_af,
        patch("main.ShapefileWriter") as mock_shp_cls,
        patch("main.WiGLEUploader") as mock_wigle_cls,
        patch("main._SESSION_OUTPUT_DIR", str(tmp_path)),
        patch("main._RATE_LIMIT_PERSIST", None),
        patch.dict(os.environ, env_patch),
    ):
        # GPS
        mock_gps = MagicMock()
        mock_gps.connect.return_value = None
        mock_gps.close.return_value = None
        mock_gps.get_fix.return_value = None
        mock_gps_cls.return_value = mock_gps

        # IgnoreList
        mock_il_cls.return_value = MagicMock()

        # Kismet
        mock_kis = MagicMock()
        mock_kis.connect = AsyncMock()
        mock_kis.close = AsyncMock()
        mock_kis.poll_devices = AsyncMock(return_value=[])
        mock_kis.get_wigle_csv_path.return_value = None
        mock_kis_cls.return_value = mock_kis

        # ADSB
        mock_adsb = MagicMock()
        mock_adsb.connect = AsyncMock()
        mock_adsb.close = AsyncMock()
        mock_adsb.poll_aircraft = AsyncMock(return_value=[])
        mock_adsb_cls.return_value = mock_adsb

        # DroneRF
        mock_drone = MagicMock()
        mock_drone.start_scan = AsyncMock()
        mock_drone.stop_scan = AsyncMock()
        mock_drone._scan_task = None
        mock_drone._detections = []
        mock_drone_cls.return_value = mock_drone

        # Persistence + ProbeAnalyzer
        mock_pe = MagicMock()
        mock_pe.update.return_value = []
        mock_pe_cls.return_value = mock_pe

        mock_pa = MagicMock()
        mock_pa.analyze.return_value = []
        mock_pa_cls.return_value = mock_pa

        # Alert backend
        mock_backend = MagicMock()
        mock_backend.send_aircraft_alert.return_value = True
        mock_backend.send_persistence_alert.return_value = True
        mock_backend.send_drone_alert.return_value = True
        mock_af.get_backend.return_value = mock_backend

        # ShapefileWriter
        mock_shp = MagicMock()
        mock_shp.write_session.return_value = str(tmp_path / "dummy.shp")
        mock_shp.write_geojson.return_value = str(tmp_path / "dummy.geojson")
        mock_shp_cls.return_value = mock_shp

        # WiGLE
        mock_wigle = MagicMock()
        mock_wigle.is_configured.return_value = False
        mock_wigle.find_latest_csv.return_value = None
        mock_wigle_cls.return_value = mock_wigle

        from main import PassiveVigilance
        o = PassiveVigilance()
        # Expose the mocks for assertions
        o._mock_backend = mock_backend
        o._mock_shp = mock_shp
        o._mock_wigle = mock_wigle
        yield o


# ---------------------------------------------------------------------------
# Initialisation
# ---------------------------------------------------------------------------


def test_orchestrator_initialises_without_error(orch):
    assert orch.session_id is not None
    assert len(orch.session_id) == 15  # YYYYMMDD_HHMMSS
    assert orch.sensor_orchestrator.all_events == []
    assert orch.sensor_orchestrator.aircraft_detections == []
    assert orch.sensor_orchestrator.drone_detections == []


# ---------------------------------------------------------------------------
# startup()
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@patch("main.detect_sdr_count", return_value=2)
async def test_startup_all_modules_available(mock_sdr, orch):
    await orch.startup()
    assert orch._gps_active is True
    assert orch._kismet_active is True
    assert orch._adsb_active is True


@pytest.mark.asyncio
@patch("main.detect_sdr_count", return_value=2)
async def test_startup_graceful_when_gps_unavailable(mock_sdr, orch):
    orch.gps.connect.side_effect = ConnectionError("gpsd not running")
    await orch.startup()
    assert orch._gps_active is False
    # Other modules should still connect
    assert orch._kismet_active is True
    assert orch._adsb_active is True


@pytest.mark.asyncio
@patch("main.detect_sdr_count", return_value=2)
async def test_startup_graceful_when_kismet_unavailable(mock_sdr, orch):
    orch.kismet.connect.side_effect = ConnectionError("Kismet not running")
    # Single attempt keeps the test fast; the retry behaviour is covered separately.
    with patch.dict(os.environ, {"KISMET_CONNECT_RETRIES": "1"}):
        await orch.startup()
    assert orch._kismet_active is False
    assert orch._gps_active is True
    assert orch._adsb_active is True


@pytest.mark.asyncio
@patch("main.detect_sdr_count", return_value=0)
async def test_startup_graceful_when_readsb_unavailable(_sdr, orch):
    # readsb-only path (DroneRF off): a failed connect leaves ADS-B inactive, and
    # startup still completes gracefully. Hermetic via DRONE off + sdr_count=0.
    orch.adsb.connect.side_effect = ConnectionError("readsb not running")
    with patch.dict(os.environ, {"DRONE_RF_ENABLED": "false"}):
        await orch.startup()
    assert orch._adsb_active is False
    assert orch._gps_active is True
    assert orch._kismet_active is True


@pytest.mark.asyncio
@patch("main.detect_sdr_count", return_value=1)
async def test_startup_shared_keeps_adsb_active_despite_startup_connect_failure(_sdr, orch):
    # In SHARED mode ADS-B is enabled (the coordinator brings readsb up during its
    # slices), so a racy startup connect() failure must NOT grey the chiclet — the
    # active flag stays True and sensor_health carries liveness (the P6-adjacent fix).
    from unittest.mock import AsyncMock
    orch.adsb.connect.side_effect = ConnectionError("readsb stopped this slice")
    orch.sdr_coordinator.start = AsyncMock()
    with patch.dict(os.environ, {"DRONE_RF_ENABLED": "true", "SDR_MODE": "shared"}):
        await orch.startup()
    assert orch._adsb_active is True


# ---------------------------------------------------------------------------
# Kismet startup connect retry (boot-readiness race)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@patch("main.asyncio.sleep", new_callable=AsyncMock)
async def test_kismet_connect_retries_then_succeeds(mock_sleep, orch):
    # Kismet's API is not ready for the first two attempts, then accepts the
    # connection — WiFi must come up, not stay greyed for the whole session.
    orch._kismet_active = False
    orch.kismet.connect.side_effect = [
        ConnectionError("Cannot reach Kismet — is it running?"),
        ConnectionError("Cannot reach Kismet — is it running?"),
        None,
    ]
    await orch._connect_kismet_with_retry()
    assert orch._kismet_active is True
    assert orch.kismet.connect.await_count == 3


@pytest.mark.asyncio
@patch("main.asyncio.sleep", new_callable=AsyncMock)
async def test_kismet_connect_stops_early_on_bad_key(mock_sleep, orch):
    # A 401 is a genuinely bad API key — retrying can't fix it, so give up at once
    # instead of stalling startup for the full retry window.
    orch._kismet_active = False
    orch.kismet.connect.side_effect = ConnectionError(
        "Kismet rejected the API key (HTTP 401) — check KISMET_API_KEY"
    )
    with patch.dict(os.environ, {"KISMET_CONNECT_RETRIES": "15"}):
        await orch._connect_kismet_with_retry()
    assert orch._kismet_active is False
    assert orch.kismet.connect.await_count == 1
    mock_sleep.assert_not_awaited()


@pytest.mark.asyncio
@patch("main.asyncio.sleep", new_callable=AsyncMock)
async def test_kismet_connect_gives_up_after_retries(mock_sleep, orch):
    # Kismet never comes up: exhaust the bounded retries and disable WiFi rather
    # than retry forever.
    orch._kismet_active = False
    orch.kismet.connect.side_effect = ConnectionError("Cannot reach Kismet")
    with patch.dict(os.environ, {"KISMET_CONNECT_RETRIES": "3"}):
        await orch._connect_kismet_with_retry()
    assert orch._kismet_active is False
    assert orch.kismet.connect.await_count == 3


# ---------------------------------------------------------------------------
# _poll_adsb()
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_poll_adsb_emergency_bypasses_rate_limiter(orch):
    """Emergency aircraft must trigger an alert regardless of rate limiter state."""
    emergency_ac = _make_aircraft(emergency=True, icao="EMG001")
    orch.adsb.poll_aircraft = AsyncMock(return_value=[emergency_ac])
    orch._adsb_active = True

    # Exhaust the rate limiter key first
    await orch.rate_limiter.is_allowed("aircraft:EMG001")  # consumes the slot

    await orch.sensor_orchestrator._poll_adsb()
    await _drain_alerts(orch)
    # Backend must still be called
    orch._mock_backend.send_aircraft_alert.assert_called_once()


@pytest.mark.asyncio
async def test_poll_adsb_rate_limiter_suppresses_repeat_normal_alert(orch):
    """An of-interest (non-emergency) aircraft is rate-limited after the first alert.

    (P7: routine transit no longer alerts at all, so the rate-limiter case is now
    exercised against an aircraft the scorer flags of-interest.)"""
    from modules.air_scoring import AirScore
    normal_ac = _make_aircraft(emergency=False, icao="NRM001")
    orch.adsb.poll_aircraft = AsyncMock(return_value=[normal_ac])
    orch._adsb_active = True

    with patch.object(orch.sensor_orchestrator, "_score_aircraft",
                      return_value=AirScore(score=0.8, severity="likely", of_interest=True)):
        await orch.sensor_orchestrator._poll_adsb()   # first poll — alert fires
        await orch.sensor_orchestrator._poll_adsb()   # second poll — rate-limited
    await _drain_alerts(orch)

    orch._mock_backend.send_aircraft_alert.assert_called_once()


# ---------------------------------------------------------------------------
# _poll_kismet()
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_poll_kismet_passes_devices_through_ignore_list(orch):
    """poll_kismet must call kismet.poll_devices() which applies the ignore list."""
    orch._kismet_active = True
    orch.kismet.poll_devices = AsyncMock(return_value=[])

    await orch.sensor_orchestrator._poll_kismet()

    orch.kismet.poll_devices.assert_called_once()


@pytest.mark.asyncio
async def test_poll_kismet_sends_alert_for_high_score_event(orch):
    """A DetectionEvent above threshold triggers a persistence alert."""
    event = _make_detection_event(alert_level="high", score=0.95)
    orch.persistence.update.return_value = [event]
    orch.kismet.poll_devices = AsyncMock(return_value=[{"macaddr": "aa:bb:cc:dd:ee:ff"}])
    orch._kismet_active = True

    await orch.sensor_orchestrator._poll_kismet()
    await _drain_alerts(orch)

    orch._mock_backend.send_persistence_alert.assert_called_once_with(event)
    assert len(orch.sensor_orchestrator.all_events) == 1
    assert orch.sensor_orchestrator.all_events[0]["mac"] == "aa:bb:cc:dd:ee:ff"


@pytest.mark.asyncio
async def test_poll_kismet_suspicious_is_display_only(orch):
    """A suspicious (0.5) detection shows in the WiFi panel but does NOT page —
    the 2026-06 post-freeze noise cut (page likely+ only)."""
    so = orch.sensor_orchestrator
    event = _make_detection_event(alert_level="suspicious", score=0.5)
    orch.persistence.update.return_value = [event]
    orch.kismet.poll_devices = AsyncMock(return_value=[{"macaddr": "aa:bb:cc:dd:ee:ff"}])
    orch._kismet_active = True

    await so._poll_kismet()
    await _drain_alerts(orch)

    orch._mock_backend.send_persistence_alert.assert_not_called()   # not paged
    assert so._stats["alerts_below_threshold"] >= 1
    assert len(so.all_events) == 1                                  # still displayed


@pytest.mark.asyncio
async def test_poll_kismet_force_page_pages_below_threshold(orch):
    """An egregious-during-baseline event scores 0.5 (suspicious) but carries
    force_page, so it pages anyway — the design-5.2 safety net, which the
    suspicious-display-only cut would otherwise silence."""
    so = orch.sensor_orchestrator
    event = _make_detection_event(alert_level="suspicious", score=0.5, force_page=True)
    orch.persistence.update.return_value = [event]
    orch.kismet.poll_devices = AsyncMock(return_value=[{"macaddr": "aa:bb:cc:dd:ee:ff"}])
    orch._kismet_active = True

    await so._poll_kismet()
    await _drain_alerts(orch)

    orch._mock_backend.send_persistence_alert.assert_called_once_with(event)   # paged
    assert so._stats.get("alerts_below_threshold", 0) == 0


@pytest.mark.asyncio
async def test_poll_kismet_dedups_repeated_device_into_one_event(orch):
    """A device that re-flags every poll updates ONE ongoing detection in place,
    not a new row each poll — the post-freeze memory-bound fix."""
    so = orch.sensor_orchestrator
    orch.kismet.poll_devices = AsyncMock(return_value=[{"macaddr": "aa:bb:cc:dd:ee:ff"}])
    orch._kismet_active = True
    for i in range(5):
        ev = _make_detection_event(alert_level="suspicious", score=0.5, observation_count=i + 2)
        orch.persistence.update.return_value = [ev]
        await so._poll_kismet()
    assert len(so.all_events) == 1                         # bounded, not 5
    assert so.all_events[0]["observation_count"] == 6      # updated in place (last i=4)
    assert "aa:bb:cc:dd:ee:ff" in so._wifi_event_index


@pytest.mark.asyncio
async def test_poll_kismet_distinct_devices_each_get_a_row(orch):
    """Distinct devices each get their own event; a repeat does not add a row."""
    so = orch.sensor_orchestrator
    orch._kismet_active = True
    for mac in ("aa:bb:cc:dd:ee:01", "aa:bb:cc:dd:ee:02", "aa:bb:cc:dd:ee:01"):
        orch.kismet.poll_devices = AsyncMock(return_value=[{"macaddr": mac}])
        orch.persistence.update.return_value = [_make_detection_event(mac=mac)]
        await so._poll_kismet()
    assert len(so.all_events) == 2                          # the repeat (01) deduped
    assert {e["mac"] for e in so.all_events} == {"aa:bb:cc:dd:ee:01", "aa:bb:cc:dd:ee:02"}


@pytest.mark.asyncio
async def test_poll_kismet_dedup_writes_one_jsonl_line_per_device(orch, tmp_path):
    """A re-flagging device appends ONE line to events.jsonl, not one per poll."""
    so = orch.sensor_orchestrator
    so._session_dir = Path(tmp_path) / "20260101_120000"
    orch.kismet.poll_devices = AsyncMock(return_value=[{"macaddr": "aa:bb:cc:dd:ee:ff"}])
    orch._kismet_active = True
    for _ in range(4):
        orch.persistence.update.return_value = [_make_detection_event()]
        await so._poll_kismet()
    lines = (so._session_dir / "events.jsonl").read_text().strip().splitlines()
    assert len(lines) == 1


@pytest.mark.asyncio
async def test_poll_kismet_persists_on_alert_level_change(orch, tmp_path):
    """A device whose alert level changes appends a fresh events.jsonl line, so a
    refresh re-seeds the CURRENT level (dedup-newest) — not the stale first-flag one.
    Re-flagging at the SAME level adds no line."""
    so = orch.sensor_orchestrator
    so._session_dir = Path(tmp_path) / "20260101_120000"
    orch.kismet.poll_devices = AsyncMock(return_value=[{"macaddr": "aa:bb:cc:dd:ee:ff"}])
    orch._kismet_active = True
    orch.persistence.update.return_value = [_make_detection_event(alert_level="suspicious", score=0.5)]
    await so._poll_kismet()                                   # first flag -> 1 line
    orch.persistence.update.return_value = [_make_detection_event(alert_level="suspicious", score=0.55)]
    await so._poll_kismet()                                   # same level -> no new line
    orch.persistence.update.return_value = [_make_detection_event(alert_level="likely", score=0.75)]
    await so._poll_kismet()                                   # level change -> +1 line
    lines = (so._session_dir / "events.jsonl").read_text().strip().splitlines()
    assert len(lines) == 2
    assert json.loads(lines[-1])["alert_level"] == "likely"


@pytest.mark.asyncio
async def test_acars_trigger_fires_once_past_threshold(orch):
    """A contact held past ACARS_TRIGGER_SECONDS requests one ACARS window, once."""
    from modules.sdr_manager import SDRMode
    so = orch.sensor_orchestrator
    so._acars_enabled = True
    so.sdr_mode = SDRMode.SHARED
    so.acars = MagicMock()
    so.aircraft_registry = None                       # skip registration resolve
    so.sdr_coordinator = MagicMock()
    so.sdr_coordinator.request_band_window = MagicMock(return_value=True)
    so._acars_trigger_s = 30

    old = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    now = datetime(2026, 1, 1, 12, 0, 40, tzinfo=timezone.utc)   # 40s later
    contact = {"icao": "abc123", "callsign": "UAL9", "segment_start": old.isoformat()}
    await so._maybe_trigger_acars(contact, "abc123", now.isoformat())
    assert contact["_acars_fired"] is True
    so.sdr_coordinator.request_band_window.assert_called_once_with("acars", so._acars_window_s)
    # Second call must NOT re-fire for the same segment.
    await so._maybe_trigger_acars(contact, "abc123", now.isoformat())
    so.sdr_coordinator.request_band_window.assert_called_once()


@pytest.mark.asyncio
async def test_acars_trigger_holds_below_threshold(orch):
    """A contact held under the threshold does not trigger."""
    from modules.sdr_manager import SDRMode
    so = orch.sensor_orchestrator
    so._acars_enabled = True
    so.sdr_mode = SDRMode.SHARED
    so.aircraft_registry = None
    so.sdr_coordinator = MagicMock()
    so._acars_trigger_s = 30
    old = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    now = datetime(2026, 1, 1, 12, 0, 10, tzinfo=timezone.utc)   # only 10s
    contact = {"icao": "abc123", "segment_start": old.isoformat()}
    await so._maybe_trigger_acars(contact, "abc123", now.isoformat())
    so.sdr_coordinator.request_band_window.assert_not_called()
    assert not contact.get("_acars_fired")


@pytest.mark.asyncio
async def test_poll_acars_correlates_by_callsign(orch, tmp_path):
    """A decoded ACARS message matches a live contact by flight-id ↔ callsign."""
    so = orch.sensor_orchestrator
    so._session_dir = Path(tmp_path) / "20260101_120000"
    so._session_dir.mkdir(parents=True, exist_ok=True)
    so._aircraft_index["abc123"] = {"icao": "abc123", "callsign": "UAL123"}
    so.acars = MagicMock()
    so._modules_active["acars"] = True
    so.acars.drain_detections = MagicMock(return_value=[
        {"tail": None, "flight_id": "UAL123", "label": "H1", "text": "OPS NORMAL",
         "timestamp": "2026-01-01T12:00:00+00:00"},
    ])
    await so._poll_acars()
    assert so._stats["acars_correlated"] == 1
    assert so._aircraft_index["abc123"]["acars"][0]["text"] == "OPS NORMAL"


@pytest.mark.asyncio
async def test_poll_acars_correlates_by_tail(orch, tmp_path):
    """Match by tail ↔ registration, normalizing padding/case."""
    so = orch.sensor_orchestrator
    so._session_dir = Path(tmp_path) / "20260101_120000"
    so._session_dir.mkdir(parents=True, exist_ok=True)
    so._aircraft_index["abc123"] = {"icao": "abc123", "registration": "N12345"}
    so.acars = MagicMock()
    so._modules_active["acars"] = True
    so.acars.drain_detections = MagicMock(return_value=[
        {"tail": ".N12345", "flight_id": None, "text": "POS", "timestamp": "2026-01-01T12:00:00+00:00"},
    ])
    await so._poll_acars()
    assert so._stats["acars_correlated"] == 1


@pytest.mark.asyncio
async def test_poll_ais_dedups_vessel_by_mmsi(orch, tmp_path):
    """Two reports for one MMSI collapse to a single event (position merged)."""
    so = orch.sensor_orchestrator
    so._session_dir = Path(tmp_path) / "20260101_120000"
    so._session_dir.mkdir(parents=True, exist_ok=True)
    so.ais = MagicMock()
    so._modules_active["ais"] = True
    so.ais.drain_detections = MagicMock(return_value=[
        {"mmsi": 366000123, "lat": None, "lon": None, "name": "TUG", "ship_type": 52,
         "timestamp": "2026-01-01T12:00:00+00:00"},
    ])
    await so._poll_ais()
    so.ais.drain_detections = MagicMock(return_value=[
        {"mmsi": 366000123, "lat": 51.4, "lon": -0.2, "name": None, "ship_type": None,
         "timestamp": "2026-01-01T12:00:05+00:00"},
    ])
    await so._poll_ais()
    assert len(so.ais_detections) == 1                     # one row per MMSI
    ev = so.ais_detections[0]
    assert ev["lat"] == 51.4 and ev["name"] == "TUG"       # position + static merged
    assert ev["observation_count"] == 2
    assert so._stats["ais_vessels_seen"] == 1


@pytest.mark.asyncio
async def test_poll_ais_drops_out_of_range_misdecode(orch, tmp_path):
    """A positioned vessel implausibly far for VHF is dropped as a misdecode; a
    near vessel and a position-less (static) report are kept."""
    so = orch.sensor_orchestrator
    so._session_dir = Path(tmp_path) / "20260101_120000"
    so._session_dir.mkdir(parents=True, exist_ok=True)
    so.ais = MagicMock()
    so._modules_active["ais"] = True
    so._ais_max_range_km = 100.0
    so._current_fix = {"lat": 51.50, "lon": -0.16}        # node reference
    so.ais.drain_detections = MagicMock(return_value=[
        {"mmsi": 367634290, "lat": 51.52, "lon": -0.19, "name": None, "ship_type": None,
         "timestamp": "2026-01-01T12:00:00+00:00"},          # ~3 km — kept
        {"mmsi": 112088045, "lat": 10.0, "lon": 100.0, "name": None, "ship_type": None,
         "timestamp": "2026-01-01T12:00:00+00:00"},          # far — dropped as misdecode
        {"mmsi": 999, "lat": None, "lon": None, "name": "DOCKED", "ship_type": 52,
         "timestamp": "2026-01-01T12:00:00+00:00"},          # no position — kept
    ])
    await so._poll_ais()
    seen = {e["mmsi"] for e in so.ais_detections}
    assert seen == {367634290, 999}
    assert so._stats["ais_out_of_range_dropped"] == 1


@pytest.mark.asyncio
async def test_poll_ais_no_gps_fix_disables_range_filter(orch, tmp_path):
    """With no GPS fix, the range filter is off (never silently hides everything)."""
    so = orch.sensor_orchestrator
    so._session_dir = Path(tmp_path) / "20260101_120000"
    so._session_dir.mkdir(parents=True, exist_ok=True)
    so.ais = MagicMock()
    so._modules_active["ais"] = True
    so._current_fix = None
    so.ais.drain_detections = MagicMock(return_value=[
        {"mmsi": 112088045, "lat": 10.0, "lon": 100.0, "name": None, "ship_type": None,
         "timestamp": "2026-01-01T12:00:00+00:00"},
    ])
    await so._poll_ais()
    assert {e["mmsi"] for e in so.ais_detections} == {112088045}   # kept (no fix → no filter)


@pytest.mark.asyncio
async def test_poll_ais_auto_disable_marks_inactive(orch):
    """An auto-disabled AIS module greys the sensor like DroneRF does."""
    so = orch.sensor_orchestrator
    so.ais = MagicMock(auto_disabled=True)
    so.ais.drain_detections = MagicMock(return_value=[])
    so._modules_active["ais"] = True
    await so._poll_ais()
    assert so._modules_active["ais"] is False


def test_record_alert_persists_and_pushes(orch, tmp_path):
    """_record_alert writes one line to alerts.jsonl AND pushes to the GUI feed,
    so alerts are durable (P5) and the Alerts tab is fed for the first time."""
    so = orch.sensor_orchestrator
    so._session_dir = Path(tmp_path) / "20260101_120000"
    so.gui_server = MagicMock()
    so._record_alert("wifi", "PHONE-LINKSYS-3 — likely", "score 0.80, 12 obs",
                     severity="likely", mac="aa:bb:cc:dd:ee:ff")

    lines = (so._session_dir / "alerts.jsonl").read_text().strip().splitlines()
    assert len(lines) == 1
    rec = json.loads(lines[0])
    assert rec["kind"] == "wifi"
    assert rec["title"] == "PHONE-LINKSYS-3 — likely"
    assert rec["severity"] == "likely"
    assert rec["mac"] == "aa:bb:cc:dd:ee:ff"
    assert "timestamp" in rec

    so.gui_server.push_event.assert_called_once()
    evt_type, payload = so.gui_server.push_event.call_args[0]
    assert evt_type == "alert"
    assert payload["title"] == "PHONE-LINKSYS-3 — likely"


def test_record_alert_survives_no_gui_server(orch, tmp_path):
    """With no GUI attached, _record_alert still persists to disk and does not raise."""
    so = orch.sensor_orchestrator
    so._session_dir = Path(tmp_path) / "20260101_120000"
    so.gui_server = None
    so._record_alert("drone", "Drone RF — 915 MHz", "915 MHz at -18.0 dB")
    lines = (so._session_dir / "alerts.jsonl").read_text().strip().splitlines()
    assert len(lines) == 1


# ---------------------------------------------------------------------------
# shutdown()
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_shutdown_writes_session_summary(orch, tmp_path):
    orch.session_id = "20260101_120000"
    orch.sensor_orchestrator._session_dir = Path(tmp_path) / "20260101_120000"
    orch._session_dir = Path(tmp_path) / "20260101_120000"
    await orch.startup()
    await orch.shutdown()

    summary_path = tmp_path / "20260101_120000" / "summary.json"
    assert summary_path.exists(), "summary.json was not created"

    data = json.loads(summary_path.read_text())
    assert data["session_id"] == "20260101_120000"
    assert "start_time" in data
    assert "end_time" in data
    assert "duration_seconds" in data


@pytest.mark.asyncio
async def test_shutdown_calls_shapefile_writer_when_events_present(orch):
    orch._kismet_active = True
    orch.sensor_orchestrator.all_events = [
        {"event_type": "wifi", "mac": "aa:bb:cc:dd:ee:ff", "lat": 51.5, "lon": -0.1}
    ]
    await orch.startup()
    await orch.shutdown()

    orch._mock_shp.write_session.assert_called_once()
    orch._mock_shp.write_geojson.assert_called_once()


@pytest.mark.asyncio
async def test_shutdown_calls_wigle_uploader_when_configured(orch, tmp_path):
    csv_path = str(tmp_path / "Kismet-test.wiglecsv")
    Path(csv_path).write_text("WiGLE CSV header\n")

    orch._mock_wigle.is_configured.return_value = True
    orch._mock_wigle.find_latest_csv.return_value = csv_path
    orch.kismet.get_wigle_csv_path.return_value = None  # force find_latest_csv path

    await orch.startup()
    await orch.shutdown()

    orch._mock_wigle.upload_session.assert_called_once_with(csv_path)


@pytest.mark.asyncio
async def test_shutdown_completes_cleanly_with_no_events(orch):
    """Shutdown must complete without error even when no sensor events were collected."""
    await orch.startup()
    await orch.shutdown()  # no events — should not raise

    orch._mock_shp.write_session.assert_not_called()
    orch._mock_shp.write_geojson.assert_not_called()


# ---------------------------------------------------------------------------
# _poll_kismet() — persistence wiring and JSONL logging
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_poll_kismet_calls_persistence_update(orch):
    """_poll_kismet() must pass polled devices to PersistenceEngine.update()."""
    device = {"macaddr": "aa:bb:cc:dd:ee:ff", "kismet.device.base.name": "testdev"}
    orch.kismet.poll_devices = AsyncMock(return_value=[device])
    orch._kismet_active = True

    await orch.sensor_orchestrator._poll_kismet()

    orch.persistence.update.assert_called_once_with([device], gps_fix=orch.sensor_orchestrator._current_fix)



@pytest.mark.asyncio
async def test_poll_kismet_appends_events_to_jsonl(orch, tmp_path):
    """Detection events above threshold must be appended to events.jsonl."""
    event = _make_detection_event(alert_level="high", score=0.95)
    orch.persistence.update.return_value = [event]
    orch.kismet.poll_devices = AsyncMock(return_value=[{"macaddr": "aa:bb:cc:dd:ee:ff"}])
    orch._kismet_active = True
    orch.sensor_orchestrator._session_dir = Path(tmp_path) / "20260101_120000"

    await orch.sensor_orchestrator._poll_kismet()

    jsonl_path = orch.sensor_orchestrator._session_dir / "events.jsonl"
    assert jsonl_path.exists(), "events.jsonl was not created"
    line = json.loads(jsonl_path.read_text().strip())
    assert line["mac"] == "aa:bb:cc:dd:ee:ff"
    assert line["event_type"] == "wifi"


@pytest.mark.asyncio
async def test_poll_adsb_appends_to_jsonl(orch, tmp_path):
    """Aircraft detections must be appended to aircraft.jsonl."""
    aircraft = _make_aircraft(icao="TEST01")
    orch.adsb.poll_aircraft = AsyncMock(return_value=[aircraft])
    orch._adsb_active = True
    orch.sensor_orchestrator._session_dir = Path(tmp_path) / "20260101_120000"

    await orch.sensor_orchestrator._poll_adsb()

    jsonl_path = orch.sensor_orchestrator._session_dir / "aircraft.jsonl"
    assert jsonl_path.exists(), "aircraft.jsonl was not created"
    line = json.loads(jsonl_path.read_text().strip())
    assert line["icao"] == "TEST01"


@pytest.mark.asyncio
async def test_poll_adsb_dedups_by_icao_into_one_track(orch):
    """One plane re-seen over many polls becomes ONE event with a positions[]
    track, not a row per sighting."""
    so = orch.sensor_orchestrator
    orch._adsb_active = True
    for la in (51.50, 51.52, 51.54, 51.56):   # ~2 km apart -> each is a track point
        orch.adsb.poll_aircraft = AsyncMock(return_value=[_make_aircraft(icao="ABC123", lat=la, lon=-0.1)])
        await so._poll_adsb()
    assert len(so.aircraft_detections) == 1
    assert so.aircraft_detections[0]["icao"] == "ABC123"
    assert len(so.aircraft_detections[0]["positions"]) == 4
    assert "ABC123" in so._aircraft_index


@pytest.mark.asyncio
async def test_poll_adsb_pushes_stationary_positioned_contact_each_poll(orch):
    """A positioned plane whose fix isn't advancing must still reach the live map on
    every poll. readsb holds a plane's position with a growing seen_pos while it isn't
    sending fresh fixes (fringe reception, slow/distant target), so the position sits
    frozen across polls and the track never 'moves'. Gating the GUI push on track
    movement left such a plane in the table/API but never pushed it to the map."""
    so = orch.sensor_orchestrator
    orch._adsb_active = True
    so.gui_server = MagicMock()
    # Identical position every poll — the track never advances, so the old
    # (moved or returned) gate would push only on the very first sighting.
    frozen = _make_aircraft(icao="ABC123", lat=51.5, lon=-0.1, altitude=35000)
    orch.adsb.poll_aircraft = AsyncMock(return_value=[frozen])
    await so._poll_adsb()                       # first sighting (new event) pushes
    so.gui_server.push_event.reset_mock()
    await so._poll_adsb()                       # same frozen fix — must STILL push
    pushed = [c for c in so.gui_server.push_event.call_args_list
              if c.args and c.args[0] == "aircraft"]
    assert pushed, "a stationary positioned contact must be pushed to the live map each poll"
    assert pushed[-1].args[1]["lat"] == 51.5 and pushed[-1].args[1]["lon"] == -0.1


@pytest.mark.asyncio
async def test_aircraft_track_thinned_for_stationary_target(orch):
    """A target reporting the same position each poll adds ONE point, not N."""
    so = orch.sensor_orchestrator
    orch._adsb_active = True
    for _ in range(5):
        orch.adsb.poll_aircraft = AsyncMock(return_value=[_make_aircraft(icao="STILL1", lat=51.5, lon=-0.1)])
        await so._poll_adsb()
    assert len(so.aircraft_detections) == 1
    assert len(so.aircraft_detections[0]["positions"]) == 1


@pytest.mark.asyncio
async def test_aircraft_distinct_icao_get_separate_events(orch):
    so = orch.sensor_orchestrator
    orch._adsb_active = True
    orch.adsb.poll_aircraft = AsyncMock(return_value=[
        _make_aircraft(icao="AAA111", lat=51.5, lon=-0.1),
        _make_aircraft(icao="BBB222", lat=52.0, lon=-1.0),
    ])
    await so._poll_adsb()
    assert len(so.aircraft_detections) == 2
    assert {e["icao"] for e in so.aircraft_detections} == {"AAA111", "BBB222"}


@pytest.mark.asyncio
async def test_aircraft_positionless_sighting_adds_no_track_point(orch):
    """A sighting with no lat/lon updates state but contributes no track point."""
    so = orch.sensor_orchestrator
    orch._adsb_active = True
    orch.adsb.poll_aircraft = AsyncMock(return_value=[_make_aircraft(icao="NOPOS", lat=None, lon=None)])
    await so._poll_adsb()
    assert len(so.aircraft_detections) == 1
    assert so.aircraft_detections[0]["positions"] == []


@pytest.mark.asyncio
async def test_aircraft_track_is_bounded(orch):
    """A long-loitering target's track is capped (oldest dropped), not unbounded."""
    so = orch.sensor_orchestrator
    so._track_max_points = 3
    orch._adsb_active = True
    lat = 51.50
    for _ in range(8):
        lat += 0.02   # keep moving so every poll is a fresh track point
        orch.adsb.poll_aircraft = AsyncMock(return_value=[_make_aircraft(icao="ORBIT1", lat=lat, lon=-0.1)])
        await so._poll_adsb()
    assert len(so.aircraft_detections[0]["positions"]) == 3


@pytest.mark.asyncio
async def test_aircraft_return_after_gap_is_flagged(orch):
    """A re-sighting after a gap > AIRCRAFT_RETURN_GAP_SECONDS flags the SAME
    airframe as returning (of-interest), with a count and a marked track gap."""
    so = orch.sensor_orchestrator
    so._adsb_active = orch._adsb_active = True
    so._aircraft_return_gap_s = 600
    orch.adsb.poll_aircraft = AsyncMock(return_value=[_make_aircraft(icao="RET001", lat=51.50, lon=-0.1)])
    await so._poll_adsb()
    assert so._aircraft_index["RET001"].get("returning") is not True
    # Backdate last sighting 20 min so the next poll reads as a return.
    so._aircraft_index["RET001"]["timestamp"] = (
        datetime.now(timezone.utc) - timedelta(minutes=20)).isoformat()
    orch.adsb.poll_aircraft = AsyncMock(return_value=[_make_aircraft(icao="RET001", lat=51.60, lon=-0.1)])
    await so._poll_adsb()
    ev = so._aircraft_index["RET001"]
    assert ev["returning"] is True
    assert ev["return_count"] == 1
    assert ev["last_gap_seconds"] >= 600
    assert any(p.get("gap") for p in ev["positions"]), "track gap not marked"
    assert so._stats["aircraft_returns"] == 1


@pytest.mark.asyncio
async def test_aircraft_return_persists_to_jsonl(orch):
    """A re-acquisition after a gap appends a fresh aircraft.jsonl line (newest has
    returning=True) so the return survives a restart; continuous sightings do not."""
    so = orch.sensor_orchestrator
    so._adsb_active = orch._adsb_active = True
    so._aircraft_return_gap_s = 600
    orch.adsb.poll_aircraft = AsyncMock(return_value=[_make_aircraft(icao="RET777", lat=51.50, lon=-0.1)])
    await so._poll_adsb()                                   # first contact -> 1 line
    orch.adsb.poll_aircraft = AsyncMock(return_value=[_make_aircraft(icao="RET777", lat=51.51, lon=-0.1)])
    await so._poll_adsb()                                   # continuous -> no new line
    lines = (so._session_dir / "aircraft.jsonl").read_text().strip().splitlines()
    assert len(lines) == 1
    # Backdate so the next sighting reads as a return.
    so._aircraft_index["RET777"]["timestamp"] = (
        datetime.now(timezone.utc) - timedelta(minutes=20)).isoformat()
    orch.adsb.poll_aircraft = AsyncMock(return_value=[_make_aircraft(icao="RET777", lat=51.60, lon=-0.1)])
    await so._poll_adsb()                                   # return -> +1 line
    lines = (so._session_dir / "aircraft.jsonl").read_text().strip().splitlines()
    assert len(lines) == 2
    assert json.loads(lines[-1])["returning"] is True


@pytest.mark.asyncio
async def test_aircraft_no_return_within_gap(orch):
    """Continuous tracking (re-seen within the gap window) is NOT a return."""
    so = orch.sensor_orchestrator
    so._adsb_active = orch._adsb_active = True
    so._aircraft_return_gap_s = 600
    for la in (51.50, 51.52):
        orch.adsb.poll_aircraft = AsyncMock(return_value=[_make_aircraft(icao="CONT01", lat=la, lon=-0.1)])
        await so._poll_adsb()
    ev = so._aircraft_index["CONT01"]
    assert ev.get("returning") is not True
    assert so._stats["aircraft_returns"] == 0
    assert not any(p.get("gap") for p in ev["positions"])


@pytest.mark.asyncio
async def test_poll_adsb_skips_idless_aircraft(orch):
    """An aircraft with no ICAO is not merged into one bogus 'unknown' airframe."""
    so = orch.sensor_orchestrator
    orch._adsb_active = True
    orch.adsb.poll_aircraft = AsyncMock(return_value=[
        _make_aircraft(icao="", lat=51.5, lon=-0.1),
        _make_aircraft(icao=None, lat=52.0, lon=-1.0),
    ])
    await so._poll_adsb()
    assert "unknown" not in so._aircraft_index
    assert so._aircraft_index == {}
    assert so.aircraft_detections == []
    assert so._stats["aircraft_idless_skipped"] == 2


# ---------------------------------------------------------------------------
# P7 — air-of-interest persistence scoring + alert gating
# ---------------------------------------------------------------------------


def _orbit_positions(n=12, dt_s=60, alt=1500):
    import math
    clat, clon = 51.5, -0.1
    r = 0.5 / 60.0
    out = []
    for i in range(n):
        ang = 2 * math.pi * i / (n - 1)
        out.append({
            "lat": clat + r * math.cos(ang),
            "lon": clon + r * math.sin(ang) / math.cos(math.radians(clat)),
            "altitude": alt,
            "timestamp": (datetime(2026, 6, 16, tzinfo=timezone.utc) + timedelta(seconds=i * dt_s)).isoformat(),
        })
    return out


@pytest.mark.asyncio
async def test_score_aircraft_orbit_flags_of_interest(orch):
    """_score_aircraft stashes an of-interest score for an orbit near the node."""
    so = orch.sensor_orchestrator
    so._current_fix = {"lat": 51.5, "lon": -0.1, "utc": "2026-06-16T00:10:00Z"}
    event = {"callsign": "TEST1", "positions": _orbit_positions()}
    air = so._score_aircraft(event)
    assert air.of_interest
    assert event["air_of_interest"] is True
    assert event["air_severity"] in ("likely", "high")


@pytest.mark.asyncio
async def test_score_aircraft_no_reference_is_zero(orch):
    """No GPS/home reference -> no geometry -> score 0, not of-interest."""
    so = orch.sensor_orchestrator
    so._current_fix = None
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("AIR_HOME_LAT", None)
        os.environ.pop("AIR_HOME_LON", None)
        event = {"callsign": "TEST1", "positions": _orbit_positions()}
        air = so._score_aircraft(event)
    assert air.score == 0.0
    assert not air.of_interest


@pytest.mark.asyncio
async def test_transit_aircraft_does_not_alert(orch):
    """A not-of-interest aircraft (transit) fires no alert — the P7 reframe."""
    from modules.air_scoring import AirScore
    so = orch.sensor_orchestrator
    orch._adsb_active = True
    so._stats["alerts_sent"] = 0
    with patch.object(so, "_score_aircraft", return_value=AirScore(score=0.1, severity=None, of_interest=False)):
        orch.adsb.poll_aircraft = AsyncMock(return_value=[_make_aircraft(icao="TRAN01", lat=51.5, lon=-0.1)])
        await so._poll_adsb()
    assert so._stats["alerts_sent"] == 0
    assert so._stats["alerts_rate_limited"] == 0


@pytest.mark.asyncio
async def test_of_interest_aircraft_alerts(orch):
    """An of-interest aircraft fires an alert at its score severity."""
    from modules.air_scoring import AirScore
    so = orch.sensor_orchestrator
    orch._adsb_active = True
    so._stats["alerts_sent"] = 0
    with patch.object(so, "_score_aircraft",
                      return_value=AirScore(score=0.95, severity="high", of_interest=True)):
        orch.adsb.poll_aircraft = AsyncMock(return_value=[_make_aircraft(icao="ORBIT9", lat=51.5, lon=-0.1)])
        await so._poll_adsb()
    assert so._stats["alerts_sent"] == 1


@pytest.mark.asyncio
async def test_emergency_alerts_regardless_of_score(orch):
    """An emergency squawk alerts even when the persistence score is nil."""
    from modules.air_scoring import AirScore
    so = orch.sensor_orchestrator
    orch._adsb_active = True
    so._stats["alerts_sent"] = 0
    em = _make_aircraft(icao="EMER01", lat=51.5, lon=-0.1)
    em["emergency"] = True
    with patch.object(so, "_score_aircraft", return_value=AirScore(of_interest=False)):
        orch.adsb.poll_aircraft = AsyncMock(return_value=[em])
        await so._poll_adsb()
    assert so._stats["alerts_sent"] == 1


# ---------------------------------------------------------------------------
# _poll_drone_rf() — dedup by frequency band
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_poll_drone_dedups_by_band_into_one_event(orch):
    """A band heard on every sweep becomes ONE event with a running count,
    not a row per sweep."""
    so = orch.sensor_orchestrator
    for _ in range(4):
        so.drone_rf.drain_detections = MagicMock(return_value=[_make_drone(freq_mhz=2400.0)])
        await so._poll_drone_rf()
    assert len(so.drone_detections) == 1
    assert so.drone_detections[0]["observation_count"] == 4
    assert "2400" in so._drone_index


@pytest.mark.asyncio
async def test_drone_distinct_bands_get_separate_events(orch):
    so = orch.sensor_orchestrator
    so.drone_rf.drain_detections = MagicMock(return_value=[
        _make_drone(freq_mhz=2400.0),
        _make_drone(freq_mhz=5800.0),
    ])
    await so._poll_drone_rf()
    assert len(so.drone_detections) == 2
    assert {str(int(e["freq_mhz"])) for e in so.drone_detections} == {"2400", "5800"}


@pytest.mark.asyncio
async def test_drone_tracks_peak_and_latest_power(orch):
    """The deduped event keeps the latest power and the peak seen on the band."""
    so = orch.sensor_orchestrator
    for p in (-40.0, -20.0, -35.0):
        so.drone_rf.drain_detections = MagicMock(return_value=[_make_drone(freq_mhz=2400.0, power_db=p)])
        await so._poll_drone_rf()
    event = so.drone_detections[0]
    assert event["power_db"] == -35.0      # latest
    assert event["peak_power_db"] == -20.0  # strongest


@pytest.mark.asyncio
async def test_drone_single_sweep_does_not_alert(orch):
    """P7: a single fleeting RF blip is shown but not paged (persistence gate)."""
    so = orch.sensor_orchestrator
    so._drone_min_sweeps = 2
    so._stats["alerts_sent"] = 0
    so.drone_rf.drain_detections = MagicMock(return_value=[_make_drone(freq_mhz=2400.0)])
    await so._poll_drone_rf()
    assert len(so.drone_detections) == 1     # still shown
    assert so._stats["alerts_sent"] == 0     # but not paged


@pytest.mark.asyncio
async def test_drone_sustained_presence_alerts(orch):
    """P7: a band heard on enough sweeps crosses the persistence gate and alerts."""
    so = orch.sensor_orchestrator
    so._drone_min_sweeps = 2
    so._stats["alerts_sent"] = 0
    for _ in range(2):
        so.drone_rf.drain_detections = MagicMock(return_value=[_make_drone(freq_mhz=2400.0)])
        await so._poll_drone_rf()
    assert so._stats["alerts_sent"] >= 1


@pytest.mark.asyncio
async def test_poll_drone_appends_every_sweep_to_jsonl(orch, tmp_path):
    """Every sweep is logged to drone.jsonl (forensic) even though the in-memory
    list holds one row per band."""
    so = orch.sensor_orchestrator
    so._session_dir = Path(tmp_path) / "20260101_120000"
    for _ in range(3):
        so.drone_rf.drain_detections = MagicMock(return_value=[_make_drone(freq_mhz=2400.0)])
        await so._poll_drone_rf()
    jsonl_path = so._session_dir / "drone.jsonl"
    assert len(jsonl_path.read_text().strip().splitlines()) == 3
    assert len(so.drone_detections) == 1


# ---------------------------------------------------------------------------
# _poll_remote_id() — dedup by UAS ID
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_poll_remote_id_dedups_by_uas_into_one_track(orch):
    """A drone broadcasting every frame becomes ONE event accumulating a
    positions[] flight path, not a row per frame."""
    so = orch.sensor_orchestrator
    for la in (51.50, 51.52, 51.54):   # ~2 km apart -> each is a track point
        so.remote_id.poll = AsyncMock(return_value=[_make_remote_id(uas_id="UAS-1", drone_lat=la)])
        await so._poll_remote_id()
    assert len(so.remote_id_detections) == 1
    assert so.remote_id_detections[0]["uas_id"] == "UAS-1"
    assert len(so.remote_id_detections[0]["positions"]) == 3
    assert "UAS-1" in so._remote_id_index


@pytest.mark.asyncio
async def test_remote_id_distinct_uas_get_separate_events(orch):
    so = orch.sensor_orchestrator
    so.remote_id.poll = AsyncMock(return_value=[
        _make_remote_id(uas_id="UAS-1", drone_lat=51.5),
        _make_remote_id(uas_id="UAS-2", drone_lat=52.0),
    ])
    await so._poll_remote_id()
    assert len(so.remote_id_detections) == 2
    assert {e["uas_id"] for e in so.remote_id_detections} == {"UAS-1", "UAS-2"}


@pytest.mark.asyncio
async def test_poll_remote_id_appends_every_frame_to_jsonl(orch, tmp_path):
    """Every frame is logged to remote_id.jsonl even though the in-memory list
    holds one event per UAS ID."""
    so = orch.sensor_orchestrator
    so._session_dir = Path(tmp_path) / "20260101_120000"
    for la in (51.50, 51.52, 51.54):
        so.remote_id.poll = AsyncMock(return_value=[_make_remote_id(uas_id="UAS-1", drone_lat=la)])
        await so._poll_remote_id()
    jsonl_path = so._session_dir / "remote_id.jsonl"
    assert len(jsonl_path.read_text().strip().splitlines()) == 3
    assert len(so.remote_id_detections) == 1


@pytest.mark.asyncio
async def test_poll_remote_id_pushes_to_gui(orch):
    """A Remote ID detection is pushed to the GUI as a 'remote_id' event (P6)."""
    from unittest.mock import MagicMock
    so = orch.sensor_orchestrator
    so.gui_server = MagicMock()
    so.remote_id.poll = AsyncMock(return_value=[_make_remote_id(uas_id="UAS-9")])
    await so._poll_remote_id()
    kinds = [c.args[0] for c in so.gui_server.push_event.call_args_list]
    assert "remote_id" in kinds


def test_prune_remote_id_index_drops_stale(orch):
    """Departed UAS past the retention window are expired so the index stays bounded."""
    so = orch.sensor_orchestrator
    so._aircraft_retention_s = 3600
    now = datetime.now(timezone.utc)
    so._remote_id_index = {
        "FRESH": {"uas_id": "FRESH", "timestamp": now.isoformat()},
        "STALE": {"uas_id": "STALE",
                  "timestamp": (now - timedelta(seconds=7200)).isoformat()},
    }
    so._prune_remote_id_index(now)
    assert set(so._remote_id_index) == {"FRESH"}


def test_current_remote_id_only_within_window(orch):
    """current_remote_id() returns only contacts inside the retention window."""
    so = orch.sensor_orchestrator
    so._aircraft_retention_s = 3600
    now = datetime.now(timezone.utc)
    so._remote_id_index = {
        "FRESH": {"uas_id": "FRESH", "timestamp": now.isoformat()},
        "OLD": {"uas_id": "OLD",
                "timestamp": (now - timedelta(seconds=7200)).isoformat()},
    }
    ids = {e["uas_id"] for e in so.current_remote_id()}
    assert ids == {"FRESH"}


# ---------------------------------------------------------------------------
# _log_health_banner()
# ---------------------------------------------------------------------------


def test_health_banner_logs_at_info(orch, caplog):
    """_log_health_banner() emits at least one INFO-level log line."""
    import logging
    with caplog.at_level(logging.INFO):
        orch.sensor_orchestrator._log_health_banner()
    assert any(r.levelno == logging.INFO for r in caplog.records)


def test_health_banner_includes_session_id(orch, caplog):
    """Health banner must contain the session ID."""
    import logging
    orch.sensor_orchestrator.session_id = "20260101_120000"
    with caplog.at_level(logging.INFO):
        orch.sensor_orchestrator._log_health_banner()
    all_msgs = " ".join(r.message for r in caplog.records)
    assert "20260101_120000" in all_msgs


def test_health_banner_reflects_sensor_degradation(orch, caplog):
    """Health banner must show Degraded when a sensor is unhealthy."""
    import logging
    orch.sensor_orchestrator._sensor_health["kismet"] = False
    with caplog.at_level(logging.INFO):
        orch.sensor_orchestrator._log_health_banner()
    all_msgs = " ".join(r.message for r in caplog.records)
    assert "Degraded" in all_msgs


# ---------------------------------------------------------------------------
# _reconnect()
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reconnect_returns_true_on_success(orch):
    """_reconnect() returns True and sets sensor health when reconnect succeeds."""
    orch.sensor_orchestrator._sensor_health["kismet"] = False
    orch.kismet.close = AsyncMock()
    orch.kismet.connect = AsyncMock()
    with patch("asyncio.sleep", new_callable=AsyncMock):
        result = await orch.sensor_orchestrator._reconnect("kismet")
    assert result is True
    assert orch.sensor_orchestrator._sensor_health["kismet"] is True


@pytest.mark.asyncio
async def test_reconnect_returns_false_after_max_attempts(orch):
    """_reconnect() returns False when all reconnect attempts fail."""
    orch.sensor_orchestrator._max_reconnect_attempts = 3
    orch.kismet.close = AsyncMock()
    orch.kismet.connect = AsyncMock(side_effect=ConnectionError("still down"))
    with patch("asyncio.sleep", new_callable=AsyncMock):
        result = await orch.sensor_orchestrator._reconnect("kismet")
    assert result is False


@pytest.mark.asyncio
async def test_reconnect_logs_warning_on_each_attempt(orch, caplog):
    """_reconnect() must log a WARNING for each attempt."""
    import logging
    orch.sensor_orchestrator._max_reconnect_attempts = 2
    orch.kismet.close = AsyncMock()
    orch.kismet.connect = AsyncMock(side_effect=ConnectionError("down"))
    with patch("asyncio.sleep", new_callable=AsyncMock):
        with caplog.at_level(logging.WARNING):
            await orch.sensor_orchestrator._reconnect("kismet")
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) >= 2


@pytest.mark.asyncio
async def test_reconnect_logs_error_when_all_attempts_fail(orch, caplog):
    """_reconnect() must log ERROR after exhausting all attempts."""
    import logging
    orch.sensor_orchestrator._max_reconnect_attempts = 1
    orch.kismet.close = AsyncMock()
    orch.kismet.connect = AsyncMock(side_effect=ConnectionError("down"))
    with patch("asyncio.sleep", new_callable=AsyncMock):
        with caplog.at_level(logging.ERROR):
            await orch.sensor_orchestrator._reconnect("kismet")
    assert any(r.levelno == logging.ERROR for r in caplog.records)


# ---------------------------------------------------------------------------
# Poll loop reconnect triggering
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_poll_kismet_triggers_reconnect_on_health_transition(orch):
    """_poll_kismet() calls _reconnect() when sensor first degrades."""
    orch._kismet_active = True
    orch.sensor_orchestrator._sensor_health["kismet"] = True
    orch.kismet.poll_devices = AsyncMock(side_effect=ConnectionError("down"))

    with patch.object(orch.sensor_orchestrator, "_reconnect", new_callable=AsyncMock, return_value=True) as mock_rc:
        await orch.sensor_orchestrator._poll_kismet()

    mock_rc.assert_called_once_with("kismet")


@pytest.mark.asyncio
async def test_poll_kismet_does_not_reconnect_on_repeated_failure(orch):
    """_poll_kismet() does NOT call _reconnect() when sensor is already degraded."""
    orch._kismet_active = True
    orch.sensor_orchestrator._sensor_health["kismet"] = False  # already degraded
    orch.kismet.poll_devices = AsyncMock(side_effect=ConnectionError("still down"))

    with patch.object(orch.sensor_orchestrator, "_reconnect", new_callable=AsyncMock) as mock_rc:
        await orch.sensor_orchestrator._poll_kismet()

    mock_rc.assert_not_called()


# ---------------------------------------------------------------------------
# shutdown() — field hardening: guaranteed writes even on partial failure
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_shutdown_writes_summary_even_when_shapefile_fails(orch, tmp_path):
    """summary.json must be written even if ShapefileWriter raises."""
    orch.session_id = "20260101_120000"
    orch.sensor_orchestrator._session_dir = Path(tmp_path) / "20260101_120000"
    orch._session_dir = Path(tmp_path) / "20260101_120000"
    orch.sensor_orchestrator.all_events = [
        {"event_type": "wifi", "mac": "aa:bb:cc:dd:ee:ff", "lat": 51.5, "lon": -0.1}
    ]
    orch._mock_shp.write_session.side_effect = RuntimeError("geopandas crash")
    orch._mock_shp.write_geojson.side_effect = RuntimeError("geopandas crash")

    await orch.startup()
    await orch.shutdown()

    summary_path = tmp_path / "20260101_120000" / "summary.json"
    assert summary_path.exists(), "summary.json must survive a shapefile failure"
    data = json.loads(summary_path.read_text())
    assert data["session_id"] == "20260101_120000"


@pytest.mark.asyncio
async def test_shutdown_writes_summary_even_when_wigle_fails(orch, tmp_path):
    """summary.json must be written even if WiGLE upload raises."""
    orch.session_id = "20260101_120000"
    orch.sensor_orchestrator._session_dir = Path(tmp_path) / "20260101_120000"
    orch._session_dir = Path(tmp_path) / "20260101_120000"
    orch._mock_wigle.is_configured.return_value = True
    orch._mock_wigle.upload_session.side_effect = RuntimeError("WiGLE down")
    orch._mock_wigle.find_latest_csv.return_value = "/tmp/fake.wiglecsv"
    orch.kismet.get_wigle_csv_path.return_value = None

    await orch.startup()
    await orch.shutdown()

    summary_path = tmp_path / "20260101_120000" / "summary.json"
    assert summary_path.exists(), "summary.json must survive a WiGLE failure"
    data = json.loads(summary_path.read_text())
    assert data["session_id"] == "20260101_120000"


@pytest.mark.asyncio
async def test_emergency_flush_writes_jsonl_without_geopandas(orch, tmp_path):
    """_emergency_flush() must write all in-memory events using only stdlib."""
    orch.session_id = "20260101_120000"
    orch.sensor_orchestrator._session_dir = Path(tmp_path) / "20260101_120000"
    orch._session_dir = Path(tmp_path) / "20260101_120000"
    orch.sensor_orchestrator.all_events = [
        {"event_type": "wifi", "mac": "aa:bb:cc:dd:ee:ff", "lat": 51.5, "lon": -0.1}
    ]
    orch.sensor_orchestrator.aircraft_detections = [
        {"event_type": "aircraft", "icao": "ABC123", "lat": 51.5, "lon": -0.1}
    ]
    orch.sensor_orchestrator.drone_detections = []

    with patch.dict("sys.modules", {"geopandas": None}):
        orch._emergency_flush()

    dump_path = tmp_path / "20260101_120000" / "emergency_dump.jsonl"
    assert dump_path.exists(), "emergency_dump.jsonl must be created"
    lines = [json.loads(line) for line in dump_path.read_text().strip().splitlines()]
    assert len(lines) == 2
    assert any(line["event_type"] == "wifi" for line in lines)
    assert any(line["event_type"] == "aircraft" for line in lines)


# ---------------------------------------------------------------------------
# Sensor stall watchdog — catch a silently-hung poll loop
# ---------------------------------------------------------------------------


def test_mark_poll_records_timestamp(orch):
    so = orch.sensor_orchestrator
    assert so._last_poll_ts.get("kismet") is None
    so._mark_poll("kismet")
    assert so._last_poll_ts.get("kismet") is not None


def test_watchdog_flags_stalled_active_sensor(orch):
    # A sensor that has not completed a poll in longer than the stall threshold,
    # while still showing healthy, is flipped to degraded and alerted.
    so = orch.sensor_orchestrator
    so._modules_active["kismet"] = True
    so._sensor_health["kismet"] = True
    so._console_alert = MagicMock()
    so._last_poll_ts["kismet"] = time.monotonic() - (so._watchdog_stall_s + 60)
    so._check_watchdog()
    assert so._sensor_health["kismet"] is False
    so._console_alert.assert_called_once()


def test_watchdog_ignores_recently_polled_sensor(orch):
    so = orch.sensor_orchestrator
    so._modules_active["adsb"] = True
    so._sensor_health["adsb"] = True
    so._last_poll_ts["adsb"] = time.monotonic()        # just polled
    so._check_watchdog()
    assert so._sensor_health["adsb"] is True


def test_watchdog_skips_inactive_sensor(orch):
    # A disabled sensor (e.g. DroneRF off) is never flagged stalled.
    so = orch.sensor_orchestrator
    so._modules_active["drone_rf"] = False
    so._sensor_health["drone_rf"] = True
    so._last_poll_ts["drone_rf"] = time.monotonic() - 10_000
    so._check_watchdog()
    assert so._sensor_health["drone_rf"] is True


def test_watchdog_startup_grace_before_first_poll(orch):
    # No timestamp yet (never polled) -> not flagged during startup.
    so = orch.sensor_orchestrator
    so._modules_active["kismet"] = True
    so._sensor_health["kismet"] = True
    so._last_poll_ts.pop("kismet", None)
    so._check_watchdog()
    assert so._sensor_health["kismet"] is True


# ---------------------------------------------------------------------------
# Data-progress watchdog — catch a frozen counter while the loop still completes
# ---------------------------------------------------------------------------


def _arm_data_stall(so, name="kismet", frozen_for=None):
    """Make *name*'s data counter look frozen for longer than the threshold.

    Seeds the last-progress snapshot to the current counter value and pushes its
    timestamp into the past, so the next _check_watchdog sees no advance.
    """
    if frozen_for is None:
        frozen_for = so._data_stall_s + 60
    stat_key = so._data_stat_key[name]
    so._last_progress_value[name] = so._stats.get(stat_key, 0)
    so._last_progress_ts[name] = time.monotonic() - frozen_for


def test_watchdog_data_stall_trips_on_frozen_counter(orch):
    # Loop keeps completing (fresh _last_poll_ts) but the cumulative counter has
    # not advanced past the data-stall threshold -> capture frozen -> degraded.
    so = orch.sensor_orchestrator
    so._modules_active["kismet"] = True
    so._sensor_health["kismet"] = True
    so._console_alert = MagicMock()
    so._last_poll_ts["kismet"] = time.monotonic()   # loop is alive
    _arm_data_stall(so, "kismet")
    tripped = so._check_watchdog()
    assert so._sensor_health["kismet"] is False
    assert "kismet" in tripped
    so._console_alert.assert_called_once()


def test_watchdog_data_progress_resets_on_advance(orch):
    # If the counter advances, the data-stall baseline is refreshed and the sensor
    # is NOT flagged even though the previous snapshot was old.
    so = orch.sensor_orchestrator
    so._modules_active["kismet"] = True
    so._sensor_health["kismet"] = True
    so._last_poll_ts["kismet"] = time.monotonic()
    so._stats["kismet_devices_seen"] = 100
    _arm_data_stall(so, "kismet")        # baseline value 100, old timestamp
    so._stats["kismet_devices_seen"] = 117   # counter advanced since baseline
    tripped = so._check_watchdog()
    assert so._sensor_health["kismet"] is True
    assert "kismet" not in tripped


def test_watchdog_data_stall_off_by_default_for_adsb(orch):
    # ADS-B is not in the data-sensor set, so an empty sky (flat counter) never
    # trips the data-stall path — an idle-but-live sensor is not flagged.
    so = orch.sensor_orchestrator
    so._modules_active["adsb"] = True
    so._sensor_health["adsb"] = True
    so._last_poll_ts["adsb"] = time.monotonic()
    assert "adsb" not in so._data_sensors
    so._last_progress_value["adsb"] = so._stats.get("aircraft_seen", 0)
    so._last_progress_ts["adsb"] = time.monotonic() - (so._data_stall_s + 600)
    tripped = so._check_watchdog()
    assert so._sensor_health["adsb"] is True
    assert "adsb" not in tripped


# ---------------------------------------------------------------------------
# Recovery escalation — reconnect, then self-restart (os._exit) with crash-guard
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handle_stall_reconnect_failure_self_restarts(orch, tmp_path):
    # A single stalled sensor whose reconnect fails escalates to os._exit(1).
    so = orch.sensor_orchestrator
    so._restart_log_path = tmp_path / "watchdog_restarts.json"
    so._reconnect = AsyncMock(return_value=False)
    so._console_alert = MagicMock()
    with patch("modules.orchestrator.os._exit") as mock_exit:
        await so._handle_stall(["kismet"])
    so._reconnect.assert_awaited_once_with("kismet")
    mock_exit.assert_called_once_with(1)


@pytest.mark.asyncio
async def test_handle_stall_reconnect_success_no_restart(orch):
    # A single stalled sensor that reconnects successfully does NOT restart.
    so = orch.sensor_orchestrator
    so._reconnect = AsyncMock(return_value=True)
    so._console_alert = MagicMock()
    with patch("modules.orchestrator.os._exit") as mock_exit:
        await so._handle_stall(["kismet"])
    mock_exit.assert_not_called()
    assert "kismet" in so._stalled_since_reconnect


@pytest.mark.asyncio
async def test_handle_stall_two_sensors_restarts_immediately(orch, tmp_path):
    # Two sensors stalled at once = process-wide wedge -> immediate restart, no
    # reconnect attempted.
    so = orch.sensor_orchestrator
    so._restart_log_path = tmp_path / "watchdog_restarts.json"
    so._reconnect = AsyncMock(return_value=True)
    so._console_alert = MagicMock()
    with patch("modules.orchestrator.os._exit") as mock_exit:
        await so._handle_stall(["kismet", "adsb"])
    mock_exit.assert_called_once_with(1)
    so._reconnect.assert_not_called()


@pytest.mark.asyncio
async def test_handle_stall_retrip_after_reconnect_restarts(orch, tmp_path):
    # A sensor that already reconnected this episode and stalls again escalates.
    so = orch.sensor_orchestrator
    so._restart_log_path = tmp_path / "watchdog_restarts.json"
    so._reconnect = AsyncMock(return_value=True)
    so._console_alert = MagicMock()
    so._stalled_since_reconnect.add("kismet")
    with patch("modules.orchestrator.os._exit") as mock_exit:
        await so._handle_stall(["kismet"])
    mock_exit.assert_called_once_with(1)
    so._reconnect.assert_not_called()


@pytest.mark.asyncio
async def test_one_reconnect_per_episode_after_recovery(orch, tmp_path):
    # An episode is: stall -> reconnect (flag set) -> recover (flag cleared) so a
    # later, unrelated stall earns its OWN reconnect instead of restarting.
    so = orch.sensor_orchestrator
    so._restart_log_path = tmp_path / "watchdog_restarts.json"
    so._reconnect = AsyncMock(return_value=True)
    so._console_alert = MagicMock()
    so._modules_active["kismet"] = True

    # Episode 1: stall -> reconnect succeeds -> sensor is flagged.
    with patch("modules.orchestrator.os._exit") as mock_exit:
        await so._handle_stall(["kismet"])
    mock_exit.assert_not_called()
    assert "kismet" in so._stalled_since_reconnect

    # Recovery: a watchdog pass where the sensor is healthy and its data is
    # progressing again clears the per-episode flag.
    so._sensor_health["kismet"] = True
    so._last_poll_ts["kismet"] = time.monotonic()        # loop is alive
    so._stats["kismet_devices_seen"] = 200
    so._last_progress_value["kismet"] = 199              # counter advanced
    so._last_progress_ts["kismet"] = time.monotonic()
    tripped = so._check_watchdog()
    assert "kismet" not in tripped
    assert "kismet" not in so._stalled_since_reconnect

    # Episode 2: a fresh, unrelated stall later gets its own reconnect attempt,
    # NOT an immediate self-restart.
    so._reconnect.reset_mock()
    with patch("modules.orchestrator.os._exit") as mock_exit:
        await so._handle_stall(["kismet"])
    mock_exit.assert_not_called()
    so._reconnect.assert_awaited_once_with("kismet")
    assert "kismet" in so._stalled_since_reconnect


def test_crash_guard_blocks_sixth_restart_in_window(orch, tmp_path):
    # The 6th self-restart within the window is suppressed (default limit 5);
    # the node logs CRITICAL and stays up instead of crash-looping.
    so = orch.sensor_orchestrator
    so._console_alert = MagicMock()
    so._restart_log_path = tmp_path / "watchdog_restarts.json"
    with patch("modules.orchestrator.os._exit") as mock_exit:
        for _ in range(5):
            so._self_restart("forced stall")
        assert mock_exit.call_count == 5     # first five exit
        mock_exit.reset_mock()
        so._self_restart("forced stall")     # sixth is suppressed
        mock_exit.assert_not_called()


def test_crash_guard_window_expiry_allows_restart(orch, tmp_path):
    # Restarts older than the window are not counted, so a fresh restart proceeds.
    so = orch.sensor_orchestrator
    so._console_alert = MagicMock()
    so._restart_log_path = tmp_path / "watchdog_restarts.json"
    old = time.time() - (so._restart_window_s + 100)
    so._restart_log_path.write_text(json.dumps([old] * 10), encoding="utf-8")
    with patch("modules.orchestrator.os._exit") as mock_exit:
        so._self_restart("forced stall")
        mock_exit.assert_called_once_with(1)


# ---------------------------------------------------------------------------
# systemd heartbeat (sd_notify WATCHDOG=1) — only while capture is progressing
# ---------------------------------------------------------------------------


def test_capture_progressing_true_when_advancing(orch):
    so = orch.sensor_orchestrator
    so._modules_active["kismet"] = True
    so._last_progress_ts["kismet"] = time.monotonic()   # just advanced
    assert so._capture_progressing() is True


def test_capture_progressing_false_when_frozen(orch):
    so = orch.sensor_orchestrator
    so._modules_active["kismet"] = True
    so._last_progress_ts["kismet"] = time.monotonic() - (so._data_stall_s + 60)
    assert so._capture_progressing() is False


def test_sd_notify_noop_without_socket(orch):
    # With no NOTIFY_SOCKET, sd_notify silently does nothing (dev/non-systemd).
    so = orch.sensor_orchestrator
    so._notify_socket = ""
    with patch("socket.socket") as mock_sock:
        so._sd_notify("WATCHDOG=1")
    mock_sock.assert_not_called()


def test_sd_notify_sends_when_socket_set(orch):
    # With NOTIFY_SOCKET set, sd_notify writes the datagram to that address.
    so = orch.sensor_orchestrator
    so._notify_socket = "/run/systemd/notify"
    mock_instance = MagicMock()
    with patch("socket.socket", return_value=mock_instance) as mock_sock:
        so._sd_notify("WATCHDOG=1")
    mock_sock.assert_called_once()
    mock_instance.sendto.assert_called_once()
    payload, addr = mock_instance.sendto.call_args[0]
    assert payload == b"WATCHDOG=1"
    assert addr == "/run/systemd/notify"


# ---------------------------------------------------------------------------
# GPS-stamping parity after decoupling the per-module gpsd read
#
# The sensor modules no longer read gpsd themselves; the orchestrator stamps
# from its own fresh _current_fix by passing it into the poll calls. These
# tests prove the fix the orchestrator holds reaches the modules and the
# resulting output records, so nothing that was GPS-stamped before is dropped.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_poll_adsb_passes_current_fix_to_module(orch):
    """_poll_adsb must hand its own _current_fix to poll_aircraft(gps_fix=...)."""
    so = orch.sensor_orchestrator
    fix = {"lat": 51.5, "lon": -0.1, "utc": "2024-01-15T12:00:00Z"}
    so._current_fix = fix
    orch.adsb.poll_aircraft = AsyncMock(return_value=[])
    orch._adsb_active = True

    await so._poll_adsb()

    orch.adsb.poll_aircraft.assert_awaited_once_with(gps_fix=fix)


@pytest.mark.asyncio
async def test_poll_adsb_event_carries_gps_stamp(orch):
    """An aircraft event must keep its gps_lat/gps_lon/gps_utc after decoupling.

    The module (here mocked) stamps from the passed fix exactly as the real
    ADSBModule does; the orchestrator must preserve those fields on the event
    it appends (and therefore on aircraft.jsonl / GeoJSON output).
    """
    so = orch.sensor_orchestrator
    fix = {"lat": 51.5, "lon": -0.1, "utc": "2024-01-15T12:00:00Z"}
    so._current_fix = fix

    async def _stamped_poll(gps_fix=None):
        ac = _make_aircraft(icao="STAMP1")
        ac.update({
            "gps_lat": gps_fix["lat"] if gps_fix else None,
            "gps_lon": gps_fix["lon"] if gps_fix else None,
            "gps_utc": gps_fix["utc"] if gps_fix else None,
        })
        return [ac]

    orch.adsb.poll_aircraft = AsyncMock(side_effect=_stamped_poll)
    orch._adsb_active = True

    await so._poll_adsb()

    assert len(so.aircraft_detections) == 1
    event = so.aircraft_detections[0]
    assert event["gps_lat"] == 51.5
    assert event["gps_lon"] == -0.1
    assert event["gps_utc"] == "2024-01-15T12:00:00Z"


@pytest.mark.asyncio
async def test_poll_kismet_passes_current_fix_to_module_and_stores(orch):
    """_poll_kismet must pass _current_fix to poll_devices, persistence and entity store."""
    so = orch.sensor_orchestrator
    fix = {"lat": 51.5, "lon": -0.1, "utc": "2024-01-15T12:00:00Z"}
    so._current_fix = fix
    orch.kismet.poll_devices = AsyncMock(return_value=[{"macaddr": "aa:bb:cc:dd:ee:ff"}])
    so.entity_store = MagicMock()
    orch._kismet_active = True

    await so._poll_kismet()

    orch.kismet.poll_devices.assert_awaited_once_with(gps_fix=fix)
    # The scorer and durable store still receive the same fresh fix.
    _, pe_kwargs = orch.persistence.update.call_args
    assert pe_kwargs.get("gps_fix") == fix
    _, es_kwargs = so.entity_store.record_poll.call_args
    assert es_kwargs.get("gps_fix") == fix


@pytest.mark.asyncio
async def test_poll_gps_uses_dedicated_executor(orch):
    """_poll_gps must dispatch get_fix on the dedicated single-thread GPS pool.

    Item 3 isolation: a wedged gpsd read can only starve this pool, never the
    default executor used by the stores/SDR/systemctl calls.
    """
    so = orch.sensor_orchestrator
    fix = {"lat": 1.0, "lon": 2.0, "utc": "2024-01-15T12:00:00Z"}
    orch.gps.get_fix = MagicMock(return_value=fix)

    await so._poll_gps()

    assert so._current_fix == fix
    # Single-worker dedicated pool exists and is distinct from the default.
    assert so._gps_executor._max_workers == 1


@pytest.mark.asyncio
async def test_poll_gps_timeout_is_handled_cleanly(orch):
    """A GPS executor dispatch that times out is treated as a degraded poll, not a crash."""
    so = orch.sensor_orchestrator

    async def _raise_timeout(_func):
        raise asyncio.TimeoutError()

    so._run_gps_call = _raise_timeout
    so._sensor_health["gps"] = True
    so._reconnect = AsyncMock(return_value=False)
    orch._gps_active = True

    # Should not raise; degrades health and continues.
    await so._poll_gps()
    assert so._sensor_health["gps"] is False


# ---------------------------------------------------------------------------
# _dispatch_alert() — alerts run OFF the event loop (soak-#3 cascade fix)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_alert_offloads_and_calls_backend(orch):
    """A dispatched alert eventually calls the backend and clears in-flight."""
    so = orch.sensor_orchestrator
    scheduled = so._dispatch_alert(orch._mock_backend.send_aircraft_alert, {"icao": "X"})
    assert scheduled is True
    await _drain_alerts(orch)
    orch._mock_backend.send_aircraft_alert.assert_called_once_with({"icao": "X"})
    # give the loop a tick for the done-callback to decrement the counter
    await asyncio.sleep(0)
    assert so._alerts_inflight == 0


@pytest.mark.asyncio
async def test_dispatch_alert_does_not_block_the_event_loop(orch):
    """A slow backend send must not block the dispatching coroutine.

    This is the whole point of the fix: a wedged backend (e.g. an unreachable
    ntfy topic) previously blocked the event loop and starved the watchdog
    heartbeat into a systemd-kill loop.
    """
    so = orch.sensor_orchestrator
    release = threading.Event()

    def slow_send(_payload):
        release.wait(2.0)
        return True

    orch._mock_backend.send_aircraft_alert.side_effect = slow_send

    t0 = time.monotonic()
    scheduled = so._dispatch_alert(orch._mock_backend.send_aircraft_alert, {"icao": "SLOW"})
    elapsed = time.monotonic() - t0

    assert scheduled is True
    assert elapsed < 0.5, "dispatch blocked on the slow send instead of offloading"

    release.set()
    await _drain_alerts(orch)


@pytest.mark.asyncio
async def test_dispatch_alert_drops_when_backlog_is_full(orch):
    """When too many sends are in flight, further alerts are dropped and counted,
    never queued unboundedly behind a wedged backend."""
    so = orch.sensor_orchestrator
    so._alert_max_inflight = 1
    release = threading.Event()

    def blocking_send(_payload):
        release.wait(2.0)
        return True

    orch._mock_backend.send_aircraft_alert.side_effect = blocking_send

    first = so._dispatch_alert(orch._mock_backend.send_aircraft_alert, {"icao": "1"})
    # worker thread is now blocked holding the single in-flight slot
    second = so._dispatch_alert(orch._mock_backend.send_aircraft_alert, {"icao": "2"})
    third = so._dispatch_alert(orch._mock_backend.send_aircraft_alert, {"icao": "3"})

    assert first is True
    assert second is False and third is False
    assert so._stats["alerts_dropped"] == 2

    release.set()
    await _drain_alerts(orch)


# ---------------------------------------------------------------------------
# BLE scanner integration (Phase 2 step 5) — advert buffering + device merge
# ---------------------------------------------------------------------------

def _advert(address, rssi=-60, company_ids=None, service_uuids=None,
            service_data_uuids=None, local_name="", appearance=None,
            directed=False, service_uuids_128=None, solicited_uuids=None,
            solicited_uuids_128=None, mfg_structures=None):
    from types import SimpleNamespace
    return SimpleNamespace(
        address=address, rssi=rssi,
        company_ids=company_ids or [], service_uuids=service_uuids or [],
        service_data_uuids=service_data_uuids or [], local_name=local_name,
        appearance=appearance,
        directed=directed, service_uuids_128=service_uuids_128 or [],
        solicited_uuids=solicited_uuids or [], solicited_uuids_128=solicited_uuids_128 or [],
        mfg_structures=mfg_structures or [],
    )


def test_ble_advert_buffered_as_btle_device(orch):
    so = orch.sensor_orchestrator
    so._on_ble_advert(_advert("c2:aa:bb:cc:dd:ee", rssi=-58,
                              service_uuids=[0x180D], local_name="Band"))
    devices = so._drain_ble_adverts()
    assert len(devices) == 1
    d = devices[0]
    assert d["type"] == "BTLE"
    assert d["last_signal"] == -58
    assert d["service_uuids"] == [0x180D]
    assert d["macaddr"] == "c2:aa:bb:cc:dd:ee"


def test_drain_clears_buffer(orch):
    so = orch.sensor_orchestrator
    so._on_ble_advert(_advert("c2:aa:bb:cc:dd:ee"))
    assert len(so._drain_ble_adverts()) == 1
    assert so._drain_ble_adverts() == []   # second drain is empty


def test_ble_advert_keeps_latest_per_address(orch):
    so = orch.sensor_orchestrator
    so._on_ble_advert(_advert("c2:11:11:11:11:11", rssi=-70))
    so._on_ble_advert(_advert("c2:11:11:11:11:11", rssi=-55))
    devices = so._drain_ble_adverts()
    assert len(devices) == 1
    assert devices[0]["last_signal"] == -55


def test_buffered_ble_device_is_fingerprintable(orch):
    # The converted device must key by ble-fp: through the unified scorer keying.
    from modules.fixed_scoring import FixedScoring
    so = orch.sensor_orchestrator
    so._on_ble_advert(_advert("c2:aa:bb:cc:dd:ee", service_uuids=[0x180D], local_name="Band"))
    device = so._drain_ble_adverts()[0]
    assert FixedScoring._device_key(device).startswith("ble-fp:")


# ---------------------------------------------------------------------------
# Enriched identity fields (PNL + reconnect) — capture/display only, not scoring
# ---------------------------------------------------------------------------

def test_enriched_identity_fields_wifi_pnl(orch):
    so = orch.sensor_orchestrator
    so.entity_store = MagicMock()
    so.entity_store.accumulated_pnl.return_value = ["Home", "Work"]
    fields = so._enriched_identity_fields({"probe_fingerprint": 777})
    assert set(fields["probe_ssids_all"]) == {"Home", "Work"}
    assert fields["fingerprint_pnl"].startswith("wifi-pnl:")
    assert fields["reconnect"] is False


def test_enriched_identity_fields_ble_reconnect(orch):
    so = orch.sensor_orchestrator
    so.entity_store = MagicMock()
    so.entity_store.accumulated_pnl.return_value = []
    fields = so._enriched_identity_fields(
        {"ble_directed": True, "solicited_uuids": [0xFD6F]})
    assert fields["reconnect"] is True
    assert "fd6f" in fields["solicited"]


def test_enriched_identity_fields_none_device(orch):
    so = orch.sensor_orchestrator
    fields = so._enriched_identity_fields(None)
    assert fields == {"probe_ssids_all": [], "fingerprint_pnl": "",
                      "reconnect": False, "solicited": [], "network_affinity": []}


# ---------------------------------------------------------------------------
# Graceful startup — radio preflight + degraded-sensor alerting
# ---------------------------------------------------------------------------

def test_startup_health_report_alerts_down_radio(orch):
    so = orch.sensor_orchestrator
    so._modules_active = {"kismet": True, "gps": True, "remote_id": True, "ble": False}
    so._record_alert = MagicMock()
    so._console_alert = MagicMock()
    so.startup_health_report({"kismet", "gps", "remote_id", "ble"})
    # exactly one down-alert, for the down radio (ble), none for the healthy ones
    assert so._record_alert.call_count == 1
    assert so._record_alert.call_args.kwargs["sensor"] == "ble"
    assert "ble" in so._radio_down_alerted


def test_startup_health_report_silent_when_all_up(orch):
    so = orch.sensor_orchestrator
    so._modules_active = {"kismet": True, "gps": True}
    so._record_alert = MagicMock()
    so._console_alert = MagicMock()
    so.startup_health_report({"kismet", "gps"})
    so._record_alert.assert_not_called()


def test_radio_health_debounced_then_recovers(orch):
    so = orch.sensor_orchestrator
    so._expected_radios = {"ble"}
    so._modules_active = {"ble": False}
    so._record_alert = MagicMock()
    so._console_alert = MagicMock()
    so._check_radio_health()              # down -> alert
    so._check_radio_health()              # still down -> debounced, no 2nd alert
    assert so._record_alert.call_count == 1
    so._modules_active["ble"] = True
    so._check_radio_health()              # recovered -> recovery alert, clears state
    assert so._record_alert.call_count == 2
    assert "ble" not in so._radio_down_alerted


# ---------------------------------------------------------------------------
# P6 — aircraft panel: live current sky (decay + index pruning)
# ---------------------------------------------------------------------------

def _ac(icao, age_s):
    from datetime import datetime, timezone, timedelta
    ts = (datetime.now(timezone.utc) - timedelta(seconds=age_s)).isoformat()
    return {"icao": icao, "timestamp": ts}


def test_current_aircraft_returns_fresh_only(orch):
    so = orch.sensor_orchestrator
    so._aircraft_retention_s = 120
    so._aircraft_index = {"AAA": _ac("AAA", 0), "BBB": _ac("BBB", 30), "CCC": _ac("CCC", 300)}
    icaos = {a["icao"] for a in so.current_aircraft()}
    assert icaos == {"AAA", "BBB"}   # CCC (300s) is stale, excluded


def test_current_aircraft_is_read_only(orch):
    # current_aircraft() must not mutate the index (pruning is the poll loop's job)
    so = orch.sensor_orchestrator
    so._aircraft_retention_s = 120
    so._aircraft_index = {"AAA": _ac("AAA", 0), "CCC": _ac("CCC", 300)}
    so.current_aircraft()
    assert set(so._aircraft_index) == {"AAA", "CCC"}


def test_prune_aircraft_index_removes_stale(orch):
    from datetime import datetime, timezone
    so = orch.sensor_orchestrator
    so._aircraft_retention_s = 120
    so._aircraft_index = {"AAA": _ac("AAA", 0), "CCC": _ac("CCC", 300)}
    so._prune_aircraft_index(datetime.now(timezone.utc))
    assert "AAA" in so._aircraft_index and "CCC" not in so._aircraft_index


def test_current_aircraft_missing_timestamp_treated_stale(orch):
    so = orch.sensor_orchestrator
    so._aircraft_index = {"AAA": {"icao": "AAA"}}  # no timestamp
    assert so.current_aircraft() == []


# ---------------------------------------------------------------------------
# DroneRF auto-disable reflected in live status (GUI chiclet accuracy)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_poll_drone_rf_marks_inactive_on_auto_disable(orch):
    so = orch.sensor_orchestrator
    so._modules_active["drone_rf"] = True
    so.drone_rf.auto_disabled = True
    so.drone_rf.drain_detections.return_value = []
    await so._poll_drone_rf()
    assert so._modules_active["drone_rf"] is False


@pytest.mark.asyncio
async def test_poll_drone_rf_leaves_active_when_running(orch):
    so = orch.sensor_orchestrator
    so._modules_active["drone_rf"] = True
    so.drone_rf.auto_disabled = False
    so.drone_rf.drain_detections.return_value = []
    await so._poll_drone_rf()
    assert so._modules_active["drone_rf"] is True


# ---------------------------------------------------------------------------
# Emergency aircraft paging — rate-limited on its own key
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_poll_adsb_emergency_repeat_is_rate_limited(orch):
    """An emergency held in view pages once per cooldown, not on every poll.

    (Pre-fix, the emergency path skipped the rate limiter entirely — a squawk
    overhead re-paged every 5 s poll for its whole stay.)"""
    em = _make_aircraft(emergency=True, icao="EMG002")
    orch.adsb.poll_aircraft = AsyncMock(return_value=[em])
    orch._adsb_active = True

    await orch.sensor_orchestrator._poll_adsb()
    await orch.sensor_orchestrator._poll_adsb()
    await _drain_alerts(orch)

    orch._mock_backend.send_aircraft_alert.assert_called_once()
    assert orch.sensor_orchestrator._stats["alerts_rate_limited"] >= 1


# ---------------------------------------------------------------------------
# Radio health — mid-run deaths that never flip _modules_active
# ---------------------------------------------------------------------------


def test_radio_health_alerts_adsb_dead_mid_run(orch):
    """A readsb/USB death mid-run only flips sensor health (the module stays
    optimistically active) — the radio-down alert must still fire."""
    so = orch.sensor_orchestrator
    so._expected_radios = {"adsb"}
    so._modules_active["adsb"] = True
    so._modules_active["sdr_coordinator"] = False
    so._sensor_health["adsb"] = False
    so._record_alert = MagicMock()
    so._console_alert = MagicMock()

    so._check_radio_health()
    assert so._record_alert.call_count == 1
    assert so._record_alert.call_args.kwargs["sensor"] == "adsb"

    so._sensor_health["adsb"] = True
    so._check_radio_health()              # recovered -> recovery alert
    assert so._record_alert.call_count == 2
    assert "adsb" not in so._radio_down_alerted


def test_radio_health_ignores_adsb_health_during_time_share(orch):
    """A scheduled time-share blackout (SDR coordinator running) stops readsb
    by design — degraded ADS-B poll health must not read as a dead radio."""
    so = orch.sensor_orchestrator
    so._expected_radios = {"adsb"}
    so._modules_active["adsb"] = True
    so._modules_active["sdr_coordinator"] = True
    so._sensor_health["adsb"] = False
    so._record_alert = MagicMock()
    so._console_alert = MagicMock()

    so._check_radio_health()
    so._record_alert.assert_not_called()


def test_radio_health_alerts_ble_controller_loss(orch):
    """A BLE controller drop mid-run flips only the scanner's `available`
    flag — the radio-down alert must fire from that signal."""
    so = orch.sensor_orchestrator
    so._expected_radios = {"ble"}
    so._modules_active["ble"] = True
    so.ble_scanner = MagicMock(available=False)
    so._record_alert = MagicMock()
    so._console_alert = MagicMock()

    so._check_radio_health()
    assert so._record_alert.call_count == 1
    assert so._record_alert.call_args.kwargs["sensor"] == "ble"

    so.ble_scanner.available = True
    so._check_radio_health()              # recovered
    assert "ble" not in so._radio_down_alerted


# ---------------------------------------------------------------------------
# _reconnect() / _handle_stall() — remote_id recovers, drone_rf escalates
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reconnect_remote_id_closes_and_connects(orch):
    so = orch.sensor_orchestrator
    so.remote_id.close = AsyncMock()
    so.remote_id.connect = AsyncMock()

    assert await so._reconnect("remote_id") is True
    so.remote_id.close.assert_awaited_once()
    so.remote_id.connect.assert_awaited_once()
    assert so._sensor_health["remote_id"] is True


@pytest.mark.asyncio
async def test_handle_stall_remote_id_reconnects_without_restart(orch):
    """A stalled Remote ID poll gets a real reconnect attempt before any
    escalation (pre-fix it fell through the unknown-module branch straight
    into a self-restart)."""
    so = orch.sensor_orchestrator
    so.remote_id.close = AsyncMock()
    so.remote_id.connect = AsyncMock()

    with patch.object(so, "_self_restart") as mock_restart:
        await so._handle_stall(["remote_id"])
    mock_restart.assert_not_called()
    assert "remote_id" in so._stalled_since_reconnect


@pytest.mark.asyncio
async def test_handle_stall_drone_rf_escalates_deliberately(orch):
    """DroneRF has no in-place reconnect path (subprocess/coordinator-managed)
    — a stall escalates to the crash-guarded self-restart by design."""
    so = orch.sensor_orchestrator
    with patch.object(so, "_self_restart") as mock_restart:
        await so._handle_stall(["drone_rf"])
    mock_restart.assert_called_once()


# ---------------------------------------------------------------------------
# In-place WiFi update refreshes score_breakdown
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_poll_kismet_inplace_update_refreshes_breakdown(orch):
    """A re-flagged device's ongoing detection must carry the CURRENT signal
    mix — the level-change line written to events.jsonl was shipping the first
    flag's stale breakdown."""
    so = orch.sensor_orchestrator
    orch._kismet_active = True
    orch.kismet.poll_devices = AsyncMock(return_value=[])
    first = _make_detection_event(score_breakdown={"novelty": 0.5},
                                  score=0.5, alert_level="suspicious")
    second = _make_detection_event(score_breakdown={"novelty": 0.5, "off_schedule": 0.3},
                                   score=0.8, alert_level="likely")

    orch.persistence.update.return_value = [first]
    await so._poll_kismet()
    orch.persistence.update.return_value = [second]
    await so._poll_kismet()

    ev = so._wifi_event_index[first.mac]
    assert ev["score_breakdown"] == {"novelty": 0.5, "off_schedule": 0.3}


# ---------------------------------------------------------------------------
# Nearby feed is mobile-only at the producer
# ---------------------------------------------------------------------------


def _make_nearby_device(mac="aa:bb:cc:dd:ee:01"):
    return {"macaddr": mac, "type": "Wi-Fi Client", "name": "", "manuf": "",
            "last_signal": -60, "mac_type": "static", "probe_ssids": []}


@pytest.mark.asyncio
async def test_nearby_feed_not_pushed_on_fixed_node(orch):
    """A fixed node keeps Kismet's full historical device list — pushing it as
    'nearby' every poll overflowed the SSE queues and evicted dashboard
    clients. Nothing on a fixed node consumes the feed, so it must not push."""
    so = orch.sensor_orchestrator
    so._node_mode = "fixed"
    so.gui_server = MagicMock()
    orch._kismet_active = True
    orch.kismet.poll_devices = AsyncMock(return_value=[_make_nearby_device()])
    orch.persistence.update.return_value = []

    await so._poll_kismet()
    pushed_types = [c.args[0] for c in so.gui_server.push_event.call_args_list]
    assert "nearby" not in pushed_types


@pytest.mark.asyncio
async def test_nearby_feed_pushed_on_mobile_node(orch):
    so = orch.sensor_orchestrator
    so._node_mode = "mobile"
    so.gui_server = MagicMock()
    orch._kismet_active = True
    orch.kismet.poll_devices = AsyncMock(return_value=[_make_nearby_device()])
    orch.persistence.update.return_value = []

    await so._poll_kismet()
    pushed_types = [c.args[0] for c in so.gui_server.push_event.call_args_list]
    assert "nearby" in pushed_types


# ---------------------------------------------------------------------------
# ACARS enrichment — position correlation, contact enrichment, persistence
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_poll_acars_correlates_by_position_when_no_identity_match(orch, tmp_path):
    """A positioned message whose tail/callsign match nothing binds to the nearest
    ADS-B contact within the match radius (the case a blank-callsign airframe needs)."""
    so = orch.sensor_orchestrator
    so._session_dir = Path(tmp_path) / "20260101_120000"
    so._session_dir.mkdir(parents=True, exist_ok=True)
    # Two contacts; the message sits ~5 km from FAR? no — near NEAR only.
    so._aircraft_index["near01"] = {"icao": "near01", "callsign": "", "lat": 51.50, "lon": -0.12}
    so._aircraft_index["far999"] = {"icao": "far999", "callsign": "", "lat": 40.00, "lon": -3.00}
    so.acars = MagicMock()
    so._modules_active["acars"] = True
    so.acars.drain_detections = MagicMock(return_value=[
        {"tail": None, "flight_id": None, "label": "10", "text": "POS",
         "lat": 51.52, "lon": -0.13, "timestamp": "2026-01-01T12:00:00+00:00"},
    ])
    await so._poll_acars()
    assert so._stats["acars_correlated"] == 1
    assert so._aircraft_index["near01"].get("acars")           # bound to the near one
    assert "acars" not in so._aircraft_index["far999"]


@pytest.mark.asyncio
async def test_poll_acars_position_no_match_outside_radius(orch, tmp_path):
    so = orch.sensor_orchestrator
    so._session_dir = Path(tmp_path) / "20260101_120000"
    so._session_dir.mkdir(parents=True, exist_ok=True)
    so._acars_position_match_km = 50.0
    so._aircraft_index["far999"] = {"icao": "far999", "callsign": "", "lat": 40.0, "lon": -3.0}
    so.acars = MagicMock()
    so._modules_active["acars"] = True
    so.acars.drain_detections = MagicMock(return_value=[
        {"tail": None, "flight_id": None, "text": "POS", "lat": 51.5, "lon": -0.1,
         "timestamp": "2026-01-01T12:00:00+00:00"},
    ])
    await so._poll_acars()
    assert so._stats["acars_correlated"] == 0
    assert "acars" not in so._aircraft_index["far999"]


@pytest.mark.asyncio
async def test_poll_acars_enriches_contact_with_route_and_flight(orch, tmp_path):
    so = orch.sensor_orchestrator
    so._session_dir = Path(tmp_path) / "20260101_120000"
    so._session_dir.mkdir(parents=True, exist_ok=True)
    so._aircraft_index["abc123"] = {"icao": "abc123", "callsign": "UAL123"}
    so.acars = MagicMock()
    so._modules_active["acars"] = True
    so.acars.drain_detections = MagicMock(return_value=[
        {"tail": None, "flight_id": "UAL123", "label": "H1", "text": "x",
         "origin": "KJFK", "destination": "KLAX", "lat": None, "lon": None,
         "timestamp": "2026-01-01T12:00:00+00:00"},
    ])
    await so._poll_acars()
    ev = so._aircraft_index["abc123"]
    assert ev["route"] == "KJFK→KLAX"
    assert ev["acars_flight"] == "UAL123"


@pytest.mark.asyncio
async def test_poll_acars_persists_enriched_contact_to_aircraft_jsonl(orch, tmp_path):
    """The enrichment must reach aircraft.jsonl so /api/aircraft rebuilds it after
    a refresh — otherwise the ACARS column is lost for any plane no longer overhead."""
    so = orch.sensor_orchestrator
    so._session_dir = Path(tmp_path) / "20260101_120000"
    so._session_dir.mkdir(parents=True, exist_ok=True)
    so._aircraft_index["abc123"] = {"icao": "abc123", "callsign": "UAL123"}
    so.acars = MagicMock()
    so._modules_active["acars"] = True
    so.acars.drain_detections = MagicMock(return_value=[
        {"tail": None, "flight_id": "UAL123", "label": "H1", "text": "OPS",
         "origin": "KJFK", "destination": "KLAX", "lat": None, "lon": None,
         "timestamp": "2026-01-01T12:00:00+00:00"},
    ])
    await so._poll_acars()
    lines = (so._session_dir / "aircraft.jsonl").read_text().strip().splitlines()
    recs = [json.loads(l) for l in lines]
    enriched = [r for r in recs if r.get("icao") == "abc123" and r.get("route") == "KJFK→KLAX"]
    assert enriched and enriched[-1]["acars"][-1]["text"] == "OPS"


@pytest.mark.asyncio
async def test_poll_acars_tail_still_wins_over_position(orch, tmp_path):
    """Identity match must take precedence over the position fallback."""
    so = orch.sensor_orchestrator
    so._session_dir = Path(tmp_path) / "20260101_120000"
    so._session_dir.mkdir(parents=True, exist_ok=True)
    so._aircraft_index["byreg"] = {"icao": "byreg", "registration": "N12345", "lat": 40.0, "lon": -3.0}
    so._aircraft_index["bypos"] = {"icao": "bypos", "callsign": "", "lat": 51.5, "lon": -0.1}
    so.acars = MagicMock()
    so._modules_active["acars"] = True
    so.acars.drain_detections = MagicMock(return_value=[
        {"tail": "N12345", "flight_id": None, "text": "POS", "lat": 51.5, "lon": -0.1,
         "timestamp": "2026-01-01T12:00:00+00:00"},
    ])
    await so._poll_acars()
    assert so._aircraft_index["byreg"].get("acars")       # tail match won
    assert "acars" not in so._aircraft_index["bypos"]


# ---------------------------------------------------------------------------
# Contact identity tiers + WiFi/BT return detection (Phase A fingerprinting)
# ---------------------------------------------------------------------------


def _wifi_event(mac, fingerprint="", device_type="Wi-Fi Client", ssid=""):
    return _make_detection_event(
        mac=mac, score=0.85, alert_level="likely",
        manufacturer="Acme", device_type=device_type, ssid=ssid,
        first_seen=datetime(2026, 1, 1, tzinfo=timezone.utc),
        last_seen=datetime(2026, 1, 1, tzinfo=timezone.utc),
        locations=[], observation_count=5,
    ) if False else DetectionEvent(
        mac=mac, score=0.85,
        score_breakdown={"novelty": 1.0},
        first_seen=datetime(2026, 1, 1, tzinfo=timezone.utc),
        last_seen=datetime(2026, 1, 1, tzinfo=timezone.utc),
        locations=[], observation_count=5, manufacturer="Acme",
        device_type=device_type, alert_level="likely", mac_type="randomized",
        ssid=ssid, fingerprint=fingerprint, fingerprint_label="Acme",
    )


def test_resolve_contact_uses_medium_tier_from_device(orch):
    so = orch.sensor_orchestrator
    ev = _wifi_event("a2:00:00:00:00:01", fingerprint="")   # scoring key is mac: (weak)
    device = {"macaddr": "a2:00:00:00:00:01", "type": "Wi-Fi Client",
              "probe_fingerprint": 4242, "fp_anchor_medium": "GymWiFi"}
    designator, confidence, identity_key = so._resolve_contact(ev, device)
    assert confidence == "medium"
    assert identity_key.startswith("wifi-fp:")   # NOT mac: — re-linkable


def test_resolve_contact_stable_across_mac_rotation(orch):
    """The whole point: two different MACs of one device resolve to the SAME contact
    identity (and, via the persisted number, the same designator)."""
    so = orch.sensor_orchestrator
    dev_a = {"macaddr": "a2:00:00:00:00:aa", "type": "Wi-Fi Client",
             "probe_fingerprint": 555, "fp_anchor_medium": "HomeX"}
    dev_b = {"macaddr": "a2:00:00:00:00:bb", "type": "Wi-Fi Client",
             "probe_fingerprint": 555, "fp_anchor_medium": "HomeX"}
    _, _, key_a = so._resolve_contact(_wifi_event("a2:00:00:00:00:aa"), dev_a)
    _, _, key_b = so._resolve_contact(_wifi_event("a2:00:00:00:00:bb"), dev_b)
    assert key_a == key_b


def test_resolve_contact_falls_back_to_mac_when_weak(orch):
    so = orch.sensor_orchestrator
    ev = _wifi_event("a2:00:00:00:00:01", fingerprint="")
    device = {"macaddr": "a2:00:00:00:00:01", "type": "Wi-Fi Client"}  # no anchor/IE
    _, confidence, identity_key = so._resolve_contact(ev, device)
    assert confidence == "weak"
    assert identity_key == "mac:a2:00:00:00:00:01"


def test_note_wifi_return_flags_after_gap(orch):
    so = orch.sensor_orchestrator
    so._wifi_return_gap_s = 900
    t0 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    r1, c1 = so._note_wifi_return("wifi-fp:abc", t0)
    assert r1 is False and c1 == 0                     # first sighting: not a return
    r2, c2 = so._note_wifi_return("wifi-fp:abc", t0 + timedelta(minutes=5))
    assert r2 is False                                 # still present: not a return
    r3, c3 = so._note_wifi_return("wifi-fp:abc", t0 + timedelta(minutes=30))
    assert r3 is True and c3 == 1                      # back after a 25-min gap


def test_note_wifi_return_never_claims_on_mac_identity(orch):
    so = orch.sensor_orchestrator
    so._wifi_return_gap_s = 900
    t0 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    so._note_wifi_return("mac:a2:00:00:00:00:01", t0)
    r, _ = so._note_wifi_return("mac:a2:00:00:00:00:01", t0 + timedelta(hours=2))
    assert r is False   # a rotating mac: identity can't be a trusted "same device"


@pytest.mark.asyncio
async def test_poll_kismet_marks_returning_contact_on_wifi_event(orch):
    so = orch.sensor_orchestrator
    orch._kismet_active = True
    so._wifi_return_gap_s = 900
    # The mocked entity store's distinctive_anchors returns a truthy MagicMock that
    # would pollute fp_anchor; drop it so the device's own medium anchor is used (a
    # real store returns a dict, so this only isolates the mock).
    so.entity_store = None
    # Seed the contact as last seen 30 min ago so this poll reads as a return.
    dev = {"macaddr": "a2:00:00:00:00:cc", "type": "Wi-Fi Client",
           "probe_fingerprint": 777, "fp_anchor_medium": "HomeY", "last_signal": -50}
    ev = _wifi_event("a2:00:00:00:00:cc")
    _, _, ck = so._resolve_contact(ev, dev)
    so._contact_last_seen[ck] = datetime.now(timezone.utc) - timedelta(minutes=30)

    orch.kismet.poll_devices = AsyncMock(return_value=[dev])
    orch.persistence.update.return_value = [ev]
    so.gui_server = MagicMock()
    await so._poll_kismet()

    row = so._wifi_event_index["a2:00:00:00:00:cc"]
    assert row["returning"] is True
    assert row["contact_confidence"] == "medium"
    assert row["contact_key"] == ck


# ---------------------------------------------------------------------------
# Cross-session returning entity (Phase B) — durable "seen before" recognition
# ---------------------------------------------------------------------------


def test_cross_session_return_flags_prior_session(orch):
    from modules.entity_store import EntityStore
    so = orch.sensor_orchestrator
    so.entity_store = EntityStore(":memory:")
    so.session_id = "sess-2"
    so._entity_return_min_gap_s = 3600
    # Seed a prior session's sighting a day ago.
    past = datetime.now(timezone.utc) - timedelta(days=1)
    so.entity_store.record_contact_sighting("wifi-fp:known1", past, "sess-1")

    res = so._note_cross_session_return("wifi-fp:known1", "strong", datetime.now(timezone.utc))
    assert res["returning_entity"] is True
    assert res["entity_visits"] == 2
    assert res["entity_days_known"] >= 1
    so.entity_store.close()


def test_cross_session_first_ever_sighting_not_returning(orch):
    from modules.entity_store import EntityStore
    so = orch.sensor_orchestrator
    so.entity_store = EntityStore(":memory:")
    so.session_id = "sess-1"
    res = so._note_cross_session_return("wifi-fp:brandnew", "strong", datetime.now(timezone.utc))
    assert res["returning_entity"] is False
    # But it IS now recorded for next time.
    assert so.entity_store.contact_registry_row("wifi-fp:brandnew") is not None
    so.entity_store.close()


def test_cross_session_quick_restart_not_a_return(orch):
    """A prior sighting only minutes ago (a crash-restart, new session id) must NOT
    read as a return visit — the min-gap guard, unless it's a different UTC day."""
    from modules.entity_store import EntityStore
    so = orch.sensor_orchestrator
    so.entity_store = EntityStore(":memory:")
    so.session_id = "sess-2"
    so._entity_return_min_gap_s = 3600
    now = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
    so.entity_store.record_contact_sighting(
        "wifi-fp:recent", now - timedelta(minutes=3), "sess-1")
    res = so._note_cross_session_return("wifi-fp:recent", "strong", now)
    assert res["returning_entity"] is False   # 3 min ago, same day -> not a return
    so.entity_store.close()


def test_cross_session_ignores_mac_identity(orch):
    from modules.entity_store import EntityStore
    so = orch.sensor_orchestrator
    so.entity_store = EntityStore(":memory:")
    so.session_id = "sess-2"
    res = so._note_cross_session_return("mac:a2:00:00:00:00:01", "weak", datetime.now(timezone.utc))
    assert res["returning_entity"] is False
    assert so.entity_store.count("contact_registry") == 0   # mac: never recorded
    so.entity_store.close()


def test_cross_session_result_cached_per_session(orch):
    from modules.entity_store import EntityStore
    so = orch.sensor_orchestrator
    so.entity_store = EntityStore(":memory:")
    so.session_id = "sess-2"
    so._entity_return_min_gap_s = 3600
    past = datetime.now(timezone.utc) - timedelta(days=1)
    so.entity_store.record_contact_sighting("wifi-fp:k", past, "sess-1")
    r1 = so._note_cross_session_return("wifi-fp:k", "strong", datetime.now(timezone.utc))
    r2 = so._note_cross_session_return("wifi-fp:k", "strong", datetime.now(timezone.utc))
    assert r1 == r2 and r1["returning_entity"] is True
    # Recorded once this session (the registry advanced a single visit, not two).
    assert so.entity_store.contact_registry_row("wifi-fp:k")["visits"] == 2
    so.entity_store.close()


def test_resolve_contact_mac_scoring_fingerprint_is_weak_not_strong(orch):
    """Regression (caught on chase): FixedScoring sets event.fingerprint to
    'mac:<mac>' for un-fingerprinted devices; that is NOT a strong content
    fingerprint, so the contact must be labeled weak and mac:-keyed, not strong."""
    so = orch.sensor_orchestrator
    ev = _wifi_event("a2:00:00:00:00:07", fingerprint="mac:a2:00:00:00:00:07")
    device = {"macaddr": "a2:00:00:00:00:07", "type": "Wi-Fi Client"}  # no anchor/IE
    _, confidence, identity_key = so._resolve_contact(ev, device)
    assert confidence == "weak"
    assert identity_key.startswith("mac:")


def test_resolve_contact_real_fingerprint_still_strong(orch):
    so = orch.sensor_orchestrator
    ev = _wifi_event("a2:00:00:00:00:08", fingerprint="wifi-fp:deadbeef")
    _, confidence, identity_key = so._resolve_contact(ev, device=None)
    assert confidence == "strong"
    assert identity_key == "wifi-fp:deadbeef"


# ---------------------------------------------------------------------------
# Cross-PHY co-presence + resident/visitor (Phase C)
# ---------------------------------------------------------------------------


def test_is_mobile_contact_excludes_aps_and_bridges(orch):
    so = orch.sensor_orchestrator
    assert so._is_mobile_contact("Wi-Fi Client") is True
    assert so._is_mobile_contact("BTLE") is True
    assert so._is_mobile_contact("Wi-Fi AP") is False
    assert so._is_mobile_contact("Wi-Fi Bridged") is False
    assert so._is_mobile_contact("Wi-Fi WDS AP") is False


def test_resident_status_classification(orch):
    so = orch.sensor_orchestrator
    # Non-mobile (AP/bridge) is the environment — never resident/visitor.
    assert so._resident_status(False, {"network_affinity": []}, {"novelty": 1.0}) == ""
    # Mobile, probes a locally-beaconed network -> resident.
    assert so._resident_status(True, {"network_affinity": ["HomeWiFi"]}, {}) == "resident"
    # Mobile, novel with no local affinity -> visitor.
    assert so._resident_status(True, {"network_affinity": []}, {"novelty": 1.0}) == "visitor"
    # Mobile, a known baseline device (novelty off) -> resident.
    assert so._resident_status(True, {"network_affinity": []}, {"novelty": 0.0, "off_schedule": 1.0}) == "resident"
    # Mobile, no baseline signal at all -> unknown.
    assert so._resident_status(True, {"network_affinity": []}, {}) == "unknown"


@pytest.mark.asyncio
async def test_poll_kismet_person_links_mobile_wifi_and_ble(orch):
    """A Wi-Fi client and a BLE device that are present together every poll link
    into one person; the AP present alongside them does NOT join."""
    from modules.entity_store import EntityStore
    so = orch.sensor_orchestrator
    so.entity_store = EntityStore(":memory:")
    # Fresh conservative linker with a low bar for the test.
    from modules.copresence import CoPresenceLinker
    so._copresence_linker = CoPresenceLinker(min_polls=3, min_jaccard=0.6,
                                             min_obs_polls=2, min_fixture_polls=3)
    orch._kismet_active = True

    phone_wifi = {"macaddr": "a2:00:00:00:00:11", "type": "Wi-Fi Client",
                  "probe_fingerprint": 111, "fp_anchor": "MyHouse", "last_signal": -40}
    phone_ble = {"macaddr": "c2:00:00:00:00:22", "type": "BTLE",
                 "service_uuids": [0x180D], "name": "MyPhone", "last_signal": -45}
    ap = {"macaddr": "d2:00:00:00:00:33", "type": "Wi-Fi AP",
          "probe_fingerprint": 999, "fp_anchor": "NeighborAP", "last_signal": -60}
    devices = [phone_wifi, phone_ble, ap]

    def ev(mac, fp):
        return DetectionEvent(
            mac=mac, score=0.85, score_breakdown={"novelty": 1.0},
            first_seen=datetime(2026, 1, 1, tzinfo=timezone.utc),
            last_seen=datetime(2026, 1, 1, tzinfo=timezone.utc),
            locations=[], observation_count=5, manufacturer="Acme",
            device_type=("BTLE" if mac.startswith("c2") else "Wi-Fi Client" if mac.startswith("a2") else "Wi-Fi AP"),
            alert_level="likely", mac_type="randomized", ssid="", fingerprint=fp,
            fingerprint_label="x")

    from modules import device_identity
    # resolve the stable contact keys the linker will see
    wifi_key = device_identity.contact_identity(phone_wifi)[0]
    ble_key = device_identity.contact_identity(phone_ble)[0]
    orch.kismet.poll_devices = AsyncMock(return_value=devices)
    orch.persistence.update.return_value = [
        ev("a2:00:00:00:00:11", "wifi-fp:x"), ev("c2:00:00:00:00:22", ""),
        ev("d2:00:00:00:00:33", "wifi-fp:y")]
    for _ in range(5):
        await so._poll_kismet()

    # The wiring: the linker is fed the MOBILE contacts (Wi-Fi client + BLE) but NOT
    # the AP — APs are the environment, never person-linked. (Cluster formation itself
    # is covered in test_copresence.py; a short all-present run makes them fixtures.)
    lk = so._copresence_linker
    ap_key = device_identity.contact_identity(ap)[0]
    assert wifi_key in lk._present_count and ble_key in lk._present_count
    assert ap_key not in lk._present_count
    so.entity_store.close()


def test_copresence_noop_when_linker_disabled(orch):
    so = orch.sensor_orchestrator
    so._copresence_linker = None          # disabled (CROSS_PHY_LINKING_ENABLED=false)
    so._update_copresence({"wifi-fp:a", "ble-fp:b"}, datetime.now(timezone.utc))
    assert so._person_of == {}            # no clustering when disabled
