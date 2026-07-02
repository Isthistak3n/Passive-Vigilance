"""Unit tests for modules/device_identity.py — shared identity resolution."""
import unittest

from modules.device_identity import (
    fingerprint_label,
    is_ble_device,
    strong_fingerprint,
)


def _wifi(probe_ssids=None, probe_fingerprint=None, fp_anchor=None, name="",
          type="Wi-Fi Client", mac="a2:00:00:00:00:01"):
    # The enriched WiFi key needs the IE hash (probe_fingerprint) AND the
    # orchestrator-attached distinctive anchor (fp_anchor = rarest near-unique SSID).
    return {"macaddr": mac, "probe_ssids": probe_ssids or [],
            "probe_fingerprint": probe_fingerprint, "fp_anchor": fp_anchor,
            "name": name, "type": type}


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
    def test_wifi_with_anchor_is_strong(self):
        fp = strong_fingerprint(_wifi(probe_fingerprint=123, fp_anchor="HomeNet"))
        self.assertTrue(fp.startswith("wifi-fp:"))

    def test_wifi_no_probe_is_none(self):
        self.assertIsNone(strong_fingerprint(_wifi()))

    def test_wifi_no_distinctive_anchor_is_none(self):
        # Has an IE hash but no distinctive SSID (only common public networks) →
        # not strongly keyed; the caller falls back to mac: (over-merge-safe).
        self.assertIsNone(strong_fingerprint(_wifi(probe_fingerprint=123, probe_ssids=["attwifi"])))

    def test_ble_service_is_strong(self):
        self.assertTrue(strong_fingerprint(_ble(service_uuids=[0x180D])).startswith("ble-fp:"))

    def test_ble_bare_vendor_is_none(self):
        self.assertIsNone(strong_fingerprint(_ble(company_ids=[0x004C])))

    def test_same_ie_and_anchor_same_key_across_macs(self):
        # Two rotated MACs sharing the IE hash + distinctive anchor collapse to one key.
        a = strong_fingerprint(_wifi(mac="a2:00:00:00:00:01", probe_fingerprint=123, fp_anchor="HomeNet"))
        b = strong_fingerprint(_wifi(mac="a2:00:00:00:00:99", probe_fingerprint=123, fp_anchor="HomeNet"))
        self.assertEqual(a, b)

    def test_different_anchor_different_key_same_ie(self):
        # Same popular IE hash, different distinctive anchors → distinct devices.
        a = strong_fingerprint(_wifi(probe_fingerprint=123, fp_anchor="HomeNetA"))
        b = strong_fingerprint(_wifi(probe_fingerprint=123, fp_anchor="HomeNetB"))
        self.assertNotEqual(a, b)


class TestFingerprintLabel(unittest.TestCase):
    def test_label_for_strong(self):
        self.assertEqual(
            fingerprint_label(_wifi(probe_fingerprint=123, fp_anchor="HomeNet")), "HomeNet")

    def test_empty_for_weak(self):
        self.assertEqual(fingerprint_label(_wifi()), "")

    def test_ble_vendor_label(self):
        self.assertEqual(fingerprint_label(_ble(service_uuids=[0x180D], company_ids=[0x004C])), "Apple")


if __name__ == "__main__":
    unittest.main()


# ---------------------------------------------------------------------------
# contact_identity — tiered DISPLAY identity (strong / medium / weak)
# ---------------------------------------------------------------------------

from modules.device_identity import contact_identity  # noqa: E402


class TestContactIdentity(unittest.TestCase):
    def test_strong_anchor_is_strong_tier_and_matches_scoring_key(self):
        d = _wifi(probe_fingerprint=123, fp_anchor="HomeNet")
        key, conf = contact_identity(d)
        self.assertEqual(conf, "strong")
        # The strong contact key lines up with the scoring fingerprint.
        self.assertEqual(key, strong_fingerprint(d))

    def test_medium_anchor_is_medium_tier(self):
        # No distinctive (strong) anchor, but a less-rare medium anchor is present.
        d = _wifi(probe_fingerprint=456)
        d["fp_anchor_medium"] = "coffeeshop"
        key, conf = contact_identity(d)
        self.assertEqual(conf, "medium")
        self.assertTrue(key.startswith("wifi-fp:"))

    def test_strong_anchor_preferred_over_medium(self):
        d = _wifi(probe_fingerprint=789, fp_anchor="HomeNet")
        d["fp_anchor_medium"] = "coffeeshop"
        key, conf = contact_identity(d)
        self.assertEqual(conf, "strong")
        self.assertEqual(key, strong_fingerprint(d))

    def test_no_anchor_no_ie_is_weak_none(self):
        key, conf = contact_identity(_wifi(probe_fingerprint=None))
        self.assertIsNone(key)
        self.assertEqual(conf, "weak")

    def test_ie_but_no_anchor_is_weak_none(self):
        # IE hash alone would over-merge (groups by stack/model) — not a contact key.
        key, conf = contact_identity(_wifi(probe_fingerprint=999))
        self.assertIsNone(key)
        self.assertEqual(conf, "weak")

    def test_medium_key_stable_across_mac_rotation(self):
        a = _wifi(probe_fingerprint=321, mac="a2:00:00:00:00:aa")
        a["fp_anchor_medium"] = "GymWiFi"
        b = _wifi(probe_fingerprint=321, mac="a2:00:00:00:00:bb")  # rotated MAC
        b["fp_anchor_medium"] = "GymWiFi"
        self.assertEqual(contact_identity(a)[0], contact_identity(b)[0])

    def test_ble_strong_advert_is_strong(self):
        key, conf = contact_identity(_ble(service_uuids=[0x180D], name="Watch"))
        self.assertEqual(conf, "strong")
        self.assertTrue(key.startswith("ble-fp:"))

    def test_ble_bare_vendor_is_weak_none(self):
        key, conf = contact_identity(_ble(company_ids=[0x004C]))  # bare Apple id
        self.assertIsNone(key)
        self.assertEqual(conf, "weak")
