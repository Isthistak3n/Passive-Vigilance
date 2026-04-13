"""Unit tests for modules/probe_analyzer.py — ProbeAnalyzer class."""

import unittest

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


if __name__ == "__main__":
    unittest.main()
