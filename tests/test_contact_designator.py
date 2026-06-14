"""Unit tests for modules/contact_designator.py — the CLASS-IDENT-# label builder."""
import unittest

from modules.contact_designator import (
    class_token,
    designator,
    fallback_number,
    group_key,
    ident_token,
)


class TestClassToken(unittest.TestCase):
    def test_ap(self):
        self.assertEqual(class_token("Wi-Fi AP"), "AP")

    def test_wds_ap(self):
        self.assertEqual(class_token("Wi-Fi WDS AP"), "AP")

    def test_client(self):
        self.assertEqual(class_token("Wi-Fi Client"), "CLI")

    def test_bridged(self):
        self.assertEqual(class_token("Wi-Fi Bridged"), "BR")

    def test_btle(self):
        self.assertEqual(class_token("BTLE"), "BLE")

    def test_unknown(self):
        self.assertEqual(class_token("Wi-Fi Device"), "DEV")
        self.assertEqual(class_token(""), "DEV")


class TestIdentToken(unittest.TestCase):
    def test_ssid_first(self):
        self.assertEqual(ident_token(ssid="NETGEAR13 5G", label="x", manufacturer="y"),
                         "NETGEAR13_5G")

    def test_hyphen_and_space_squeezed(self):
        self.assertEqual(ident_token(ssid="Cafe-Net WiFi"), "Cafe_Net_WiFi")

    def test_label_when_no_ssid(self):
        self.assertEqual(ident_token(label="Apple", manufacturer="z"), "Apple")

    def test_manufacturer_when_no_ssid_or_label(self):
        self.assertEqual(ident_token(manufacturer="Espressif"), "Espressif")

    def test_unknown_manufacturer_skipped(self):
        # 'Unknown' is not identifying -> fall through to the token
        self.assertEqual(ident_token(manufacturer="Unknown", fingerprint="wifi-fp:7a3f2b"),
                         "a3f2b"[-4:])

    def test_token_from_fingerprint_tail(self):
        self.assertEqual(ident_token(fingerprint="ble-fp:deadbeef"), "beef")

    def test_token_from_mac_tail_when_nothing_else(self):
        self.assertEqual(ident_token(mac="aa:bb:cc:dd:ee:ff"), "eeff")

    def test_length_capped(self):
        long = "A" * 40
        self.assertLessEqual(len(ident_token(ssid=long)), 18)


class TestDesignatorAndGroup(unittest.TestCase):
    def test_group_key(self):
        self.assertEqual(group_key("AP", "NETGEAR13_5G"), "AP-NETGEAR13_5G")

    def test_designator(self):
        self.assertEqual(designator("AP", "NETGEAR13_5G", 2), "AP-NETGEAR13_5G-2")

    def test_fallback_number_stable(self):
        a = fallback_number("wifi-fp:abc")
        b = fallback_number("wifi-fp:abc")
        c = fallback_number("wifi-fp:xyz")
        self.assertEqual(a, b)            # deterministic
        self.assertNotEqual(a, c)         # distinct keys differ (very likely)
        self.assertTrue(0 <= a < 10000)


if __name__ == "__main__":
    unittest.main()
