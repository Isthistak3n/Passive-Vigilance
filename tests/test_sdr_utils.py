"""Unit tests for modules/sdr_utils.py — RTL-SDR presence detection + ID source."""

import unittest
from unittest.mock import MagicMock, patch

from modules import sdr_utils

_LSUSB_WITH_RTL = (
    "Bus 001 Device 003: ID 7392:c611 Edimax Bluetooth Adapter\n"
    "Bus 001 Device 005: ID 0bda:2838 Realtek Semiconductor Corp. RTL2838 DVB-T\n"
)
_LSUSB_NO_RTL = (
    "Bus 001 Device 003: ID 7392:c611 Edimax Bluetooth Adapter\n"
    "Bus 002 Device 002: ID 0bda:8812 Realtek RTL8812AU WLAN Adapter\n"
)


class TestIsRtlSdrPresent(unittest.TestCase):

    @patch("modules.sdr_utils.subprocess.run")
    def test_detects_known_rtl_sdr(self, mock_run):
        mock_run.return_value = MagicMock(stdout=_LSUSB_WITH_RTL)
        self.assertTrue(sdr_utils.is_rtl_sdr_present())

    @patch("modules.sdr_utils.subprocess.run")
    def test_absent_when_no_rtl_sdr(self, mock_run):
        mock_run.return_value = MagicMock(stdout=_LSUSB_NO_RTL)
        self.assertFalse(sdr_utils.is_rtl_sdr_present())

    @patch("modules.sdr_utils.subprocess.run", side_effect=FileNotFoundError("lsusb"))
    def test_missing_lsusb_returns_false(self, _mock_run):
        self.assertFalse(sdr_utils.is_rtl_sdr_present())


class TestRtlSdrIdSource(unittest.TestCase):
    """The vendor:product set must stay derived from the components (single source)."""

    def test_combined_ids_match_components(self):
        expected = {f"{sdr_utils.RTL_SDR_VENDOR}:{p}" for p in sdr_utils.RTL_SDR_PRODUCTS}
        self.assertEqual(set(sdr_utils.get_rtl_sdr_usb_ids()), expected)

    def test_known_product_present(self):
        self.assertIn("2838", sdr_utils.RTL_SDR_PRODUCTS)
        self.assertEqual(sdr_utils.RTL_SDR_VENDOR, "0bda")


if __name__ == "__main__":
    unittest.main()
