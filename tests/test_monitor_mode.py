"""Unit tests for KismetModule.get_interface_status() — subprocess mocked."""

import unittest
from unittest.mock import MagicMock, patch

import modules.kismet  # noqa: F401


_IW_DEV_MONITOR = """\
phy#6
\tInterface wlan1
\t\tifindex 6
\t\twdev 0x600000001
\t\taddr 9c:ef:d5:f6:8d:ac
\t\ttype monitor
\t\tchannel 1 (2412 MHz), width: 20 MHz (no HT), center1: 2412 MHz
\t\ttxpower 6.00 dBm
phy#0
\tInterface wlan0
\t\tifindex 3
\t\ttype managed
"""

_IW_DEV_MANAGED = """\
phy#6
\tInterface wlan1
\t\tifindex 6
\t\ttype managed
\t\ttxpower 6.00 dBm
phy#0
\tInterface wlan0
\t\tifindex 3
\t\ttype managed
"""

_IW_DEV_NO_WLAN1 = """\
phy#0
\tInterface wlan0
\t\tifindex 3
\t\ttype managed
"""


class TestGetInterfaceStatus(unittest.TestCase):

    @patch("modules.kismet.WIFI_MONITOR_INTERFACE", "wlan1")
    @patch("modules.kismet.subprocess.run")
    def test_returns_monitor_when_in_monitor_mode(self, mock_run):
        """get_interface_status() should return is_monitor=True when mode is monitor."""
        from modules.kismet import KismetModule

        mock_run.return_value = MagicMock(stdout=_IW_DEV_MONITOR, returncode=0)

        km = KismetModule()
        status = km.get_interface_status()

        self.assertEqual(status["interface"], "wlan1")
        self.assertEqual(status["mode"], "monitor")
        self.assertTrue(status["is_monitor"])
        self.assertIn("phy", status)

    @patch("modules.kismet.WIFI_MONITOR_INTERFACE", "wlan1")
    @patch("modules.kismet.subprocess.run")
    def test_returns_managed_when_in_managed_mode(self, mock_run):
        """get_interface_status() should return is_monitor=False in managed mode."""
        from modules.kismet import KismetModule

        mock_run.return_value = MagicMock(stdout=_IW_DEV_MANAGED, returncode=0)

        km = KismetModule()
        status = km.get_interface_status()

        self.assertEqual(status["mode"], "managed")
        self.assertFalse(status["is_monitor"])

    @patch("modules.kismet.WIFI_MONITOR_INTERFACE", "wlan1")
    @patch("modules.kismet.subprocess.run")
    def test_returns_safe_defaults_when_interface_missing(self, mock_run):
        """get_interface_status() should return is_monitor=False if interface not found."""
        from modules.kismet import KismetModule

        mock_run.return_value = MagicMock(stdout=_IW_DEV_NO_WLAN1, returncode=0)

        km = KismetModule()
        status = km.get_interface_status()

        self.assertEqual(status["interface"], "wlan1")
        self.assertEqual(status["mode"], "unknown")
        self.assertFalse(status["is_monitor"])

    @patch("modules.kismet.WIFI_MONITOR_INTERFACE", "wlan1")
    @patch("modules.kismet.subprocess.run")
    def test_returns_safe_defaults_when_iw_fails(self, mock_run):
        """get_interface_status() should return safe defaults if iw command fails."""
        from modules.kismet import KismetModule

        mock_run.side_effect = FileNotFoundError("iw not found")

        km = KismetModule()
        status = km.get_interface_status()

        self.assertFalse(status["is_monitor"])
        self.assertEqual(status["mode"], "unknown")

    @patch("modules.kismet.WIFI_MONITOR_INTERFACE", "wlan1")
    @patch("modules.kismet.subprocess.run")
    def test_status_dict_has_required_keys(self, mock_run):
        """get_interface_status() dict must always contain all required keys."""
        from modules.kismet import KismetModule

        mock_run.return_value = MagicMock(stdout=_IW_DEV_MONITOR, returncode=0)

        km = KismetModule()
        status = km.get_interface_status()

        for key in ("interface", "mode", "phy", "is_monitor"):
            self.assertIn(key, status, f"missing key: {key}")


if __name__ == "__main__":
    unittest.main()
