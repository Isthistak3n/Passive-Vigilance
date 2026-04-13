"""Ignore list — filter known-benign devices from alerts."""

import json
import logging
import os
import tempfile
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

_MAC_FILE = "mac_ignore.json"
_SSID_FILE = "ssid_ignore.json"

_MAC_SCHEMA_VERSION = 1
_SSID_SCHEMA_VERSION = 1


def _normalize_mac(mac: str) -> str:
    """Normalize a MAC address to lowercase colon-separated form."""
    cleaned = mac.strip().lower().replace("-", ":").replace(".", ":")
    # Handle compact form (no separators) e.g. aabbccddeeff
    if len(cleaned) == 12 and ":" not in cleaned:
        cleaned = ":".join(cleaned[i:i+2] for i in range(0, 12, 2))
    return cleaned


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class IgnoreList:
    """Persistent filter for known-benign MACs, OUIs, and SSIDs.

    Data is stored in two JSON files inside *data_dir*:
    - ``mac_ignore.json``  — full MACs and OUI prefixes (first 3 octets)
    - ``ssid_ignore.json`` — SSID strings (case-insensitive match)

    Writes are atomic: data is written to a temp file then ``os.rename()``-d
    into place so a crash during save never corrupts existing data.
    """

    def __init__(self, data_dir: str) -> None:
        self._dir = data_dir
        self._mac_path = os.path.join(data_dir, _MAC_FILE)
        self._ssid_path = os.path.join(data_dir, _SSID_FILE)

        # In-memory sets for O(1) lookup
        self._macs: dict[str, dict] = {}   # normalized MAC → entry
        self._ouis: dict[str, dict] = {}   # 3-octet prefix → entry
        self._ssids: dict[str, dict] = {}  # lowercased SSID → entry

        os.makedirs(data_dir, exist_ok=True)
        self.load()

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def load(self) -> None:
        """Load ignore lists from disk. Missing files are treated as empty."""
        self._macs = {}
        self._ouis = {}
        self._ssids = {}

        if os.path.exists(self._mac_path):
            try:
                with open(self._mac_path, encoding="utf-8") as fh:
                    data = json.load(fh)
                for entry in data.get("entries", []):
                    mac = entry.get("mac", "")
                    if entry.get("type") == "oui":
                        self._ouis[mac] = entry
                    else:
                        self._macs[mac] = entry
                logger.debug(
                    "Loaded %d MACs and %d OUIs from ignore list",
                    len(self._macs), len(self._ouis),
                )
            except Exception as exc:
                logger.warning("Could not load MAC ignore list: %s", exc)

        if os.path.exists(self._ssid_path):
            try:
                with open(self._ssid_path, encoding="utf-8") as fh:
                    data = json.load(fh)
                for entry in data.get("entries", []):
                    ssid = entry.get("ssid", "").lower()
                    if ssid:
                        self._ssids[ssid] = entry
                logger.debug("Loaded %d SSIDs from ignore list", len(self._ssids))
            except Exception as exc:
                logger.warning("Could not load SSID ignore list: %s", exc)

    def save(self) -> None:
        """Atomically write both ignore lists to disk."""
        self._save_mac_file()
        self._save_ssid_file()

    def _atomic_write(self, path: str, data: dict) -> None:
        dir_ = os.path.dirname(path)
        fd, tmp = tempfile.mkstemp(dir=dir_, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh, indent=2, ensure_ascii=False)
                fh.write("\n")
            os.rename(tmp, path)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def _save_mac_file(self) -> None:
        entries = list(self._ouis.values()) + list(self._macs.values())
        self._atomic_write(self._mac_path, {
            "version": _MAC_SCHEMA_VERSION,
            "description": "MAC addresses and OUI prefixes to ignore",
            "entries": entries,
        })

    def _save_ssid_file(self) -> None:
        self._atomic_write(self._ssid_path, {
            "version": _SSID_SCHEMA_VERSION,
            "description": "SSIDs to ignore (case-insensitive)",
            "entries": list(self._ssids.values()),
        })

    # ------------------------------------------------------------------
    # Lookup
    # ------------------------------------------------------------------

    def is_ignored_mac(self, mac: str) -> bool:
        """Return True if *mac* matches a full MAC entry or an OUI prefix."""
        norm = _normalize_mac(mac)
        if norm in self._macs:
            return True
        oui = ":".join(norm.split(":")[:3])
        return oui in self._ouis

    def is_ignored_ssid(self, ssid: str) -> bool:
        """Return True if *ssid* matches an ignore-list entry (case-insensitive)."""
        return ssid.lower() in self._ssids

    # ------------------------------------------------------------------
    # Mutations
    # ------------------------------------------------------------------

    def add_mac(self, mac: str, label: str = "") -> None:
        """Add a full MAC address to the ignore list."""
        norm = _normalize_mac(mac)
        self._macs[norm] = {
            "mac":   norm,
            "label": label,
            "added": _now_iso(),
            "type":  "full",
        }
        logger.debug("Added MAC %s (%s) to ignore list", norm, label)

    def add_oui(self, oui: str, label: str = "") -> None:
        """Add an OUI prefix (first 3 octets) to the ignore list."""
        norm = _normalize_mac(oui)
        # Accept either 3-octet prefix or full MAC — keep only first 3 octets
        parts = norm.split(":")
        prefix = ":".join(parts[:3])
        self._ouis[prefix] = {
            "mac":   prefix,
            "label": label,
            "added": _now_iso(),
            "type":  "oui",
        }
        logger.debug("Added OUI %s (%s) to ignore list", prefix, label)

    def add_ssid(self, ssid: str, label: str = "") -> None:
        """Add an SSID to the ignore list (stored and matched case-insensitively)."""
        key = ssid.lower()
        self._ssids[key] = {
            "ssid":  ssid,
            "label": label,
            "added": _now_iso(),
        }
        logger.debug("Added SSID '%s' (%s) to ignore list", ssid, label)

    def remove_mac(self, mac: str) -> bool:
        """Remove a full MAC or OUI prefix. Returns True if something was removed."""
        norm = _normalize_mac(mac)
        parts = norm.split(":")
        if len(parts) <= 3:
            removed = self._ouis.pop(norm, None) or self._ouis.pop(":".join(parts[:3]), None)
        else:
            removed = self._macs.pop(norm, None)
            if removed is None:
                oui = ":".join(parts[:3])
                removed = self._ouis.pop(oui, None)
        if removed:
            logger.debug("Removed MAC/OUI %s from ignore list", norm)
        return removed is not None

    def remove_ssid(self, ssid: str) -> bool:
        """Remove an SSID. Returns True if something was removed."""
        removed = self._ssids.pop(ssid.lower(), None)
        if removed:
            logger.debug("Removed SSID '%s' from ignore list", ssid)
        return removed is not None

    # ------------------------------------------------------------------
    # Bulk import
    # ------------------------------------------------------------------

    def add_from_kismet(self, kismet_devices: list) -> int:
        """Bulk-add all devices from a Kismet ``poll_devices()`` result.

        Each device whose ``macaddr`` is not already listed is added with
        an auto-generated label based on name/manuf fields.
        Returns the count of newly added entries.
        """
        added = 0
        for device in kismet_devices:
            mac = device.get("macaddr", "")
            if not mac:
                continue
            norm = _normalize_mac(mac)
            if norm in self._macs:
                continue
            name = device.get("name") or device.get("manuf") or ""
            self.add_mac(norm, label=name.strip())
            added += 1
        logger.info("Bulk-added %d devices from Kismet to ignore list", added)
        return added

    # ------------------------------------------------------------------
    # Stats
    # ------------------------------------------------------------------

    def stats(self) -> dict:
        """Return counts of entries in the ignore list."""
        return {
            "mac_count":  len(self._macs),
            "oui_count":  len(self._ouis),
            "ssid_count": len(self._ssids),
        }
