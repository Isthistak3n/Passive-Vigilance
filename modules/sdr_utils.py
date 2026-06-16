"""Shared utilities for RTL-SDR hardware detection."""

import logging
import subprocess
from typing import FrozenSet

logger = logging.getLogger(__name__)

# Single source of truth for RTL-SDR (Realtek RTL2832U) USB identity. Callers that
# match `lsusb` output use the combined vendor:product set; callers that match
# sysfs idVendor/idProduct separately (e.g. the SDR coordinator's usbreset node
# lookup) use the components. Keep all RTL-SDR ID matching pointed here.
RTL_SDR_VENDOR: str = "0bda"
RTL_SDR_PRODUCTS: FrozenSet[str] = frozenset({"2832", "2838", "2813"})
_RTL_SDR_USB_IDS: FrozenSet[str] = frozenset(
    f"{RTL_SDR_VENDOR}:{product}" for product in RTL_SDR_PRODUCTS
)


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
