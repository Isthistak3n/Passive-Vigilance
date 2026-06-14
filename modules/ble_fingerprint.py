"""Randomization-resistant fingerprint for BLE advertisers (Phase 2 step 2).

A device's Bluetooth address rotates every few minutes, but what it *advertises*
changes far more slowly. This module turns one captured advertisement into a
stable signature so a device's rotating addresses collapse to a single logical
identity — the prerequisite for "is this a new device, or one I've always seen
here?" keyed on identity rather than address (design-entity-fingerprinting.md /
design-ble-advertisement-capture.md).

The hard constraint, mirrored from the WiFi probe-SSID grouping: a *bare vendor
id alone must never merge distinct devices*. Every Apple phone broadcasts company
id 0x004C; grouping on that would fuse the whole room into one entity. So the
signature is only treated as a **strong** (groupable) identity when it carries a
real discriminator beyond the vendor id — an advertised service, service data, a
name, or an appearance. Bare-vendor or empty advertisers fingerprint **weakly**
and are left keyed by address by the scorer, exactly like a WiFi client that
probes no named SSIDs.

``tx_power`` is deliberately excluded from the signature (it is used for the
approaching-signal trend, not identity, and can flap).
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Optional, Protocol

# A small set of common company ids for human-readable labels; everything else
# falls back to a hex label. Not used in the signature itself.
_VENDOR_NAMES = {
    0x004C: "Apple",
    0x0006: "Microsoft",
    0x0075: "Samsung",
    0x00E0: "Google",
    0x0087: "Garmin",
    0x0157: "Huawei",
    0x05A7: "Sonos",
    0x0499: "Ruuvi",
}

# Well-known service-data UUIDs worth labelling.
_SERVICE_DATA_NAMES = {
    0xFEAA: "Eddystone",
    0xFD6F: "ExposureNotification",
}


class _AdvertLike(Protocol):
    company_ids: list[int]
    service_uuids: list[int]
    service_data_uuids: list[int]
    local_name: str
    appearance: Optional[int]


@dataclass(frozen=True)
class BLEFingerprint:
    key: str        # "ble-fp:<12 hex>" — stable across address rotation
    strong: bool    # True when groupable (has a discriminator beyond bare vendor)
    label: str      # human-readable identity for the GUI


def _canonical(advert: _AdvertLike) -> str:
    """Deterministic string of the stable, identity-bearing fields."""
    vendor = ",".join(f"{c:04x}" for c in sorted(set(advert.company_ids)))
    svc = ",".join(f"{u:04x}" for u in sorted(set(advert.service_uuids)))
    svcdata = ",".join(f"{u:04x}" for u in sorted(set(advert.service_data_uuids)))
    appearance = "" if advert.appearance is None else f"{advert.appearance:04x}"
    name = advert.local_name or ""
    return f"v:{vendor}|s:{svc}|d:{svcdata}|a:{appearance}|n:{name}"


def compute_ble_fingerprint(advert: _AdvertLike) -> Optional[BLEFingerprint]:
    """Return a :class:`BLEFingerprint`, or None if the advert carries nothing
    identifying at all (no vendor, services, service data, name, or appearance)."""
    has_vendor = bool(advert.company_ids)
    has_discriminator = bool(
        advert.service_uuids
        or advert.service_data_uuids
        or advert.local_name
        or advert.appearance is not None
    )
    if not has_vendor and not has_discriminator:
        return None

    key = "ble-fp:" + hashlib.blake2b(
        _canonical(advert).encode("utf-8"), digest_size=6
    ).hexdigest()
    return BLEFingerprint(key=key, strong=has_discriminator, label=_label(advert))


def _label(advert: _AdvertLike) -> str:
    """Best human-readable identity: name, else beacon/service-data kind, else vendor."""
    if advert.local_name:
        return advert.local_name
    for uuid in sorted(set(advert.service_data_uuids)):
        if uuid in _SERVICE_DATA_NAMES:
            return _SERVICE_DATA_NAMES[uuid]
    if advert.company_ids:
        cid = sorted(set(advert.company_ids))[0]
        return _VENDOR_NAMES.get(cid, f"vendor 0x{cid:04x}")
    return "BLE"
