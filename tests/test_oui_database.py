"""Tests for the offline OUI manufacturer database in modules.mac_utils.

Uses a small inline Wireshark-format ``manuf`` fixture so the tests never depend on
the real ~2 MB file being present on the dev machine or CI.
"""

import pytest

import modules.mac_utils as mac_utils
from modules.mac_utils import (
    OUIDatabase,
    get_manufacturer,
    get_randomization_vendor_hint,
)

# Tab-separated PREFIX[/mask]\tSHORT\tFULL. AC:DE:48 is deliberately subdivided into a
# 24-bit block and finer 28-/36-bit sub-blocks so longest-prefix matching is exercised.
_FIXTURE_LINES = [
    "# a comment line — must be skipped",
    "\t".join(["00:00:0C", "Cisco", "Cisco Systems, Inc"]),
    "\t".join(["AC:DE:48", "VendorL", "Vendor L (MA-L /24)"]),
    "\t".join(["AC:DE:48:0/28", "VendorM", "Vendor M (MA-M /28)"]),
    "\t".join(["AC:DE:48:00:0/36", "VendorS", "Vendor S (MA-S /36)"]),
]


def _write_manuf(tmp_path):
    p = tmp_path / "manuf"
    p.write_text("\n".join(_FIXTURE_LINES) + "\n", encoding="utf-8")
    return str(p)


def _db(tmp_path):
    return OUIDatabase(_write_manuf(tmp_path))


@pytest.fixture
def oui_singleton(tmp_path, monkeypatch):
    """Point the module-level get_manufacturer() singleton at the fixture, and
    restore it afterwards so no other test file sees a polluted database."""
    path = _write_manuf(tmp_path)
    monkeypatch.setenv("OUI_MANUF_PATH", path)   # auto-reverted by monkeypatch
    saved = mac_utils._oui_db
    mac_utils._oui_db = None                     # force a fresh load from the fixture
    yield path
    mac_utils._oui_db = saved                    # restore the original singleton


# ---------------------------------------------------------------------------
# OUIDatabase.lookup — the three prefix lengths + longest-prefix matching
# ---------------------------------------------------------------------------

def test_lookup_24bit_ma_l(tmp_path):
    assert _db(tmp_path).lookup("00:00:0c:11:22:33") == "Cisco"


def test_lookup_28bit_ma_m_wins_over_24bit(tmp_path):
    db = _db(tmp_path)
    # In the AC:DE:48:0/28 sub-block → the more-specific MA-M vendor, not the /24.
    assert db.lookup("ac:de:48:0f:11:22") == "VendorM"
    # Outside the /28 sub-block → falls back to the /24 registrant.
    assert db.lookup("ac:de:48:ff:11:22") == "VendorL"


def test_lookup_36bit_ma_s_wins_over_shorter(tmp_path):
    # In the AC:DE:48:00:0/36 sub-block → the most-specific MA-S vendor.
    assert _db(tmp_path).lookup("ac:de:48:00:01:22") == "VendorS"


def test_lookup_unknown_prefix_returns_empty(tmp_path):
    assert _db(tmp_path).lookup("ff:ff:ff:00:00:00") == ""


def test_lookup_missing_file_is_graceful(tmp_path):
    db = OUIDatabase(str(tmp_path / "does-not-exist"))
    assert db.lookup("00:00:0c:11:22:33") == ""   # no exception, empty result


# ---------------------------------------------------------------------------
# get_manufacturer() convenience wrapper + vendor-hint integration
# ---------------------------------------------------------------------------

def test_get_manufacturer_matches_direct_lookup(oui_singleton):
    direct = OUIDatabase(oui_singleton).lookup("00:00:0c:11:22:33")
    assert get_manufacturer("00:00:0c:11:22:33") == direct == "Cisco"


def test_randomized_mac_hint_still_unknown(oui_singleton):
    # Randomized MAC (locally-administered bit set) has no meaningful OUI.
    assert get_randomization_vendor_hint("02:ab:cd:ef:01:23") == "Unknown"


def test_static_mac_hint_now_returns_vendor(oui_singleton):
    # New behavior: a static MAC resolves to its real vendor via the OUI database.
    assert get_randomization_vendor_hint("00:00:0c:11:22:33") == "Cisco"
