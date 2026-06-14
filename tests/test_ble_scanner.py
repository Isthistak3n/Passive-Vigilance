"""Unit tests for modules/ble_scanner.py — pure advertisement / HCI parsing.

The socket plumbing needs a real controller and is exercised by
scripts/ble_capture_spike.py; here we test the parsing that turns raw bytes into
structured advertisements, which is where the fingerprint material comes from.
"""
import struct
import unittest

from modules.ble_scanner import (
    parse_advertisement_data,
    parse_hci_advertising_report,
    resolve_hci_index,
)


class TestResolveHciIndex(unittest.TestCase):

    def test_env_override_numeric(self):
        self.assertEqual(resolve_hci_index(preferred=0, available=[0], env="1"), 1)

    def test_env_override_hci_prefixed(self):
        self.assertEqual(resolve_hci_index(env="hci2"), 2)

    def test_preferred_when_no_env(self):
        self.assertEqual(resolve_hci_index(preferred=3, available=[0, 1]), 3)

    def test_lowest_present_when_no_env_or_preferred(self):
        # dongle re-enumerated to hci1 — auto-detect must find it, not hardcode 0
        self.assertEqual(resolve_hci_index(available=[1]), 1)

    def test_lowest_of_multiple(self):
        self.assertEqual(resolve_hci_index(available=[2, 1, 3]), 1)

    def test_default_zero_when_nothing(self):
        self.assertEqual(resolve_hci_index(), 0)


def _ad(*structs: bytes) -> bytes:
    """Join already-encoded AD structures."""
    return b"".join(structs)


def _hci_adv_report(addr: str, data: bytes, rssi: int, addr_type: int = 1,
                    evt_type: int = 0) -> bytes:
    """Build a single-report HCI LE Advertising Report event packet."""
    addr_le = bytes(int(b, 16) for b in addr.split(":"))[::-1]
    body = (
        bytes([0x02, 0x01, evt_type, addr_type])  # subevent, num_reports, evt_type, addr_type
        + addr_le
        + bytes([len(data)])
        + data
        + struct.pack("b", rssi)
    )
    return bytes([0x04, 0x3E, len(body)]) + body


class TestParseAdvertisementData(unittest.TestCase):

    def test_apple_manufacturer_company_id(self):
        # Flags (06) + manufacturer-specific with Apple company id 0x004c
        data = _ad(b"\x02\x01\x06", b"\x05\xff\x4c\x00\x02\x15")
        p = parse_advertisement_data(data)
        self.assertEqual(p.company_ids, [0x004C])

    def test_service_uuid16_list(self):
        # complete list of 16-bit service UUIDs: 0x180D, 0x180F
        data = _ad(b"\x05\x03\x0d\x18\x0f\x18")
        p = parse_advertisement_data(data)
        self.assertEqual(p.service_uuids, [0x180D, 0x180F])

    def test_service_data_name_txpower_appearance(self):
        data = _ad(
            b"\x03\x16\xaa\xfe",          # service data, Eddystone UUID 0xFEAA
            b"\x05\x09ABCD",              # complete local name "ABCD"
            b"\x02\x0a\xf4",              # tx power -12 dBm (0xf4 signed)
            b"\x03\x19\x80\x01",          # appearance 0x0180
        )
        p = parse_advertisement_data(data)
        self.assertEqual(p.service_data_uuids, [0xFEAA])
        self.assertEqual(p.local_name, "ABCD")
        self.assertEqual(p.tx_power, -12)
        self.assertEqual(p.appearance, 0x0180)

    def test_empty(self):
        p = parse_advertisement_data(b"")
        self.assertEqual(p.company_ids, [])
        self.assertEqual(p.local_name, "")

    def test_truncated_length_does_not_raise(self):
        # a length byte claiming more than remains must just stop the walk
        data = b"\x02\x01\x06\x20\xff\x4c"  # last struct claims len 0x20 but only 2 bytes follow
        p = parse_advertisement_data(data)
        # the over-long manufacturer struct is parsed from what's present, not crashed on
        self.assertIsInstance(p.company_ids, list)

    def test_zero_length_terminator_stops(self):
        data = b"\x02\x01\x06\x00\xff\xff\xff"  # 0x00 length terminates
        p = parse_advertisement_data(data)
        self.assertEqual(p.company_ids, [])  # nothing after the terminator is read


class TestParseHciAdvertisingReport(unittest.TestCase):

    def test_parses_address_rssi_and_payload(self):
        data = _ad(b"\x02\x01\x06", b"\x05\xff\x4c\x00\x02\x15")
        pkt = _hci_adv_report("E0:4E:4B:0F:31:50", data, rssi=-58)
        adv = parse_hci_advertising_report(pkt)
        self.assertIsNotNone(adv)
        self.assertEqual(adv.address, "E0:4E:4B:0F:31:50")
        self.assertEqual(adv.rssi, -58)
        self.assertEqual(adv.company_ids, [0x004C])

    def test_non_event_packet_returns_none(self):
        self.assertIsNone(parse_hci_advertising_report(b"\x02\x00\x00\x00"))

    def test_non_le_meta_returns_none(self):
        pkt = bytes([0x04, 0x0E, 0x04, 0x01, 0x00, 0x00, 0x00])  # command complete, not LE meta
        self.assertIsNone(parse_hci_advertising_report(pkt))

    def test_wrong_subevent_returns_none(self):
        pkt = bytes([0x04, 0x3E, 0x04, 0x01, 0x00, 0x00, 0x00])  # LE meta but subevent 0x01
        self.assertIsNone(parse_hci_advertising_report(pkt))

    def test_too_short_returns_none(self):
        self.assertIsNone(parse_hci_advertising_report(b"\x04\x3e"))

    def test_no_payload_advert(self):
        pkt = _hci_adv_report("AA:BB:CC:DD:EE:FF", b"", rssi=-70)
        adv = parse_hci_advertising_report(pkt)
        self.assertIsNotNone(adv)
        self.assertEqual(adv.address, "AA:BB:CC:DD:EE:FF")
        self.assertEqual(adv.rssi, -70)
        self.assertEqual(adv.company_ids, [])


if __name__ == "__main__":
    unittest.main()
