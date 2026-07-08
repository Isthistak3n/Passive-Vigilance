"""MAC address utilities — randomization detection, OUI vendor lookup, and
device fingerprinting."""

import logging
import os
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

# Bit mask: bit 1 of first octet = locally administered. Equivalently, the second
# hex digit of the first octet is 2, 6, A, or E.
_LOCALLY_ADMINISTERED_BIT = 0x02

# Offline IEEE OUI → vendor database (Wireshark ``manuf`` file). Resolves under
# systemd or by hand; overridable via OUI_MANUF_PATH. The file is ~2 MB and
# git-ignored — bootstrap with scripts/fetch_oui.sh.
_DEFAULT_MANUF_PATH = str(
    Path(__file__).resolve().parent.parent / "data" / "oui" / "manuf"
)


def normalize_mac(mac: str) -> str:
    """Return *mac* as lowercase colon-separated form (e.g. ``aa:bb:cc:dd:ee:ff``)."""
    cleaned = mac.strip().lower().replace("-", ":").replace(".", ":")
    if len(cleaned) == 12 and ":" not in cleaned:
        cleaned = ":".join(cleaned[i:i+2] for i in range(0, 12, 2))
    return cleaned


class OUIDatabase:
    """Offline IEEE OUI → vendor lookup from a Wireshark ``manuf`` file.

    Parses all three IEEE assignment block sizes — 24-bit MA-L (``aa:bb:cc``),
    28-bit MA-M (``aa:bb:cc:d``), and 36-bit MA-S (``aa:bb:cc:dd:e``) — and
    resolves a MAC by **longest-prefix-first** match (36 → 28 → 24), so a MAC in
    a finely-subdivided block gets its specific assignee rather than the parent
    block's registrant.

    The file (~2 MB) is loaded **lazily on the first lookup** — never at import —
    to keep startup fast on the Pi, and the parse is guarded by a lock so the
    asyncio capture path and synchronous test calls share a single load.
    """

    def __init__(self, path: Optional[str] = None) -> None:
        self._path = path or os.getenv("OUI_MANUF_PATH", _DEFAULT_MANUF_PATH)
        # One table per significant-nibble length: 6 (24-bit), 7 (28-bit), 9 (36-bit).
        self._t24: dict[str, str] = {}
        self._t28: dict[str, str] = {}
        self._t36: dict[str, str] = {}
        self._loaded = False
        self._lock = threading.Lock()

    def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        with self._lock:
            if self._loaded:            # another thread loaded it while we waited
                return
            self._load()
            self._loaded = True         # set even on failure — don't retry every call

    def _load(self) -> None:
        try:
            with open(self._path, "r", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    self._parse_line(line)
            logger.info("OUI database loaded from %s (%d/%d/%d MA-L/M/S prefixes)",
                        self._path, len(self._t24), len(self._t28), len(self._t36))
        except FileNotFoundError:
            logger.warning(
                "OUI manuf file not found at %s — offline manufacturer lookup "
                "disabled (run scripts/fetch_oui.sh to download it)", self._path)
        except OSError as exc:  # pragma: no cover - defensive
            logger.warning("OUI manuf file read failed (%s): %s", self._path, exc)

    def _parse_line(self, line: str) -> None:
        """Parse one ``PREFIX[/mask]<TAB>SHORT<TAB>FULL`` record into the tables."""
        line = line.rstrip("\n")
        if not line or line.startswith("#"):
            return
        parts = line.split("\t")
        if len(parts) < 2:
            return
        prefix_field, short = parts[0].strip(), parts[1].strip()
        if not prefix_field or not short:
            return
        if "/" in prefix_field:
            pfx, _, mask_s = prefix_field.partition("/")
            try:
                mask = int(mask_s)
            except ValueError:
                return
        else:
            pfx, mask = prefix_field, 24
        hexdigits = pfx.replace(":", "").replace("-", "").replace(".", "").lower()
        nsig = mask // 4                # significant hex nibbles for this mask
        key = hexdigits[:nsig]
        if len(key) != nsig:            # malformed / truncated prefix line
            return
        if nsig == 9:
            self._t36[key] = short
        elif nsig == 7:
            self._t28[key] = short
        elif nsig == 6:
            self._t24[key] = short

    def lookup(self, mac: str) -> str:
        """Return the short vendor name for *mac*, or ``""`` if not found or the
        database is unavailable. Longest prefix wins (36 → 28 → 24 bit)."""
        self._ensure_loaded()
        hexmac = normalize_mac(mac).replace(":", "")
        if len(hexmac) < 6:
            return ""
        for nsig, table in ((9, self._t36), (7, self._t28), (6, self._t24)):
            hit = table.get(hexmac[:nsig])
            if hit:
                return hit
        return ""


# Process-wide singleton so callers don't manage an instance and the ~2 MB file is
# parsed once. Created lazily (thread-safe) on the first get_manufacturer() call.
_oui_db: Optional[OUIDatabase] = None
_oui_db_lock = threading.Lock()


def get_manufacturer(mac: str) -> str:
    """Resolve *mac* to a vendor name via the shared offline OUI database.

    Returns ``""`` when the vendor is unknown or the database file is not present,
    so a missing file degrades silently rather than raising.
    """
    global _oui_db
    if _oui_db is None:
        with _oui_db_lock:
            if _oui_db is None:
                _oui_db = OUIDatabase()
    return _oui_db.lookup(mac)


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
    """Return a vendor/platform hint for *mac*.

    A **randomized** MAC carries no vendor-assigned OUI (the address is locally
    administered), so there is nothing to resolve — returns ``"Unknown"``.

    A **static** MAC has a real OUI, so it is resolved against the offline OUI
    database (:func:`get_manufacturer`); returns the vendor name, or ``""`` when
    the vendor is unknown or the database file is not installed.
    """
    if is_randomized_mac(mac):
        return "Unknown"
    return get_manufacturer(mac)


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
