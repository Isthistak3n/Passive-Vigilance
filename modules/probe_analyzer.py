"""WiFi probe request analyzer — detect devices probing suspicious SSID patterns."""

import logging

logger = logging.getLogger(__name__)

# SSID substrings that may indicate surveillance equipment or deliberate tracking
_SURVEILLANCE_PATTERNS = [
    "surveillance",
    "stingray",
    "imsi",
    "dirtbox",
    "triggerfish",
    "cellhawk",
    "monitor",
    "probe",
    "intercept",
    "track",
    "follow",
    "watch",
]

# Number of unique probed SSIDs above which a device is considered suspicious
_SSID_COUNT_THRESHOLD = 10


class ProbeAnalyzer:
    """Accumulate WiFi probe request history and flag suspicious probe patterns.

    Intended to run on each ``poll_devices()`` result.  Probe history
    persists across calls — the longer the session, the more accurate the
    analysis.

    Suspicion indicators:

    - Device probing more than :data:`_SSID_COUNT_THRESHOLD` unique SSIDs
      (likely a phone or tracking device scanning for known networks)
    - Device probing an SSID matching a known surveillance-related pattern
    """

    def __init__(self) -> None:
        # mac → set of SSIDs seen probed by that device
        self._probe_history: dict = {}

    # ------------------------------------------------------------------
    # Analysis
    # ------------------------------------------------------------------

    def analyze(self, devices: list) -> list:
        """Update probe history and return devices with suspicious probe patterns.

        Updates internal history from *devices*, then evaluates every device
        in the current poll against suspicion criteria.

        Args:
            devices: Device dicts from :meth:`~modules.kismet.KismetModule.poll_devices`.

        Returns:
            Subset of *devices* flagged as suspicious, with ``probe_indicators``
            (list[str]) and ``probe_ssid_count`` (int) keys added.
        """
        self._update_history(devices)

        suspicious = []
        for device in devices:
            mac = device.get("macaddr", "")
            if not mac or mac not in self._probe_history:
                continue
            indicators = self._evaluate(mac)
            if indicators:
                suspicious.append({
                    **device,
                    "probe_indicators": indicators,
                    "probe_ssid_count": len(self._probe_history[mac]),
                })

        logger.debug(
            "ProbeAnalyzer: %d devices, %d suspicious",
            len(devices), len(suspicious),
        )
        return suspicious

    def get_probe_summary(self, mac: str) -> dict:
        """Return probe statistics for a specific MAC address.

        Returns:
            Dict with keys ``mac``, ``ssid_count``, ``unique_ssids``,
            ``suspicion_indicators``.
        """
        ssids = self._probe_history.get(mac, set())
        return {
            "mac":                  mac,
            "ssid_count":           len(ssids),
            "unique_ssids":         sorted(ssids),
            "suspicion_indicators": self._evaluate(mac),
        }

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _update_history(self, devices: list) -> None:
        """Accumulate SSIDs from probe-capable device types."""
        for device in devices:
            mac   = device.get("macaddr", "")
            ssid  = device.get("name", "").strip()
            dtype = device.get("type", "").lower()
            if not mac or not ssid:
                continue
            # WiFi client devices generate probe requests
            if any(kw in dtype for kw in ("client", "wi-fi", "wifi", "probe")):
                self._probe_history.setdefault(mac, set()).add(ssid)

    def _evaluate(self, mac: str) -> list:
        """Return a list of suspicion indicator strings for *mac*, or []."""
        ssids = self._probe_history.get(mac, set())
        indicators = []

        if len(ssids) > _SSID_COUNT_THRESHOLD:
            indicators.append(
                f"probing {len(ssids)} unique SSIDs (threshold: {_SSID_COUNT_THRESHOLD})"
            )

        matching = [s for s in ssids if self._is_surveillance_ssid(s)]
        if matching:
            indicators.append(
                f"probing surveillance-related SSID(s): {', '.join(sorted(matching)[:3])}"
            )

        return indicators

    @staticmethod
    def _is_surveillance_ssid(ssid: str) -> bool:
        """Return True if *ssid* contains a known surveillance-related pattern."""
        lower = ssid.lower()
        return any(pattern in lower for pattern in _SURVEILLANCE_PATTERNS)
