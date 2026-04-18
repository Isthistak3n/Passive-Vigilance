"""MAC address utilities — randomization detection and device fingerprinting."""

import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# Second hex digit of first octet values that indicate locally administered (randomized) MACs
_RANDOMIZED_SECOND_DIGITS = frozenset({"2", "6", "a", "e"})

# Bit mask: bit 1 of first octet = locally administered
_LOCALLY_ADMINISTERED_BIT = 0x02


def normalize_mac(mac: str) -> str:
    """Return *mac* as lowercase colon-separated form (e.g. ``aa:bb:cc:dd:ee:ff``)."""
    cleaned = mac.strip().lower().replace("-", ":").replace(".", ":")
    if len(cleaned) == 12 and ":" not in cleaned:
        cleaned = ":".join(cleaned[i:i+2] for i in range(0, 12, 2))
    return cleaned


def is_randomized_mac(mac: str) -> bool:
    """Return True if *mac* has the locally administered (randomized) bit set.

    The locally administered bit is bit 1 of the first octet.  When set, the
    address was not assigned by the hardware vendor — common in iOS 14+,
    Android 10+, and Windows 10+ random MAC mode.

    Equivalently: the second hex digit of the first octet is 2, 6, A, or E.
    """
    try:
        norm = normalize_mac(mac)
        first_octet = int(norm.split(":")[0], 16)
        return bool(first_octet & _LOCALLY_ADMINISTERED_BIT)
    except (ValueError, IndexError):
        return False


def get_mac_type(mac: str) -> str:
    """Return ``"randomized"`` if the MAC has the locally administered bit set, else ``"static"``."""
    return "randomized" if is_randomized_mac(mac) else "static"


def get_randomization_vendor_hint(mac: str) -> str:
    """Return a platform hint string for a randomized MAC.

    Randomized MACs do not carry vendor-assigned OUI information, so platform
    detection from the MAC address alone is unreliable.  Returns an empty
    string for static (non-randomized) MACs.

    Without additional behavioral context (probe request patterns, timing,
    capabilities), the platform cannot be identified from the MAC alone.
    """
    if not is_randomized_mac(mac):
        return ""
    return "Unknown"


@dataclass
class MACFingerprint:
    """A cluster of randomized MACs that are likely the same physical device."""

    canonical_mac: str              # representative MAC for the group
    all_macs: list = field(default_factory=list)   # all MACs in this cluster
    probe_ssids: list = field(default_factory=list)  # union of probe SSIDs
    avg_rssi: float = 0.0
    device_count: int = 1


def group_by_fingerprint(devices: list) -> list:
    """Cluster randomized MACs that are likely the same physical device.

    Groups are formed when two randomized MACs share at least one probe SSID.
    The ``name`` field and optional ``probe_ssids`` list in each device dict
    are used as the probe set.  MACs with no probe SSIDs are not merged with
    any other MAC.

    Args:
        devices: Device dicts, each with at least ``macaddr``.  Optional fields:
                 ``last_signal`` (dBm float), ``name`` (SSID str),
                 ``probe_ssids`` (list of str).

    Returns:
        List of :class:`MACFingerprint` objects — one per discovered cluster.
    """
    rand_devices = [d for d in devices if is_randomized_mac(d.get("macaddr", ""))]
    if not rand_devices:
        return []

    mac_probes: dict[str, frozenset] = {}
    mac_rssi: dict[str, float] = {}
    for d in rand_devices:
        mac = normalize_mac(d.get("macaddr", ""))
        ssids: set = set()
        name = (d.get("name") or "").strip()
        if name:
            ssids.add(name.lower())
        for s in d.get("probe_ssids", []):
            if s:
                ssids.add(s.strip().lower())
        mac_probes[mac] = frozenset(ssids)
        mac_rssi[mac] = float(d.get("last_signal") or 0.0)

    # Union-find
    parent: dict[str, str] = {mac: mac for mac in mac_probes}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(x: str, y: str) -> None:
        px, py = find(x), find(y)
        if px != py:
            parent[py] = px

    macs = list(mac_probes)
    for i, mac1 in enumerate(macs):
        for mac2 in macs[i + 1:]:
            p1 = mac_probes[mac1]
            p2 = mac_probes[mac2]
            if p1 and p2 and (p1 & p2):
                union(mac1, mac2)

    groups: dict[str, list] = {}
    for mac in macs:
        groups.setdefault(find(mac), []).append(mac)

    fingerprints = []
    for canonical, members in groups.items():
        all_probes: set = set()
        rssi_vals: list = []
        for mac in members:
            all_probes |= set(mac_probes[mac])
            val = mac_rssi[mac]
            if val:
                rssi_vals.append(val)
        avg = sum(rssi_vals) / len(rssi_vals) if rssi_vals else 0.0
        fingerprints.append(MACFingerprint(
            canonical_mac=canonical,
            all_macs=sorted(members),
            probe_ssids=sorted(all_probes),
            avg_rssi=round(avg, 2),
            device_count=len(members),
        ))

    return fingerprints
