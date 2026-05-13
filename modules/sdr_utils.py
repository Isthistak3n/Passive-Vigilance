"""Shared utilities for RTL-SDR hardware detection."""

import logging
import subprocess
from typing import FrozenSet

logger = logging.getLogger(__name__)

_RTL_SDR_USB_IDS: FrozenSet[str] = frozenset({
    "0bda:2832", "0bda:2838", "0bda:2813"
})


def is_rtl_sdr_present() -> bool:
    """Return True if a known RTL-SDR dongle is detected via lsusb."""
    try:
        result = subprocess.run(
            ["lsusb"], capture_output=True, text=True, timeout=5
        )
        for line in result.stdout.splitlines():
            parts = line.lower().split()
            for part in parts:
                if part in _RTL_SDR_USB_IDS:
                    logger.debug("RTL-SDR detected: %s", line.strip())
                    return True
    except Exception as exc:
        logger.debug("lsusb check failed: %s", exc)
    return False


def get_rtl_sdr_usb_ids() -> FrozenSet[str]:
    """Return the set of known RTL-SDR USB vendor:product IDs."""
    return _RTL_SDR_USB_IDS
