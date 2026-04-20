"""SDR hardware inventory and mode resolution.

Detects how many RTL-SDR dongles are present and resolves the operating
mode (SHARED time-share vs DEDICATED simultaneous) from that count plus
the SDR_MODE environment variable.
"""

import enum
import logging
import re
import subprocess

logger = logging.getLogger(__name__)

# rtl_test -t enumerates devices and exits; 10 s is more than enough.
_RTL_TEST_TIMEOUT = 10


class SDRMode(enum.Enum):
    AUTO = "auto"
    SHARED = "shared"
    DEDICATED = "dedicated"


def detect_sdr_count() -> int:
    """Return the number of RTL-SDR dongles detected via ``rtl_test -t``.

    Returns 0 if ``rtl_test`` is not installed, times out, or reports no
    devices.  Always returns a non-negative integer.
    """
    try:
        result = subprocess.run(
            ["rtl_test", "-t"],
            capture_output=True,
            text=True,
            timeout=_RTL_TEST_TIMEOUT,
        )
        output = result.stdout + result.stderr
        for line in output.splitlines():
            m = re.search(r"found\s+(\d+)\s+device", line, re.IGNORECASE)
            if m:
                count = int(m.group(1))
                logger.debug("SDR detection: %d device(s) found", count)
                return count
        if "no supported devices" in output.lower():
            logger.debug("SDR detection: no supported devices found")
            return 0
        # rtl_test ran but produced unexpected output — treat as 0
        logger.debug("SDR detection: could not parse rtl_test output")
        return 0
    except FileNotFoundError:
        logger.debug("rtl_test not found — install rtl-sdr package for SDR detection")
        return 0
    except subprocess.TimeoutExpired:
        logger.warning("SDR detection: rtl_test timed out after %ds", _RTL_TEST_TIMEOUT)
        return 0
    except Exception as exc:
        logger.warning("SDR detection failed: %s", exc)
        return 0


def resolve_sdr_mode(env_setting: str, detected_count: int) -> SDRMode:
    """Return the effective :class:`SDRMode` from *env_setting* and *detected_count*.

    Rules
    -----
    - ``"shared"``    → always SHARED regardless of dongle count
    - ``"dedicated"`` → always DEDICATED regardless of dongle count
    - ``"auto"``      → DEDICATED if count ≥ 2, otherwise SHARED
      (callers must handle the 0-dongle sub-case: both modules disabled)
    """
    setting = env_setting.strip().lower()
    if setting == SDRMode.SHARED.value:
        return SDRMode.SHARED
    if setting == SDRMode.DEDICATED.value:
        return SDRMode.DEDICATED
    # AUTO
    if detected_count >= 2:
        return SDRMode.DEDICATED
    return SDRMode.SHARED
