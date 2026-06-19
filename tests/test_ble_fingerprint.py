"""Unit tests for modules/ble_fingerprint.py — the randomization-resistant signature."""
import unittest
from types import SimpleNamespace

from modules.ble_fingerprint import compute_ble_fingerprint


def _adv(company_ids=None, service_uuids=None, service_data_uuids=None,
         local_name="", appearance=None, service_uuids_128=None,
         solicited_uuids=None, solicited_uuids_128=None, mfg_structures=None):
    return SimpleNamespace(
        company_ids=company_ids or [],
        service_uuids=service_uuids or [],
        service_data_uuids=service_data_uuids or [],
        local_name=local_name,
        appearance=appearance,
        service_uuids_128=service_uuids_128 or [],
        solicited_uuids=solicited_uuids or [],
        solicited_uuids_128=solicited_uuids_128 or [],
        mfg_structures=mfg_structures or [],
    )


class TestStrength(unittest.TestCase):

    def test_service_uuid_makes_it_strong(self):
        fp = compute_ble_fingerprint(_adv(company_ids=[0x004C], service_uuids=[0x180D]))
        self.assertIsNotNone(fp)
        self.assertTrue(fp.strong)

    def test_bare_vendor_is_weak_not_groupable(self):
        # every Apple phone sends 0x004C with no other content — must NOT be groupable
        fp = compute_ble_fingerprint(_adv(company_ids=[0x004C]))
        self.assertIsNotNone(fp)
        self.assertFalse(fp.strong)

    def test_name_alone_is_strong(self):
        fp = compute_ble_fingerprint(_adv(local_name="MyBand"))
        self.assertTrue(fp.strong)

    def test_appearance_alone_is_strong(self):
        fp = compute_ble_fingerprint(_adv(appearance=0x0180))
        self.assertTrue(fp.strong)

    def test_completely_empty_returns_none(self):
        self.assertIsNone(compute_ble_fingerprint(_adv()))

    def test_mfg_type_structure_makes_vendor_strong(self):
        # Apple with a message-type prefix (not bare company id) is now groupable
        fp = compute_ble_fingerprint(_adv(company_ids=[0x004C], mfg_structures=["004c:t10"]))
        self.assertTrue(fp.strong)

    def test_bare_mfg_structure_stays_weak(self):
        # company id with no type byte == bare vendor — must NOT become groupable
        fp = compute_ble_fingerprint(_adv(company_ids=[0x004C], mfg_structures=["004c"]))
        self.assertFalse(fp.strong)

    def test_solicited_uuid_makes_it_strong(self):
        self.assertTrue(compute_ble_fingerprint(_adv(solicited_uuids=[0xFD6F])).strong)

    def test_service_uuid128_makes_it_strong(self):
        fp = compute_ble_fingerprint(_adv(company_ids=[0x004C], service_uuids_128=["0102" * 8]))
        self.assertTrue(fp.strong)

    def test_mfg_type_changes_key(self):
        a = compute_ble_fingerprint(_adv(company_ids=[0x004C], mfg_structures=["004c:t10"]))
        b = compute_ble_fingerprint(_adv(company_ids=[0x004C], mfg_structures=["004c:t07"]))
        self.assertNotEqual(a.key, b.key)


class TestStability(unittest.TestCase):

    def test_same_content_same_key_across_addresses(self):
        # the key is derived from content only, so two sightings (different MACs)
        # of the same device produce the same fingerprint
        a = _adv(company_ids=[0x004C], service_uuids=[0x180D, 0x180F], local_name="Band")
        b = _adv(company_ids=[0x004C], service_uuids=[0x180F, 0x180D], local_name="Band")
        self.assertEqual(compute_ble_fingerprint(a).key, compute_ble_fingerprint(b).key)

    def test_different_services_different_key(self):
        a = compute_ble_fingerprint(_adv(company_ids=[0x004C], service_uuids=[0x180D]))
        b = compute_ble_fingerprint(_adv(company_ids=[0x004C], service_uuids=[0x1234]))
        self.assertNotEqual(a.key, b.key)

    def test_key_has_expected_prefix(self):
        fp = compute_ble_fingerprint(_adv(service_uuids=[0x180D]))
        self.assertTrue(fp.key.startswith("ble-fp:"))


class TestLabel(unittest.TestCase):

    def test_local_name_preferred(self):
        fp = compute_ble_fingerprint(_adv(company_ids=[0x004C], local_name="Pixel Buds"))
        self.assertEqual(fp.label, "Pixel Buds")

    def test_eddystone_from_service_data(self):
        fp = compute_ble_fingerprint(_adv(service_data_uuids=[0xFEAA]))
        self.assertEqual(fp.label, "Eddystone")

    def test_known_vendor_name(self):
        fp = compute_ble_fingerprint(_adv(company_ids=[0x004C], service_uuids=[0x180D]))
        self.assertEqual(fp.label, "Apple")

    def test_unknown_vendor_hex_fallback(self):
        fp = compute_ble_fingerprint(_adv(company_ids=[0x1234], service_uuids=[0x180D]))
        self.assertEqual(fp.label, "vendor 0x1234")


class TestTxPowerExcluded(unittest.TestCase):

    def test_tx_power_not_in_signature(self):
        # adverts identical except for a (hypothetical) tx_power attribute must key
        # the same — tx_power is used for proximity, not identity
        a = _adv(service_uuids=[0x180D])
        b = _adv(service_uuids=[0x180D])
        a.tx_power = -59
        b.tx_power = -72
        self.assertEqual(compute_ble_fingerprint(a).key, compute_ble_fingerprint(b).key)


if __name__ == "__main__":
    unittest.main()
