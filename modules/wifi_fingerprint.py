"""Randomization-resistant fingerprint for WiFi clients (Phase 2 step 2, WiFi side).

The WiFi analogue of :mod:`modules.ble_fingerprint`. A WiFi client randomizes its
MAC, but what it *probes for* — the set of named SSIDs it asks about and the
structure of its probe-request information elements (IEs) — changes far more
slowly. This module turns one Kismet device record into a stable signature so a
client's rotating MACs collapse to one logical identity, with the same
``key / strong / label`` shape the BLE fingerprint uses so the scorer can treat
both modalities identically.

Same over-merge safeguard as BLE: a client that probes **no named SSIDs** is left
weak (not groupable) — matching the existing ``mac_utils.group_by_fingerprint``
rule that never merges MACs without probe SSIDs. Kismet's ``probe_fingerprint``
(the IE-set hash) is folded into the key as a *finer* discriminator: it can only
split two same-SSID devices that run different stacks apart, never merge distinct
devices, and it gives an SSID-less client a stable-but-weak handle.

Note (reconciled in step 3 / scoring): this is the conservative *per-device*
signature — it groups devices with the **same** probe set. The existing scorer
union-finds devices sharing **any** SSID; choosing exact-set vs. shared-SSID
clustering is a scoring-layer decision, not a signature one.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class WiFiFingerprint:
    key: str        # "wifi-fp:<12 hex>" — stable across MAC rotation
    strong: bool    # True when groupable (probes at least one named SSID)
    label: str      # human-readable identity for the GUI


def _named_ssids(device: dict) -> list[str]:
    """Non-empty probed SSIDs (the broadcast/wildcard '' is already excluded upstream)."""
    ssids = device.get("probe_ssids") or []
    return sorted({s for s in ssids if isinstance(s, str) and s.strip()})


def compute_wifi_fingerprint(device: dict) -> Optional[WiFiFingerprint]:
    """Return a :class:`WiFiFingerprint`, or None if there is nothing to fingerprint
    (no named probe SSIDs, no IE fingerprint, no advertised name)."""
    ssids = _named_ssids(device)
    probe_fp = device.get("probe_fingerprint")
    # A 0 / None probe_fingerprint is "no IE signature" — treat as absent.
    has_ie = bool(probe_fp)
    name = (device.get("name") or "").strip()

    if not ssids and not has_ie and not name:
        return None

    canonical = f"p:{','.join(ssids)}|f:{probe_fp if has_ie else ''}"
    key = "wifi-fp:" + hashlib.blake2b(canonical.encode("utf-8"), digest_size=6).hexdigest()
    return WiFiFingerprint(key=key, strong=bool(ssids), label=_label(device, ssids, name))


def _label(device: dict, ssids: list[str], name: str) -> str:
    """Best human-readable identity: a probed network, else the device/AP name,
    else the manufacturer, else the device type."""
    if ssids:
        extra = f" +{len(ssids) - 1}" if len(ssids) > 1 else ""
        return f"{ssids[0]}{extra}"
    if name:
        return name
    manuf = (device.get("manuf") or "").strip()
    if manuf and manuf.lower() != "unknown":
        return manuf
    return device.get("type") or "Wi-Fi"
