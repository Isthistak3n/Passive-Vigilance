"""WiGLE uploader — submits wardriving capture files to WiGLE.net."""

import logging

logger = logging.getLogger(__name__)


class WiGLEUploader:
    """Uploads Kismet/WiGLE-format capture files to the WiGLE.net API."""

    def upload_session(self, filepath: str) -> None:
        """Upload the capture file at *filepath* to WiGLE using configured API credentials."""
        raise NotImplementedError
