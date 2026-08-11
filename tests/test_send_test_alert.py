"""Tests for scripts/send_test_alert.py — end-to-end backend validation tool."""

import importlib.util
import os
from pathlib import Path
from unittest.mock import patch

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "send_test_alert.py"
_spec = importlib.util.spec_from_file_location("send_test_alert", _SCRIPT)
tool = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(tool)


def test_console_backend_validates_successfully(capsys):
    rc = tool.main(["--backend", "console"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "Console" in out
    assert "Generic send: OK" in out


def test_unconfigured_backend_is_a_failure_not_a_silent_console_pass(capsys):
    """The factory's console fallback IS the failure this tool exists to catch."""
    with patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "", "TELEGRAM_CHAT_ID": ""}):
        rc = tool.main(["--backend", "telegram"])
    out = capsys.readouterr().out
    assert rc == 2
    assert "not configured" in out


def test_full_exercises_every_typed_formatter(capsys):
    rc = tool.main(["--backend", "console", "--full"])
    out = capsys.readouterr().out
    assert rc == 0
    for path in ("persistence", "aircraft", "drone", "remote_id"):
        assert f"Typed {path}:" in out


def test_synthetic_event_uses_the_documentation_mac_range():
    """The test page must never look like a real device."""
    event = tool._test_event()
    assert event.mac.startswith("00:00:5e:00:53:")
    assert event.device_type.startswith("TEST")
