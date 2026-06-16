"""Unit tests for modules/probe_analyzer.py — ProbeAnalyzer class."""

import unittest
from unittest.mock import patch

import modules.probe_analyzer  # noqa: F401 — ensure module importable

from modules.probe_analyzer import ProbeAnalyzer

MAC_A = "aa:bb:cc:dd:ee:ff"
MAC_B = "11:22:33:44:55:66"


def _device(mac: str, ssid: str, dtype: str = "Wi-Fi Client") -> dict:
    return {"macaddr": mac, "name": ssid, "type": dtype, "manuf": "Test", "last_signal": -60}


class TestProbeAnalyzerAnalyze(unittest.TestCase):

    def test_analyze_flags_device_probing_many_ssids(self):
        """analyze() returns a suspicious entry for a device probing >10 unique SSIDs."""
        pa = ProbeAnalyzer()

        # Build up history: call analyze() with 11 different SSIDs
        for i in range(11):
            pa.analyze([_device(MAC_A, f"Network_{i:02d}")])

        # Final call also carries the last SSID
        results = pa.analyze([_device(MAC_A, "Network_10")])

        macs = [r["macaddr"] for r in results]
        self.assertIn(MAC_A, macs)
        match = next(r for r in results if r["macaddr"] == MAC_A)
        self.assertGreater(match["probe_ssid_count"], 10)
        self.assertTrue(len(match["probe_indicators"]) > 0)

    def test_analyze_does_not_flag_device_with_few_probes(self):
        """analyze() should not flag a device probing only a small number of SSIDs."""
        pa = ProbeAnalyzer()

        for ssid in ["HomeNet", "WorkNet", "CafeWifi"]:
            pa.analyze([_device(MAC_B, ssid)])

        results = pa.analyze([_device(MAC_B, "HomeNet")])
        self.assertEqual(results, [])

    def test_analyze_flags_surveillance_ssid_pattern(self):
        """analyze() flags a device probing an SSID matching a surveillance pattern."""
        pa = ProbeAnalyzer()
        results = pa.analyze([_device(MAC_A, "stingray_monitor_net")])
        # Single observation of a surveillance SSID should flag
        macs = [r["macaddr"] for r in results]
        self.assertIn(MAC_A, macs)


class TestProbeAnalyzerSummary(unittest.TestCase):

    def test_get_probe_summary_returns_correct_structure(self):
        """get_probe_summary() returns a dict with all required keys."""
        pa = ProbeAnalyzer()
        pa.analyze([_device(MAC_A, "SomeNetwork")])
        summary = pa.get_probe_summary(MAC_A)
        for key in ("mac", "ssid_count", "unique_ssids", "suspicion_indicators"):
            self.assertIn(key, summary, f"missing key: {key}")
        self.assertEqual(summary["mac"], MAC_A)
        self.assertIsInstance(summary["unique_ssids"], list)
        self.assertIsInstance(summary["suspicion_indicators"], list)

    def test_get_probe_summary_unknown_mac_returns_empty(self):
        """get_probe_summary() for an unknown MAC returns zero counts."""
        pa = ProbeAnalyzer()
        summary = pa.get_probe_summary("ff:ff:ff:ff:ff:ff")
        self.assertEqual(summary["ssid_count"], 0)
        self.assertEqual(summary["unique_ssids"], [])


class TestProbeAnalyzerBounds(unittest.TestCase):
    """The probe history must stay bounded on a long-running node."""

    @patch("modules.probe_analyzer._MAX_TRACKED_MACS", 10)
    def test_mac_history_is_lru_bounded(self):
        """Tracking more MACs than the cap evicts the least-recently-seen ones."""
        pa = ProbeAnalyzer()
        for i in range(25):
            pa.analyze([_device("00:00:00:00:00:%02x" % i, "Net")])
        self.assertLessEqual(len(pa._probe_history), 10)
        # The most recent MAC is retained; the oldest is evicted.
        self.assertIn("00:00:00:00:00:18", pa._probe_history)   # i=24
        self.assertNotIn("00:00:00:00:00:00", pa._probe_history)  # i=0

    @patch("modules.probe_analyzer._MAX_SSIDS_PER_MAC", 5)
    def test_ssids_per_mac_is_bounded(self):
        """A single chatty device cannot accumulate SSIDs without limit."""
        pa = ProbeAnalyzer()
        for i in range(50):
            pa.analyze([_device(MAC_A, f"Net_{i:02d}")])
        self.assertLessEqual(len(pa._probe_history[MAC_A]), 5)

    @patch("modules.probe_analyzer._MAX_TRACKED_MACS", 3)
    def test_reseen_mac_is_not_evicted(self):
        """A MAC seen again is refreshed (most-recent) and survives eviction."""
        pa = ProbeAnalyzer()
        pa.analyze([_device(MAC_A, "Net")])
        for i in range(5):
            pa.analyze([_device("00:00:00:00:00:%02x" % i, "Net")])
            pa.analyze([_device(MAC_A, "Net")])  # keep MAC_A fresh
        self.assertIn(MAC_A, pa._probe_history)


if __name__ == "__main__":
    unittest.main()
