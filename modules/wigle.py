"""WiGLE uploader — submits Kismet wardriving CSV exports to WiGLE.net."""

import glob
import logging
import os
from typing import Optional

import requests
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

_UPLOAD_URL = "https://api.wigle.net/api/v2/file/upload"

# Directories Kismet typically writes WiGLE CSV files to
_DEFAULT_SEARCH_DIRS = [
    os.path.expanduser("~"),
    os.path.expanduser("~/.kismet/logs"),
    "/var/log/kismet",
]


class WiGLEUploader:
    """Uploads Kismet WiGLE CSV export files to the WiGLE.net API.

    Auth is HTTP Basic using the WiGLE API name and key from ``.env``.
    The upload endpoint accepts multipart form-data with a single ``file`` field.
    """

    def __init__(self) -> None:
        self._api_name = os.getenv("WIGLE_API_NAME", "")
        self._api_key = os.getenv("WIGLE_API_KEY", "")

    def is_configured(self) -> bool:
        """Return True if both WIGLE_API_NAME and WIGLE_API_KEY are set."""
        return bool(self._api_name and self._api_key)

    def upload_session(self, kismet_csv_path: str) -> bool:
        """Upload the Kismet WiGLE CSV at *kismet_csv_path* to WiGLE.net.

        Args:
            kismet_csv_path: Filesystem path to the ``.wiglecsv`` file.

        Returns:
            True on a successful upload, False on any error.
        """
        if not self.is_configured():
            logger.warning("WiGLE upload skipped — WIGLE_API_NAME/WIGLE_API_KEY not configured")
            return False

        if not os.path.exists(kismet_csv_path):
            logger.error("WiGLE upload failed — file not found: %s", kismet_csv_path)
            return False

        try:
            with open(kismet_csv_path, "rb") as fh:
                resp = requests.post(
                    _UPLOAD_URL,
                    auth=(self._api_name, self._api_key),
                    files={
                        "file": (os.path.basename(kismet_csv_path), fh, "text/csv"),
                    },
                    timeout=60,
                )
            if resp.ok:
                try:
                    message = resp.json().get("message", "accepted")
                except Exception:
                    message = resp.text[:100]
                logger.info("WiGLE upload successful: %s", message)
                return True
            else:
                logger.error(
                    "WiGLE upload failed: HTTP %d — %s",
                    resp.status_code,
                    resp.text[:200],
                )
                return False
        except requests.RequestException as exc:
            logger.error("WiGLE upload error: %s", exc)
            return False

    def find_latest_csv(self, kismet_log_dir: Optional[str] = None) -> Optional[str]:
        """Return the path of the most recently modified Kismet WiGLE CSV file.

        Args:
            kismet_log_dir: Optional extra directory to search first.

        Returns:
            Absolute path to the most recent ``.wiglecsv`` file, or ``None``
            if no files are found in any of the default search locations.
        """
        search_dirs = []
        if kismet_log_dir:
            search_dirs.append(kismet_log_dir)
        search_dirs.extend(_DEFAULT_SEARCH_DIRS)

        candidates: list[str] = []
        for directory in search_dirs:
            candidates.extend(glob.glob(os.path.join(directory, "*.wiglecsv")))

        # Deduplicate (glob across overlapping dirs can produce dupes)
        candidates = list(dict.fromkeys(candidates))

        if not candidates:
            logger.debug("WiGLE: no .wiglecsv files found in search paths")
            return None

        latest = max(candidates, key=os.path.getmtime)
        logger.debug("WiGLE: most recent CSV — %s", latest)
        return latest
