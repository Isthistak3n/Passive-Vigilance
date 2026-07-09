"""Unit tests for modules/mac_utils.py."""

import unittest
from unittest.mock import patch

import modules.mac_utils  # noqa: F401 — ensure module importable

from modules.mac_utils import (
    MACFingerprint,
    get_mac_type,
    get_randomization_vendor_hint,
    group_by_fingerprint,
    is_randomized_mac,
    normalize_mac,
)

# Known randomized MACs — locally administered bit set (second hex digit 2/6/a/e)
RAND_MAC_1 = "02:ab:cd:ef:01:23"   # second digit = 2
RAND_MAC_2 = "06:11:22:33:44:55"   # second digit = 6
RAND_MAC_3 = "0a:aa:bb:cc:dd:ee"   # second digit = a
RAND_MAC_4 = "0e:ff:00:11:22:33"   # second digit = e

# Known static MAC — OUI assigned (second hex digit 0)
STATIC_MAC  = "a4:c3:f0:11:22:33"  # second digit = 4 (bit 1 = 0)
STATIC_MAC2 = "00:1a:2b:3c:4d:5e"  # second digit = 0


class TestIsRandomizedMac(unittest.TestCase):

    def test_returns_true_for_second_digit_2(self):
        self.assertTrue(is_randomized_mac(RAND_MAC_1))

    def test_returns_true_for_second_digit_6(self):
        self.assertTrue(is_randomized_mac(RAND_MAC_2))

    def test_returns_true_for_second_digit_a(self):
        self.assertTrue(is_randomized_mac(RAND_MAC_3))

    def test_returns_true_for_second_digit_e(self):
        self.assertTrue(is_randomized_mac(RAND_MAC_4))

    def test_returns_false_for_static_mac(self):
        self.assertFalse(is_randomized_mac(STATIC_MAC))

    def test_handles_uppercase_input(self):
        self.assertTrue(is_randomized_mac("02:AB:CD:EF:01:23"))

    def test_handles_dash_separated_input(self):
        self.assertTrue(is_randomized_mac("02-ab-cd-ef-01-23"))


class TestGetMacType(unittest.TestCase):

    def test_returns_randomized_for_locally_administered(self):
        self.assertEqual(get_mac_type(RAND_MAC_1), "randomized")

    def test_returns_static_for_oui_assigned(self):
        self.assertEqual(get_mac_type(STATIC_MAC), "static")


class TestNormalizeMac(unittest.TestCase):

    def test_lowercases_and_uses_colons(self):
        self.assertEqual(normalize_mac("AA-BB-CC-DD-EE-FF"), "aa:bb:cc:dd:ee:ff")

    def test_compact_form_expanded(self):
        self.assertEqual(normalize_mac("aabbccddeeff"), "aa:bb:cc:dd:ee:ff")

    def test_already_normalized_unchanged(self):
        self.assertEqual(normalize_mac("aa:bb:cc:dd:ee:ff"), "aa:bb:cc:dd:ee:ff")


class TestGetRandomizationVendorHint(unittest.TestCase):
    """Tests the branching contract of get_randomization_vendor_hint() itself:
    randomized -> "Unknown" always; static -> delegates to get_manufacturer().

    The static-MAC case is tested via a mocked get_manufacturer() rather than
    relying on the real OUI database's absence/presence — data/oui/manuf is an
    optional, separately-downloaded file (scripts/fetch_oui.sh); whether a given
    prefix resolves depends on ambient machine state, not on this function's own
    logic. See tests/test_oui_database.py for OUIDatabase's own lookup behavior.
    """

    def test_static_mac_returns_whatever_get_manufacturer_resolves(self):
        with patch("modules.mac_utils.get_manufacturer", return_value="Acme") as m:
            self.assertEqual(get_randomization_vendor_hint(STATIC_MAC), "Acme")
            m.assert_called_once_with(STATIC_MAC)

    def test_static_mac_returns_empty_when_oui_database_has_no_match(self):
        with patch("modules.mac_utils.get_manufacturer", return_value=""):
            self.assertEqual(get_randomization_vendor_hint(STATIC_MAC), "")

    def test_returns_unknown_for_randomized_mac_regardless_of_oui(self):
        # Randomized MACs short-circuit to "Unknown" without consulting the OUI
        # database at all — their OUI is meaningless by definition.
        with patch("modules.mac_utils.get_manufacturer") as m:
            self.assertEqual(get_randomization_vendor_hint(RAND_MAC_1), "Unknown")
            m.assert_not_called()


class TestGroupByFingerprint(unittest.TestCase):

    def test_groups_macs_sharing_probe_ssid(self):
        """Two randomized MACs probing the same SSID are grouped together."""
        devices = [
            {"macaddr": RAND_MAC_1, "name": "HomeNetwork", "last_signal": -60},
            {"macaddr": RAND_MAC_2, "name": "HomeNetwork", "last_signal": -65},
        ]
        groups = group_by_fingerprint(devices)
        self.assertEqual(len(groups), 1)
        self.assertIsInstance(groups[0], MACFingerprint)
        self.assertEqual(groups[0].device_count, 2)

    def test_does_not_group_macs_with_different_ssids(self):
        """Two randomized MACs probing different SSIDs remain separate groups."""
        devices = [
            {"macaddr": RAND_MAC_1, "name": "NetworkA", "last_signal": -60},
            {"macaddr": RAND_MAC_2, "name": "NetworkB", "last_signal": -65},
        ]
        groups = group_by_fingerprint(devices)
        self.assertEqual(len(groups), 2)

    def test_returns_empty_for_no_randomized_macs(self):
        """Static MACs are not fingerprinted — returns empty list."""
        devices = [
            {"macaddr": STATIC_MAC, "name": "SomeNet", "last_signal": -50},
        ]
        groups = group_by_fingerprint(devices)
        self.assertEqual(groups, [])

    def test_returns_empty_for_empty_input(self):
        self.assertEqual(group_by_fingerprint([]), [])


if __name__ == "__main__":
    unittest.main()
