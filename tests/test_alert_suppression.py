"""Tests for modules.alert_suppression — the rotation-stable cooldown-key routing.

The invariant under test: no paged alert is ever keyed on a rotating identity. A
rotation-stable contact keeps its per-identity key (a distinct real device still pages
individually); an un-fingerprintable randomized device collapses into a coarse, bounded
proximity bucket so its address rotations can't defeat the cooldown.
"""
import pytest

from modules import alert_suppression as sup


# --------------------------------------------------------------------------
# rssi_band — coarse proximity bands, placeholder-safe
# --------------------------------------------------------------------------

@pytest.mark.parametrize("dbm,expected", [
    (-20.0, "vclose"),
    (-40.0, "vclose"),   # boundary is inclusive on the strong side
    (-40.1, "close"),
    (-55.0, "close"),
    (-55.1, "far"),
    (-90.0, "far"),
])
def test_rssi_band_breakpoints(dbm, expected):
    assert sup.rssi_band(dbm) == expected


def test_rssi_band_placeholder_and_missing_are_unknown():
    # 0 and None are Kismet placeholders, not distances -> not a band.
    assert sup.rssi_band(0) == "unknown"
    assert sup.rssi_band(0.0) == "unknown"
    assert sup.rssi_band(None) == "unknown"
    assert sup.rssi_band("not-a-number") == "unknown"


def test_rssi_band_accepts_numeric_strings():
    assert sup.rssi_band("-35") == "vclose"


# --------------------------------------------------------------------------
# is_rotating_identity — only a randomized MAC on a mac: key rotates
# --------------------------------------------------------------------------

def test_rotating_identity_only_randomized_mac_keyed():
    assert sup.is_rotating_identity("randomized", "mac:aa:bb:cc:dd:ee:ff") is True


def test_static_mac_is_not_rotating():
    assert sup.is_rotating_identity("static", "mac:aa:bb:cc:dd:ee:ff") is False


def test_fingerprinted_randomized_is_not_rotating():
    # A randomized device WITH a strong content fingerprint is rotation-stable.
    assert sup.is_rotating_identity("randomized", "wifi-fp:deadbeef") is False
    assert sup.is_rotating_identity("randomized", "ble-fp:cafef00d") is False


def test_missing_fingerprint_is_not_rotating():
    # No fingerprint at all can't start with "mac:" -> handled, not crashing.
    assert sup.is_rotating_identity("randomized", "") is False
    assert sup.is_rotating_identity("randomized", None) is False


# --------------------------------------------------------------------------
# cooldown_key — the routing rule (the whole point)
# --------------------------------------------------------------------------

def test_stable_fingerprint_keeps_per_identity_key():
    key = sup.cooldown_key(
        fingerprint="wifi-fp:abc123", mac="aa:bb:cc:dd:ee:ff",
        mac_type="randomized", device_type="Wi-Fi Client", signal=-35,
    )
    assert key == "persist:wifi-fp:abc123"


def test_static_mac_keeps_per_identity_key():
    key = sup.cooldown_key(
        fingerprint="mac:11:22:33:44:55:66", mac="11:22:33:44:55:66",
        mac_type="static", device_type="BTLE", signal=-35,
    )
    assert key == "persist:mac:11:22:33:44:55:66"


def test_missing_fingerprint_falls_back_to_mac():
    key = sup.cooldown_key(
        fingerprint="", mac="11:22:33:44:55:66",
        mac_type="static", device_type="Wi-Fi AP", signal=None,
    )
    assert key == "persist:11:22:33:44:55:66"


def test_rotating_wifi_collapses_to_proximity_bucket():
    key = sup.cooldown_key(
        fingerprint="mac:aa:bb:cc:dd:ee:ff", mac="aa:bb:cc:dd:ee:ff",
        mac_type="randomized", device_type="Wi-Fi Client", signal=-35,
    )
    assert key == "proximity:wifi:vclose"


def test_rotating_ble_collapses_to_ble_bucket():
    key = sup.cooldown_key(
        fingerprint="mac:aa:bb:cc:dd:ee:ff", mac="aa:bb:cc:dd:ee:ff",
        mac_type="randomized", device_type="BTLE", signal=-48,
    )
    assert key == "proximity:ble:close"


def test_rotating_addresses_share_one_bucket_across_rotation():
    # The core property: two DIFFERENT randomized addresses at the same modality/band
    # produce the SAME cooldown key, so the cooldown actually damps them.
    common = dict(mac_type="randomized", device_type="Wi-Fi Client", signal=-30)
    k1 = sup.cooldown_key(fingerprint="mac:aa:aa:aa:aa:aa:aa", mac="aa:aa:aa:aa:aa:aa", **common)
    k2 = sup.cooldown_key(fingerprint="mac:bb:bb:bb:bb:bb:bb", mac="bb:bb:bb:bb:bb:bb", **common)
    assert k1 == k2 == "proximity:wifi:vclose"


def test_two_distinct_fingerprinted_devices_never_share_a_bucket():
    # Over-merge guard the other way: real distinct identities stay distinct.
    k1 = sup.cooldown_key(fingerprint="wifi-fp:1111", mac="a", mac_type="randomized",
                          device_type="Wi-Fi Client", signal=-30)
    k2 = sup.cooldown_key(fingerprint="wifi-fp:2222", mac="b", mac_type="randomized",
                          device_type="Wi-Fi Client", signal=-30)
    assert k1 != k2


def test_bucket_space_is_bounded():
    # The self-bounding property: every rotating contact maps into a small fixed set
    # of buckets (<= modalities x bands), regardless of how many addresses appear.
    keys = set()
    for i in range(500):
        mac = f"{i:012x}"
        mac = ":".join(mac[j:j+2] for j in range(0, 12, 2))
        for dt in ("Wi-Fi Client", "BTLE"):
            for sig in (-20, -45, -70, None):
                keys.add(sup.cooldown_key(
                    fingerprint="mac:" + mac, mac=mac,
                    mac_type="randomized", device_type=dt, signal=sig))
    # 2 modalities x 4 bands (vclose/close/far/unknown) = 8 possible buckets, no more.
    assert keys <= {
        f"proximity:{m}:{b}"
        for m in ("wifi", "ble")
        for b in ("vclose", "close", "far", "unknown")
    }
    assert len(keys) == 8
