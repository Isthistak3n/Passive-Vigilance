"""Shared device-identity resolution for both scoring engines.

A randomized device defeats MAC-based tracking, but its *advertised content*
changes slowly. This module centralizes the rotation-resistant fingerprint logic
so fixed and mobile scoring resolve identity the same way: WiFi clients by their
probed SSIDs + IE hash (``wifi-fp:``), BLE advertisers by their vendor / services
/ name advertisement (``ble-fp:``), via :mod:`modules.wifi_fingerprint` /
:mod:`modules.ble_fingerprint`.

It exposes the *strong* fingerprint (the part worth grouping on) and leaves the
MAC fallback to each caller, because the two engines key slightly differently:
``FixedScoring`` uses ``mac:<mac>`` keys, the mobile ``PersistenceEngine`` uses the
raw MAC. A weak fingerprint (bare vendor id, no named SSID) returns None so
distinct devices are never merged — the safeguard the signature modules apply.
"""
from __future__ import annotations

from types import SimpleNamespace
from typing import Optional

from modules.ble_fingerprint import compute_ble_fingerprint
from modules.wifi_fingerprint import compute_wifi_fingerprint

_BLE_ADVERT_KEYS = ("company_ids", "service_uuids", "service_data_uuids", "appearance")


def is_ble_device(device: dict) -> bool:
    """True if the record is a BLE advertiser — by its phy/type or by carrying
    advertisement fields (populated once the BLE scanner feeds the pipeline)."""
    kind = f"{device.get('type', '')} {device.get('phyname', '')}"
    if "BTLE" in kind or "BLE" in kind or "Bluetooth" in kind:
        return True
    return any(device.get(k) for k in _BLE_ADVERT_KEYS)


def _ble_advert_view(device: dict) -> SimpleNamespace:
    """Adapt a device record's flat advertisement fields into the attribute view
    :func:`compute_ble_fingerprint` expects. Absent fields read as empty, so a
    Kismet BLE record (no advertisement payload) fingerprints weakly -> None."""
    return SimpleNamespace(
        company_ids=device.get("company_ids") or [],
        service_uuids=device.get("service_uuids") or [],
        service_data_uuids=device.get("service_data_uuids") or [],
        local_name=device.get("name") or "",
        appearance=device.get("appearance"),
        service_uuids_128=device.get("service_uuids_128") or [],
        solicited_uuids=device.get("solicited_uuids") or [],
        solicited_uuids_128=device.get("solicited_uuids_128") or [],
        mfg_structures=device.get("mfg_structures") or [],
    )


def _fingerprint(device: dict):
    """The BLE or WiFi fingerprint object for *device*, by modality (or None)."""
    if is_ble_device(device):
        return compute_ble_fingerprint(_ble_advert_view(device))
    return compute_wifi_fingerprint(device)


def strong_fingerprint(device: dict) -> Optional[str]:
    """The rotation-stable fingerprint key (``wifi-fp:`` / ``ble-fp:``) for a device
    with a STRONG fingerprint, else None. Callers apply their own MAC fallback."""
    fp = _fingerprint(device)
    return fp.key if (fp is not None and fp.strong) else None


def fingerprint_label(device: dict) -> str:
    """Human-readable identity for a strongly-fingerprinted device (vendor, beacon
    kind, or probed SSID), else ``""`` — used to label the GUI's collapsed row."""
    fp = _fingerprint(device)
    return fp.label if (fp is not None and fp.strong) else ""
