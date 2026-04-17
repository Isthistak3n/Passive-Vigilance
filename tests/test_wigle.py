"""Tests for WiGLEUploader — HTTP upload and CSV file discovery."""

import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Clear credentials before importing so defaults are empty
for _key in ("WIGLE_API_NAME", "WIGLE_API_KEY"):
    os.environ.pop(_key, None)

from modules.wigle import WiGLEUploader


# ---------------------------------------------------------------------------
# is_configured()
# ---------------------------------------------------------------------------


def test_is_configured_false_when_credentials_missing():
    os.environ.pop("WIGLE_API_NAME", None)
    os.environ.pop("WIGLE_API_KEY", None)
    uploader = WiGLEUploader()
    assert uploader.is_configured() is False


def test_is_configured_true_when_credentials_present():
    with patch.dict(os.environ, {"WIGLE_API_NAME": "testuser", "WIGLE_API_KEY": "testkey"}):
        uploader = WiGLEUploader()
    assert uploader.is_configured() is True


# ---------------------------------------------------------------------------
# upload_session()
# ---------------------------------------------------------------------------


def test_upload_session_returns_false_when_not_configured(tmp_path):
    uploader = WiGLEUploader()  # no credentials in env
    csv_file = tmp_path / "test.wiglecsv"
    csv_file.write_text("WiGLE CSV data\n")
    result = uploader.upload_session(str(csv_file))
    assert result is False


def test_upload_session_calls_correct_endpoint(tmp_path):
    csv_file = tmp_path / "Kismet-test.wiglecsv"
    csv_file.write_text("WiGLE CSV data\n")

    with patch.dict(os.environ, {"WIGLE_API_NAME": "user", "WIGLE_API_KEY": "key"}):
        uploader = WiGLEUploader()

    mock_resp = MagicMock()
    mock_resp.ok = True
    mock_resp.json.return_value = {"message": "Upload queued"}

    with patch("modules.wigle.requests.post", return_value=mock_resp) as mock_post:
        result = uploader.upload_session(str(csv_file))

    assert result is True
    mock_post.assert_called_once()
    call_url = mock_post.call_args[0][0]
    assert "wigle.net" in call_url
    assert "file/upload" in call_url


def test_upload_session_returns_false_on_http_error(tmp_path):
    csv_file = tmp_path / "Kismet-test.wiglecsv"
    csv_file.write_text("WiGLE CSV data\n")

    with patch.dict(os.environ, {"WIGLE_API_NAME": "user", "WIGLE_API_KEY": "key"}):
        uploader = WiGLEUploader()

    mock_resp = MagicMock()
    mock_resp.ok = False
    mock_resp.status_code = 403
    mock_resp.text = "Forbidden"

    with patch("modules.wigle.requests.post", return_value=mock_resp):
        result = uploader.upload_session(str(csv_file))

    assert result is False


# ---------------------------------------------------------------------------
# find_latest_csv()
# ---------------------------------------------------------------------------


def test_find_latest_csv_returns_none_when_no_csv_found(tmp_path):
    uploader = WiGLEUploader()
    result = uploader.find_latest_csv(kismet_log_dir=str(tmp_path))
    assert result is None


def test_find_latest_csv_returns_most_recent_file(tmp_path):
    import time

    older = tmp_path / "Kismet-old.wiglecsv"
    older.write_text("old data")
    time.sleep(0.05)
    newer = tmp_path / "Kismet-new.wiglecsv"
    newer.write_text("new data")

    uploader = WiGLEUploader()
    result = uploader.find_latest_csv(kismet_log_dir=str(tmp_path))
    assert result == str(newer)
