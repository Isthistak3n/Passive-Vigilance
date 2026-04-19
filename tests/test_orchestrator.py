"""Tests for the PassiveVigilance asyncio orchestrator (main.py).

All module connections are mocked — no real hardware, network, or filesystem
access required.
"""

import asyncio
import json
import os
from datetime import datetime, timezone
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


@pytest.fixture()
def orch(tmp_path):
    """PassiveVigilance instance with all modules mocked and output in tmp_path."""
    env_patch = {
        "SESSION_OUTPUT_DIR": str(tmp_path),
        # Speed up GPS startup wait loop — no real device available in tests
        "GPS_STARTUP_TIMEOUT_SECONDS": "0",
    }

    with (
        patch("main.GPSModule") as mock_gps_cls,
        patch("main.IgnoreList") as mock_il_cls,
        patch("main.KismetModule") as mock_kis_cls,
        patch("main.ADSBModule") as mock_adsb_cls,
        patch("main.DroneRFModule") as mock_drone_cls,
        patch("main.PersistenceEngine") as mock_pe_cls,
        patch("main.ProbeAnalyzer") as mock_pa_cls,
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
    assert orch.all_events == []
    assert orch.aircraft_detections == []
    assert orch.drone_detections == []


# ---------------------------------------------------------------------------
# startup()
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_startup_all_modules_available(orch):
    await orch.startup()
    assert orch._gps_active is True
    assert orch._kismet_active is True
    assert orch._adsb_active is True


@pytest.mark.asyncio
async def test_startup_graceful_when_gps_unavailable(orch):
    orch.gps.connect.side_effect = ConnectionError("gpsd not running")
    await orch.startup()
    assert orch._gps_active is False
    # Other modules should still connect
    assert orch._kismet_active is True
    assert orch._adsb_active is True


@pytest.mark.asyncio
async def test_startup_graceful_when_kismet_unavailable(orch):
    orch.kismet.connect.side_effect = ConnectionError("Kismet not running")
    await orch.startup()
    assert orch._kismet_active is False
    assert orch._gps_active is True
    assert orch._adsb_active is True


@pytest.mark.asyncio
async def test_startup_graceful_when_readsb_unavailable(orch):
    orch.adsb.connect.side_effect = ConnectionError("readsb not running")
    await orch.startup()
    assert orch._adsb_active is False
    assert orch._gps_active is True
    assert orch._kismet_active is True


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
    orch.rate_limiter.is_allowed("aircraft:EMG001")  # consumes the slot

    await orch._poll_adsb()
    # Backend must still be called
    orch._mock_backend.send_aircraft_alert.assert_called_once()


@pytest.mark.asyncio
async def test_poll_adsb_rate_limiter_suppresses_repeat_normal_alert(orch):
    """A normal (non-emergency) aircraft should be rate-limited after first alert."""
    normal_ac = _make_aircraft(emergency=False, icao="NRM001")
    orch.adsb.poll_aircraft = AsyncMock(return_value=[normal_ac])
    orch._adsb_active = True

    await orch._poll_adsb()   # first poll — alert fires
    await orch._poll_adsb()   # second poll — rate-limited, no second alert

    orch._mock_backend.send_aircraft_alert.assert_called_once()


# ---------------------------------------------------------------------------
# _poll_kismet()
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_poll_kismet_passes_devices_through_ignore_list(orch):
    """poll_kismet must call kismet.poll_devices() which applies the ignore list."""
    orch._kismet_active = True
    orch.kismet.poll_devices = AsyncMock(return_value=[])

    await orch._poll_kismet()

    orch.kismet.poll_devices.assert_called_once()


@pytest.mark.asyncio
async def test_poll_kismet_sends_alert_for_high_score_event(orch):
    """A DetectionEvent above threshold triggers a persistence alert."""
    event = _make_detection_event(alert_level="high", score=0.95)
    orch.persistence.update.return_value = [event]
    orch.kismet.poll_devices = AsyncMock(return_value=[{"macaddr": "aa:bb:cc:dd:ee:ff"}])
    orch._kismet_active = True

    await orch._poll_kismet()

    orch._mock_backend.send_persistence_alert.assert_called_once_with(event)
    assert len(orch.all_events) == 1
    assert orch.all_events[0]["mac"] == "aa:bb:cc:dd:ee:ff"


# ---------------------------------------------------------------------------
# shutdown()
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_shutdown_writes_session_summary(orch, tmp_path):
    orch.session_id = "20260101_120000"
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
    orch.all_events = [
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

    await orch._poll_kismet()

    orch.persistence.update.assert_called_once_with([device], gps_fix=orch._current_fix)



@pytest.mark.asyncio
async def test_poll_kismet_appends_events_to_jsonl(orch, tmp_path):
    """Detection events above threshold must be appended to events.jsonl."""
    event = _make_detection_event(alert_level="high", score=0.95)
    orch.persistence.update.return_value = [event]
    orch.kismet.poll_devices = AsyncMock(return_value=[{"macaddr": "aa:bb:cc:dd:ee:ff"}])
    orch._kismet_active = True
    orch.session_id = "20260101_120000"
    orch._session_dir = Path(tmp_path) / "20260101_120000"

    await orch._poll_kismet()

    jsonl_path = orch._session_dir / "events.jsonl"
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
    orch.session_id = "20260101_120000"
    orch._session_dir = Path(tmp_path) / "20260101_120000"

    await orch._poll_adsb()

    jsonl_path = orch._session_dir / "aircraft.jsonl"
    assert jsonl_path.exists(), "aircraft.jsonl was not created"
    line = json.loads(jsonl_path.read_text().strip())
    assert line["icao"] == "TEST01"


# ---------------------------------------------------------------------------
# _log_health_banner()
# ---------------------------------------------------------------------------


def test_health_banner_logs_at_info(orch, caplog):
    """_log_health_banner() emits at least one INFO-level log line."""
    import logging
    with caplog.at_level(logging.INFO):
        orch._log_health_banner()
    assert any(r.levelno == logging.INFO for r in caplog.records)


def test_health_banner_includes_session_id(orch, caplog):
    """Health banner must contain the session ID."""
    import logging
    orch.session_id = "20260101_120000"
    with caplog.at_level(logging.INFO):
        orch._log_health_banner()
    all_msgs = " ".join(r.message for r in caplog.records)
    assert "20260101_120000" in all_msgs


def test_health_banner_reflects_sensor_degradation(orch, caplog):
    """Health banner must show Degraded when a sensor is unhealthy."""
    import logging
    orch._sensor_health["kismet"] = False
    with caplog.at_level(logging.INFO):
        orch._log_health_banner()
    all_msgs = " ".join(r.message for r in caplog.records)
    assert "Degraded" in all_msgs


# ---------------------------------------------------------------------------
# _reconnect()
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reconnect_returns_true_on_success(orch):
    """_reconnect() returns True and sets sensor health when reconnect succeeds."""
    orch._sensor_health["kismet"] = False
    orch.kismet.close = AsyncMock()
    orch.kismet.connect = AsyncMock()
    with patch("asyncio.sleep", new_callable=AsyncMock):
        result = await orch._reconnect("kismet")
    assert result is True
    assert orch._sensor_health["kismet"] is True


@pytest.mark.asyncio
async def test_reconnect_returns_false_after_max_attempts(orch):
    """_reconnect() returns False when all reconnect attempts fail."""
    orch._max_reconnect_attempts = 3
    orch.kismet.close = AsyncMock()
    orch.kismet.connect = AsyncMock(side_effect=ConnectionError("still down"))
    with patch("asyncio.sleep", new_callable=AsyncMock):
        result = await orch._reconnect("kismet")
    assert result is False


@pytest.mark.asyncio
async def test_reconnect_logs_warning_on_each_attempt(orch, caplog):
    """_reconnect() must log a WARNING for each attempt."""
    import logging
    orch._max_reconnect_attempts = 2
    orch.kismet.close = AsyncMock()
    orch.kismet.connect = AsyncMock(side_effect=ConnectionError("down"))
    with patch("asyncio.sleep", new_callable=AsyncMock):
        with caplog.at_level(logging.WARNING):
            await orch._reconnect("kismet")
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) >= 2


@pytest.mark.asyncio
async def test_reconnect_logs_error_when_all_attempts_fail(orch, caplog):
    """_reconnect() must log ERROR after exhausting all attempts."""
    import logging
    orch._max_reconnect_attempts = 1
    orch.kismet.close = AsyncMock()
    orch.kismet.connect = AsyncMock(side_effect=ConnectionError("down"))
    with patch("asyncio.sleep", new_callable=AsyncMock):
        with caplog.at_level(logging.ERROR):
            await orch._reconnect("kismet")
    assert any(r.levelno == logging.ERROR for r in caplog.records)


# ---------------------------------------------------------------------------
# Poll loop reconnect triggering
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_poll_kismet_triggers_reconnect_on_health_transition(orch):
    """_poll_kismet() calls _reconnect() when sensor first degrades."""
    orch._kismet_active = True
    orch._sensor_health["kismet"] = True
    orch.kismet.poll_devices = AsyncMock(side_effect=ConnectionError("down"))

    with patch.object(orch, "_reconnect", new_callable=AsyncMock, return_value=True) as mock_rc:
        await orch._poll_kismet()

    mock_rc.assert_called_once_with("kismet")


@pytest.mark.asyncio
async def test_poll_kismet_does_not_reconnect_on_repeated_failure(orch):
    """_poll_kismet() does NOT call _reconnect() when sensor is already degraded."""
    orch._kismet_active = True
    orch._sensor_health["kismet"] = False  # already degraded
    orch.kismet.poll_devices = AsyncMock(side_effect=ConnectionError("still down"))

    with patch.object(orch, "_reconnect", new_callable=AsyncMock) as mock_rc:
        await orch._poll_kismet()

    mock_rc.assert_not_called()


# ---------------------------------------------------------------------------
# shutdown() — field hardening: guaranteed writes even on partial failure
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_shutdown_writes_summary_even_when_shapefile_fails(orch, tmp_path):
    """summary.json must be written even if ShapefileWriter raises."""
    orch.session_id = "20260101_120000"
    orch._session_dir = Path(tmp_path) / "20260101_120000"
    orch.all_events = [
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
    orch._session_dir = Path(tmp_path) / "20260101_120000"
    orch.all_events = [
        {"event_type": "wifi", "mac": "aa:bb:cc:dd:ee:ff", "lat": 51.5, "lon": -0.1}
    ]
    orch.aircraft_detections = [
        {"event_type": "aircraft", "icao": "ABC123", "lat": 51.5, "lon": -0.1}
    ]
    orch.drone_detections = []

    with patch.dict("sys.modules", {"geopandas": None}):
        orch._emergency_flush()

    dump_path = tmp_path / "20260101_120000" / "emergency_dump.jsonl"
    assert dump_path.exists(), "emergency_dump.jsonl must be created"
    lines = [json.loads(line) for line in dump_path.read_text().strip().splitlines()]
    assert len(lines) == 2
    assert any(line["event_type"] == "wifi" for line in lines)
    assert any(line["event_type"] == "aircraft" for line in lines)
