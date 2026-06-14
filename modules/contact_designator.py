"""Contact designators for WiFi/BT devices (design-contact-designators.md).

Turns a device into a stable, human-readable track designator ``CLASS-IDENT-#``
(naval/air contact style). This module is the *pure label builder*; the persisted,
stable instance number lives in the entity store (``assign_contact_number``), and
the orchestrator wires the two together when it shapes the WiFi/BT event.
"""
from __future__ import annotations

import re
import zlib

_MAX_IDENT = 18


def class_token(device_type: str) -> str:
    """Short device-class token — the part that makes the Device column redundant."""
    tl = (device_type or "").lower()
    if "btle" in tl or "ble" in tl or "bluetooth" in tl:
        return "BLE"
    if "ap" in tl.split():        # 'Wi-Fi AP', 'Wi-Fi WDS AP'
        return "AP"
    if "bridged" in tl:
        return "BR"
    if "client" in tl:
        return "CLI"
    return "DEV"


def _sanitize(s: str) -> str:
    """One readable token: whitespace/'-' -> '_', drop other punctuation, length-cap."""
    s = re.sub(r"\s+", "_", (s or "").strip()).replace("-", "_")
    s = re.sub(r"[^A-Za-z0-9_]", "", s)
    return s[:_MAX_IDENT]


def ident_token(*, ssid: str = "", label: str = "", manufacturer: str = "",
                fingerprint: str = "", mac: str = "") -> str:
    """Most-identifying name available: network name -> vendor -> short stable token."""
    for cand in (ssid, label, manufacturer):
        tok = _sanitize(cand)
        if tok and tok.lower() != "unknown":
            return tok
    # Nothing descriptive: a short stable token from the fingerprint key / MAC tail.
    # Strip a known prefix (wifi-fp:/ble-fp:/mac:) then keep the hex tail, so a raw
    # MAC yields its last 4 hex (not just its final octet).
    key = fingerprint or mac or ""
    body = key
    if ":" in key and key.split(":", 1)[0] in ("wifi-fp", "ble-fp", "mac"):
        body = key.split(":", 1)[1]
    tail = re.sub(r"[^A-Za-z0-9]", "", body)
    return (tail[-4:] or "x").lower()


def group_key(class_tok: str, ident: str) -> str:
    """The CLASS-IDENT bucket that the instance number is sequential within."""
    return f"{class_tok}-{ident}"


def designator(class_tok: str, ident: str, number: int) -> str:
    return f"{class_tok}-{ident}-{number}"


def fallback_number(identity_key: str) -> int:
    """Stable (but non-sequential) number when no entity store is available — a
    deterministic hash of the identity key, so the designator is still stable."""
    return zlib.crc32((identity_key or "").encode("utf-8")) % 10000
