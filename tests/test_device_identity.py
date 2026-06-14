"""Unit tests for modules/device_identity.py — shared identity resolution."""
import unittest

from modules.device_identity import (
    fingerprint_label,
    is_ble_device,
    strong_fingerprint,
)


def _wifi(probe_ssids=None, probe_fingerprint=None, name="", type="Wi-Fi Client"):
    return {"macaddr": "a2:00:00:00:00:01", "probe_ssids": probe_ssids or [],
            "probe_fingerprint": probe_fingerprint, "name": name, "type": type}


def _ble(service_uuids=None, name="", company_ids=None):
    return {"macaddr": "c2:00:00:00:00:01", "type": "BTLE",
            "company_ids": company_ids or [], "service_uuids": service_uuids or [],
            "service_data_uuids": [], "name": name, "appearance": None}


class TestIsBleDevice(unittest.TestCase):
    def test_btle_type(self):
        self.assertTrue(is_ble_device({"type": "BTLE"}))

    def test_advert_fields(self):
        self.assertTrue(is_ble_device({"service_uuids": [0x180D]}))

    def test_wifi_is_not_ble(self):
        self.assertFalse(is_ble_device({"type": "Wi-Fi Client"}))


class TestStrongFingerprint(unittest.TestCase):
    def test_wifi_named_probe_is_strong(self):
        self.assertTrue(strong_fingerprint(_wifi(probe_ssids=["HomeNet"])).startswith("wifi-fp:"))

    def test_wifi_no_probe_is_none(self):
        self.assertIsNone(strong_fingerprint(_wifi()))

    def test_ble_service_is_strong(self):
        self.assertTrue(strong_fingerprint(_ble(service_uuids=[0x180D])).startswith("ble-fp:"))

    def test_ble_bare_vendor_is_none(self):
        self.assertIsNone(strong_fingerprint(_ble(company_ids=[0x004C])))

    def test_same_content_same_key(self):
        a = strong_fingerprint(_wifi(probe_ssids=["A", "B"]))
        b = strong_fingerprint(_wifi(probe_ssids=["B", "A"]))
        self.assertEqual(a, b)


class TestFingerprintLabel(unittest.TestCase):
    def test_label_for_strong(self):
        self.assertEqual(fingerprint_label(_wifi(probe_ssids=["HomeNet"])), "HomeNet")

    def test_empty_for_weak(self):
        self.assertEqual(fingerprint_label(_wifi()), "")

    def test_ble_vendor_label(self):
        self.assertEqual(fingerprint_label(_ble(service_uuids=[0x180D], company_ids=[0x004C])), "Apple")


if __name__ == "__main__":
    unittest.main()
