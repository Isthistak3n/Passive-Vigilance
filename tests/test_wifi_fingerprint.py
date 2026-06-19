"""Unit tests for modules/wifi_fingerprint.py — the WiFi randomization-resistant signature."""
import unittest

from modules.wifi_fingerprint import compute_pnl_fingerprint, compute_wifi_fingerprint


def _dev(probe_ssids=None, probe_fingerprint=None, name="", manuf="", type="Wi-Fi Client"):
    return {
        "probe_ssids": probe_ssids or [],
        "probe_fingerprint": probe_fingerprint,
        "name": name,
        "manuf": manuf,
        "type": type,
    }


class TestStrength(unittest.TestCase):

    def test_named_ssids_make_it_strong(self):
        fp = compute_wifi_fingerprint(_dev(probe_ssids=["HomeNet", "CoffeeWiFi"]))
        self.assertIsNotNone(fp)
        self.assertTrue(fp.strong)

    def test_ie_fingerprint_alone_is_weak(self):
        # no named SSIDs but a distinctive IE hash → keyed, but not groupable
        fp = compute_wifi_fingerprint(_dev(probe_fingerprint=123456789))
        self.assertIsNotNone(fp)
        self.assertFalse(fp.strong)

    def test_nothing_to_fingerprint_returns_none(self):
        self.assertIsNone(compute_wifi_fingerprint(_dev()))

    def test_zero_probe_fingerprint_treated_as_absent(self):
        self.assertIsNone(compute_wifi_fingerprint(_dev(probe_fingerprint=0)))


class TestStability(unittest.TestCase):

    def test_same_ssid_set_same_key_regardless_of_order(self):
        a = compute_wifi_fingerprint(_dev(probe_ssids=["A", "B"], probe_fingerprint=42))
        b = compute_wifi_fingerprint(_dev(probe_ssids=["B", "A"], probe_fingerprint=42))
        self.assertEqual(a.key, b.key)

    def test_different_ie_fingerprint_splits_same_ssids(self):
        # same probed SSIDs but different stacks → different (finer) keys, never merged
        a = compute_wifi_fingerprint(_dev(probe_ssids=["A"], probe_fingerprint=1))
        b = compute_wifi_fingerprint(_dev(probe_ssids=["A"], probe_fingerprint=2))
        self.assertNotEqual(a.key, b.key)

    def test_different_ssids_different_key(self):
        a = compute_wifi_fingerprint(_dev(probe_ssids=["A"]))
        b = compute_wifi_fingerprint(_dev(probe_ssids=["B"]))
        self.assertNotEqual(a.key, b.key)

    def test_key_prefix(self):
        fp = compute_wifi_fingerprint(_dev(probe_ssids=["A"]))
        self.assertTrue(fp.key.startswith("wifi-fp:"))

    def test_blank_ssids_ignored(self):
        # the wildcard/broadcast probe and whitespace are not identity
        self.assertIsNone(compute_wifi_fingerprint(_dev(probe_ssids=["", "  "])))


class TestLabel(unittest.TestCase):

    def test_single_ssid_label(self):
        fp = compute_wifi_fingerprint(_dev(probe_ssids=["HomeNet"]))
        self.assertEqual(fp.label, "HomeNet")

    def test_multi_ssid_label_counts_extras(self):
        fp = compute_wifi_fingerprint(_dev(probe_ssids=["HomeNet", "Cafe", "Airport"]))
        self.assertEqual(fp.label, "Airport +2")  # sorted: Airport, Cafe, HomeNet

    def test_name_label_when_no_ssids(self):
        fp = compute_wifi_fingerprint(_dev(probe_fingerprint=7, name="MyAP"))
        self.assertEqual(fp.label, "MyAP")

    def test_manuf_label_when_no_ssids_or_name(self):
        fp = compute_wifi_fingerprint(_dev(probe_fingerprint=7, manuf="Espressif"))
        self.assertEqual(fp.label, "Espressif")

    def test_unknown_manuf_falls_through_to_type(self):
        fp = compute_wifi_fingerprint(_dev(probe_fingerprint=7, manuf="Unknown", type="Wi-Fi Client"))
        self.assertEqual(fp.label, "Wi-Fi Client")


class TestPNLFingerprint(unittest.TestCase):

    def test_ie_anchor_stable_as_pnl_grows(self):
        # Same IE hash, growing accumulated PNL -> SAME key (anchored on the IE hash,
        # not the SSID set), so the identity survives rotation and PNL accrual.
        a = compute_pnl_fingerprint(_dev(probe_fingerprint=777), accumulated_pnl=["Home"])
        b = compute_pnl_fingerprint(_dev(probe_fingerprint=777),
                                    accumulated_pnl=["Home", "Work", "Cafe"])
        self.assertEqual(a.key, b.key)
        self.assertEqual(b.pnl, ("Cafe", "Home", "Work"))  # carried, sorted
        self.assertTrue(b.strong)

    def test_different_ie_hash_different_key(self):
        a = compute_pnl_fingerprint(_dev(probe_fingerprint=111), accumulated_pnl=["Home"])
        b = compute_pnl_fingerprint(_dev(probe_fingerprint=222), accumulated_pnl=["Home"])
        self.assertNotEqual(a.key, b.key)

    def test_no_ie_and_no_pnl_returns_none(self):
        self.assertIsNone(compute_pnl_fingerprint(_dev(), accumulated_pnl=[]))

    def test_pnl_only_fallback_when_no_ie(self):
        fp = compute_pnl_fingerprint(_dev(probe_fingerprint=None), accumulated_pnl=["HomeNet"])
        self.assertIsNotNone(fp)
        self.assertTrue(fp.strong)
        self.assertEqual(fp.pnl, ("HomeNet",))


if __name__ == "__main__":
    unittest.main()
