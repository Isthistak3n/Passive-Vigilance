"""Unit tests for modules/ignore_list.py — IgnoreList class."""

import json
import os
import tempfile
import unittest

import modules.ignore_list  # noqa: F401 — ensure module is importable

from modules.ignore_list import IgnoreList


class TestIgnoreListMACFiltering(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.il = IgnoreList(data_dir=self.tmp)

    # ------------------------------------------------------------------
    # MAC add / lookup
    # ------------------------------------------------------------------

    def test_added_full_mac_is_ignored(self):
        self.il.add_mac("AA:BB:CC:DD:EE:FF")
        self.assertTrue(self.il.is_ignored_mac("AA:BB:CC:DD:EE:FF"))

    def test_mac_lookup_is_case_insensitive(self):
        self.il.add_mac("aa:bb:cc:dd:ee:ff")
        self.assertTrue(self.il.is_ignored_mac("AA:BB:CC:DD:EE:FF"))

    def test_mac_with_dashes_normalized(self):
        self.il.add_mac("AA-BB-CC-DD-EE-FF")
        self.assertTrue(self.il.is_ignored_mac("aa:bb:cc:dd:ee:ff"))

    def test_unknown_mac_not_ignored(self):
        self.il.add_mac("aa:bb:cc:dd:ee:ff")
        self.assertFalse(self.il.is_ignored_mac("11:22:33:44:55:66"))

    # ------------------------------------------------------------------
    # OUI add / lookup
    # ------------------------------------------------------------------

    def test_oui_prefix_matches_device_in_same_vendor(self):
        self.il.add_oui("aa:bb:cc")
        self.assertTrue(self.il.is_ignored_mac("aa:bb:cc:11:22:33"))

    def test_oui_does_not_match_different_vendor(self):
        self.il.add_oui("aa:bb:cc")
        self.assertFalse(self.il.is_ignored_mac("11:22:33:aa:bb:cc"))

    def test_oui_extracted_from_full_mac(self):
        """add_oui() should accept a full MAC and extract the OUI prefix."""
        self.il.add_oui("aa:bb:cc:dd:ee:ff")
        self.assertTrue(self.il.is_ignored_mac("aa:bb:cc:00:00:00"))

    # ------------------------------------------------------------------
    # SSID add / lookup
    # ------------------------------------------------------------------

    def test_added_ssid_is_ignored(self):
        self.il.add_ssid("MyHomeNetwork")
        self.assertTrue(self.il.is_ignored_ssid("MyHomeNetwork"))

    def test_ssid_lookup_is_case_insensitive(self):
        self.il.add_ssid("MyHomeNetwork")
        self.assertTrue(self.il.is_ignored_ssid("myhomenetwork"))

    def test_unknown_ssid_not_ignored(self):
        self.il.add_ssid("KnownNetwork")
        self.assertFalse(self.il.is_ignored_ssid("UnknownNetwork"))

    # ------------------------------------------------------------------
    # Remove
    # ------------------------------------------------------------------

    def test_remove_mac_returns_true_and_stops_matching(self):
        self.il.add_mac("aa:bb:cc:dd:ee:ff")
        removed = self.il.remove_mac("aa:bb:cc:dd:ee:ff")
        self.assertTrue(removed)
        self.assertFalse(self.il.is_ignored_mac("aa:bb:cc:dd:ee:ff"))

    def test_remove_ssid_returns_true_and_stops_matching(self):
        self.il.add_ssid("MyNet")
        removed = self.il.remove_ssid("MyNet")
        self.assertTrue(removed)
        self.assertFalse(self.il.is_ignored_ssid("MyNet"))

    def test_remove_nonexistent_mac_returns_false(self):
        removed = self.il.remove_mac("ff:ee:dd:cc:bb:aa")
        self.assertFalse(removed)


class TestIgnoreListPersistence(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def test_save_and_reload_preserves_mac(self):
        il = IgnoreList(data_dir=self.tmp)
        il.add_mac("aa:bb:cc:dd:ee:ff", label="test device")
        il.save()

        il2 = IgnoreList(data_dir=self.tmp)
        self.assertTrue(il2.is_ignored_mac("aa:bb:cc:dd:ee:ff"))

    def test_save_and_reload_preserves_oui(self):
        il = IgnoreList(data_dir=self.tmp)
        il.add_oui("de:ad:be", label="test vendor")
        il.save()

        il2 = IgnoreList(data_dir=self.tmp)
        self.assertTrue(il2.is_ignored_mac("de:ad:be:00:11:22"))

    def test_save_and_reload_preserves_ssid(self):
        il = IgnoreList(data_dir=self.tmp)
        il.add_ssid("HomeNet", label="my AP")
        il.save()

        il2 = IgnoreList(data_dir=self.tmp)
        self.assertTrue(il2.is_ignored_ssid("homenet"))

    def test_mac_file_is_valid_json(self):
        il = IgnoreList(data_dir=self.tmp)
        il.add_mac("11:22:33:44:55:66")
        il.save()

        mac_path = os.path.join(self.tmp, "mac_ignore.json")
        with open(mac_path) as fh:
            data = json.load(fh)
        self.assertIn("version", data)
        self.assertIn("entries", data)
        self.assertEqual(len(data["entries"]), 1)


class TestIgnoreListBulkImport(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.il = IgnoreList(data_dir=self.tmp)

    def test_add_from_kismet_adds_new_devices(self):
        devices = [
            {"macaddr": "aa:bb:cc:dd:ee:ff", "name": "Phone", "manuf": "Apple"},
            {"macaddr": "11:22:33:44:55:66", "name": "Laptop", "manuf": "Intel"},
        ]
        added = self.il.add_from_kismet(devices)
        self.assertEqual(added, 2)
        self.assertTrue(self.il.is_ignored_mac("aa:bb:cc:dd:ee:ff"))

    def test_add_from_kismet_skips_duplicates(self):
        self.il.add_mac("aa:bb:cc:dd:ee:ff")
        devices = [{"macaddr": "aa:bb:cc:dd:ee:ff", "name": "Phone", "manuf": ""}]
        added = self.il.add_from_kismet(devices)
        self.assertEqual(added, 0)

    def test_add_from_kismet_skips_empty_mac(self):
        devices = [{"macaddr": "", "name": "unknown"}]
        added = self.il.add_from_kismet(devices)
        self.assertEqual(added, 0)


class TestIgnoreListStats(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.il = IgnoreList(data_dir=self.tmp)

    def test_stats_returns_correct_counts(self):
        self.il.add_mac("aa:bb:cc:dd:ee:ff")
        self.il.add_oui("11:22:33")
        self.il.add_ssid("TestNet")
        s = self.il.stats()
        self.assertEqual(s["mac_count"], 1)
        self.assertEqual(s["oui_count"], 1)
        self.assertEqual(s["ssid_count"], 1)

    def test_stats_empty_on_new_instance(self):
        s = self.il.stats()
        self.assertEqual(s["mac_count"], 0)
        self.assertEqual(s["oui_count"], 0)
        self.assertEqual(s["ssid_count"], 0)


if __name__ == "__main__":
    unittest.main()
