"""Tests for the alert engine — AlertBackend, RateLimiter, NtfyBackend, AlertFactory."""

import os
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Clear any real credentials that might leak from .env before importing the module.
for _key in (
    "NTFY_TOPIC",
    "NTFY_SERVER",
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_CHAT_ID",
    "DISCORD_WEBHOOK_URL",
    "ALERT_BACKEND",
    "DRONE_ALERT_COOLDOWN_SECONDS",
    "PERSISTENCE_ALERT_COOLDOWN_SECONDS",
    "AIRCRAFT_ALERT_COOLDOWN_SECONDS",
):
    os.environ.pop(_key, None)

from modules.alerts import (  # noqa: E402  (import after env cleanup)
    AlertFactory,
    ConsoleBackend,
    DiscordBackend,
    NtfyBackend,
    RateLimiter,
    TelegramBackend,
)
from modules.persistence import DetectionEvent  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_event(**overrides) -> DetectionEvent:
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


def _make_drone(**overrides) -> dict:
    d = {"freq_mhz": 915.0, "power_db": -18.5, "lat": 51.5074, "lon": -0.1278}
    d.update(overrides)
    return d


def _make_aircraft(**overrides) -> dict:
    a = {
        "icao": "abc123",
        "callsign": "BAW123",
        "registration": "G-EUPT",
        "operator": "British Airways",
        "country": "UK",
        "altitude": 35000,
        "emergency": False,
    }
    a.update(overrides)
    return a


# ---------------------------------------------------------------------------
# RateLimiter tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rate_limiter_allows_first_call():
    rl = RateLimiter(cooldown_seconds=300)
    assert await rl.is_allowed("drone:915mhz") is True


@pytest.mark.asyncio
async def test_rate_limiter_blocks_within_cooldown():
    rl = RateLimiter(cooldown_seconds=300)
    await rl.is_allowed("drone:915mhz")  # first call records time
    assert await rl.is_allowed("drone:915mhz") is False


@pytest.mark.asyncio
async def test_rate_limiter_allows_after_cooldown_expires():
    rl = RateLimiter(cooldown_seconds=300)
    with patch("modules.alerts.time.monotonic") as mock_time:
        mock_time.return_value = 0.0
        await rl.is_allowed("key")
        mock_time.return_value = 301.0
        assert await rl.is_allowed("key") is True


@pytest.mark.asyncio
async def test_rate_limiter_reset_allows_immediate_realert():
    rl = RateLimiter(cooldown_seconds=300)
    await rl.is_allowed("mac:aa:bb:cc:dd:ee:ff")
    assert await rl.is_allowed("mac:aa:bb:cc:dd:ee:ff") is False
    await rl.reset("mac:aa:bb:cc:dd:ee:ff")
    assert await rl.is_allowed("mac:aa:bb:cc:dd:ee:ff") is True


@pytest.mark.asyncio
async def test_rate_limiter_persists_state_to_json(tmp_path):
    """is_allowed() must write a JSON file when persist_path is set."""
    persist_file = str(tmp_path / "rate_limits.json")
    rl = RateLimiter(cooldown_seconds=300, persist_path=persist_file)
    await rl.is_allowed("drone:915mhz")

    import json as _json
    data = _json.loads(Path(persist_file).read_text())
    assert "drone:915mhz" in data
    # Value must be a parseable ISO datetime string
    from datetime import datetime, timezone
    dt = datetime.fromisoformat(data["drone:915mhz"])
    assert dt.tzinfo is not None


@pytest.mark.asyncio
async def test_rate_limiter_loads_state_from_json(tmp_path):
    """A newly created RateLimiter with persist_path must honour state written by a previous instance."""
    import json as _json
    from datetime import datetime, timedelta, timezone

    persist_file = str(tmp_path / "rate_limits.json")
    # Write a state that recorded an alert 10 seconds ago (still within 300 s cooldown)
    recent_ts = (datetime.now(timezone.utc) - timedelta(seconds=10)).isoformat()
    Path(persist_file).write_text(_json.dumps({"drone:915mhz": recent_ts}))

    rl = RateLimiter(cooldown_seconds=300, persist_path=persist_file)
    # The key should still be blocked because only 10 s have elapsed
    assert await rl.is_allowed("drone:915mhz") is False


# ---------------------------------------------------------------------------
# NtfyBackend — is_configured
# ---------------------------------------------------------------------------


def test_ntfy_not_configured_without_topic():
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("NTFY_TOPIC", None)
        backend = NtfyBackend()
        assert backend.is_configured() is False


def test_ntfy_configured_with_topic():
    with patch.dict(os.environ, {"NTFY_TOPIC": "my-secret-topic"}):
        backend = NtfyBackend()
        assert backend.is_configured() is True


# ---------------------------------------------------------------------------
# NtfyBackend — send()
# ---------------------------------------------------------------------------


def test_ntfy_send_calls_correct_url():
    with patch.dict(os.environ, {"NTFY_TOPIC": "test-topic", "NTFY_SERVER": "https://ntfy.sh"}):
        backend = NtfyBackend()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status.return_value = None
        with patch("modules.alerts.requests.post", return_value=mock_resp) as mock_post:
            result = backend.send("Title", "Body", priority="high", tags=["tag1"])
        assert result is True
        mock_post.assert_called_once()
        call_args = mock_post.call_args
        assert call_args[0][0] == "https://ntfy.sh/test-topic"
        assert call_args[1]["headers"]["Title"] == "Title"
        assert call_args[1]["headers"]["Priority"] == "high"
        assert call_args[1]["headers"]["Tags"] == "tag1"


def test_ntfy_send_returns_false_on_http_error():
    with patch.dict(os.environ, {"NTFY_TOPIC": "test-topic"}):
        backend = NtfyBackend()
        import requests as req_lib
        mock_resp = MagicMock()
        mock_resp.status_code = 403
        mock_resp.raise_for_status.side_effect = req_lib.HTTPError("403")
        with patch("modules.alerts.requests.post", return_value=mock_resp):
            result = backend.send("Title", "Body")
        assert result is False


def test_ntfy_send_returns_false_when_unconfigured():
    os.environ.pop("NTFY_TOPIC", None)
    backend = NtfyBackend()
    with patch("modules.alerts.requests.post") as mock_post:
        result = backend.send("Title", "Body")
    assert result is False
    mock_post.assert_not_called()


# ---------------------------------------------------------------------------
# NtfyBackend — send_drone_alert()
# ---------------------------------------------------------------------------


def test_ntfy_send_drone_alert_formats_message():
    with patch.dict(os.environ, {"NTFY_TOPIC": "test-topic"}):
        backend = NtfyBackend()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status.return_value = None
        with patch("modules.alerts.requests.post", return_value=mock_resp) as mock_post:
            backend.send_drone_alert(_make_drone())
        body_sent = mock_post.call_args[1]["data"].decode("utf-8")
        assert "915" in body_sent
        assert "dBm" in body_sent
        assert "51.5074" in body_sent
        headers = mock_post.call_args[1]["headers"]
        assert headers["Title"] == "Drone RF Detected"
        assert headers["Priority"] == "high"
        assert "drone" in headers["Tags"]


# ---------------------------------------------------------------------------
# NtfyBackend — send_persistence_alert()
# ---------------------------------------------------------------------------


def test_ntfy_send_persistence_alert_formats_message():
    with patch.dict(os.environ, {"NTFY_TOPIC": "test-topic"}):
        backend = NtfyBackend()
        event = _make_event()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status.return_value = None
        with patch("modules.alerts.requests.post", return_value=mock_resp) as mock_post:
            backend.send_persistence_alert(event)
        body_sent = mock_post.call_args[1]["data"].decode("utf-8")
        assert "aa:bb:cc:dd:ee:ff" in body_sent
        assert "0.85" in body_sent
        assert "10 times" in body_sent
        headers = mock_post.call_args[1]["headers"]
        assert "LIKELY" in headers["Title"]
        assert "surveillance" in headers["Tags"]


# ---------------------------------------------------------------------------
# NtfyBackend — send_aircraft_alert()
# ---------------------------------------------------------------------------


def test_ntfy_send_aircraft_alert_includes_emergency_flag():
    with patch.dict(os.environ, {"NTFY_TOPIC": "test-topic"}):
        backend = NtfyBackend()
        aircraft = _make_aircraft(emergency=True)
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status.return_value = None
        with patch("modules.alerts.requests.post", return_value=mock_resp) as mock_post:
            backend.send_aircraft_alert(aircraft)
        body_sent = mock_post.call_args[1]["data"].decode("utf-8")
        assert "Emergency: True" in body_sent
        assert "abc123" in body_sent
        headers = mock_post.call_args[1]["headers"]
        assert headers["Priority"] == "max"  # urgent maps to max in ntfy


def test_ntfy_send_aircraft_alert_non_emergency_is_default_priority():
    with patch.dict(os.environ, {"NTFY_TOPIC": "test-topic"}):
        backend = NtfyBackend()
        aircraft = _make_aircraft(emergency=False)
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status.return_value = None
        with patch("modules.alerts.requests.post", return_value=mock_resp) as mock_post:
            backend.send_aircraft_alert(aircraft)
        headers = mock_post.call_args[1]["headers"]
        assert headers["Priority"] == "default"


# ---------------------------------------------------------------------------
# TelegramBackend — is_configured
# ---------------------------------------------------------------------------


def test_telegram_not_configured_without_tokens():
    os.environ.pop("TELEGRAM_BOT_TOKEN", None)
    os.environ.pop("TELEGRAM_CHAT_ID", None)
    backend = TelegramBackend()
    assert backend.is_configured() is False


def test_telegram_not_configured_with_only_token():
    with patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "123:abc"}):
        os.environ.pop("TELEGRAM_CHAT_ID", None)
        backend = TelegramBackend()
        assert backend.is_configured() is False


# ---------------------------------------------------------------------------
# DiscordBackend — is_configured
# ---------------------------------------------------------------------------


def test_discord_not_configured_without_url():
    os.environ.pop("DISCORD_WEBHOOK_URL", None)
    backend = DiscordBackend()
    assert backend.is_configured() is False


# ---------------------------------------------------------------------------
# ConsoleBackend
# ---------------------------------------------------------------------------


def test_console_send_always_returns_true(capsys):
    backend = ConsoleBackend()
    result = backend.send("Test Title", "Test body", priority="high", tags=["test"])
    assert result is True
    captured = capsys.readouterr()
    assert "Test Title" in captured.out
    assert "Test body" in captured.out


def test_console_is_always_configured():
    backend = ConsoleBackend()
    assert backend.is_configured() is True


# ---------------------------------------------------------------------------
# AlertFactory
# ---------------------------------------------------------------------------


def test_alert_factory_returns_ntfy_when_configured():
    with patch.dict(os.environ, {"NTFY_TOPIC": "my-topic", "ALERT_BACKEND": "ntfy"}):
        backend = AlertFactory.get_backend("ntfy")
    assert isinstance(backend, NtfyBackend)


def test_alert_factory_falls_back_to_console_when_backend_unconfigured():
    os.environ.pop("NTFY_TOPIC", None)
    backend = AlertFactory.get_backend("ntfy")
    assert isinstance(backend, ConsoleBackend)


def test_alert_factory_returns_console_for_unknown_backend():
    backend = AlertFactory.get_backend("signal")
    assert isinstance(backend, ConsoleBackend)


def test_alert_factory_reads_alert_backend_env():
    with patch.dict(os.environ, {"NTFY_TOPIC": "my-topic", "ALERT_BACKEND": "ntfy"}):
        backend = AlertFactory.get_backend()
    assert isinstance(backend, NtfyBackend)
