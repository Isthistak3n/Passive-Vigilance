"""alert_suppression — keep every paged alert on a rotation-stable cooldown key.

The alert rate limiter (:class:`modules.alerts.RateLimiter`) damps repeat sends by
holding a per-key cooldown. That only works if the key is *stable* for one logical
contact. A randomized-MAC device that exposes no strong content fingerprint is keyed
``mac:<addr>`` (the scoring fallback), and its address rotates every few minutes — so
keying the cooldown on it hands every rotation a fresh bucket and defeats the limiter
by construction (the #193 class of bug, in a corner the fingerprint keying can't reach
because there is no fingerprint to key on).

Almost every path already avoids this: novelty and off-schedule are suppressed at
source for such devices (the #138 guard in :mod:`modules.fixed_scoring`), and everything
else sits below the paging bar. The one path that still pages an un-fingerprintable
device is the **egregious-during-baseline safety net** (design 5.2, ``force_page``): a
device physically in the operator's space must alert *even though* it randomizes and
*even though* it scores below the bar. That is the correct behaviour — but on a dense
node during the learning window, many such close devices, each rotating its MAC every
few minutes and re-evaluated every poll, would each page per rotation.

The invariant this module enforces: **no paged alert is ever keyed on a rotating
identity.** A rotation-stable identity (a real static MAC, or a ``wifi-fp:``/``ble-fp:``
content fingerprint) keeps its per-identity cooldown key, unchanged. An un-fingerprintable
*randomized* device collapses instead into a **coarse proximity bucket**
``proximity:<wifi|ble>:<rssi-band>`` — "one or more close, un-trackable devices are
present" pages once per band per cooldown window, no matter how many addresses rotate
through it. The bucket space is deliberately tiny (2 modalities x 4 bands = 8 buckets),
so it is self-bounding — the coarse band count is the ceiling, no separate limiter needed.

Pure functions, no I/O and no state — the orchestrator computes the key at the send site
and hands it to the existing rate limiter.
"""
from __future__ import annotations

from typing import Optional

# RSSI is negative dBm; less negative = physically closer. Bands are coarse on purpose
# — the point is to collapse many un-trackable close contacts into a handful of honest
# proximity buckets, not to distinguish them. Breakpoints roughly track the Wi-Fi
# egregious presets (-30/-40/-50): "vclose" is in-the-room strong, "far" is below any
# egregious bar (a rotating device only reaches a send here via the proximity net, so
# in practice the strong bands dominate). A missing/placeholder reading -> "unknown".
_BAND_VCLOSE = -40.0   # >= this: very strong, immediate space
_BAND_CLOSE = -55.0    # [-55, -40): close
#                       < -55: far ; None/0: unknown


def _is_ble(device_type: Optional[str]) -> bool:
    """True if Kismet classifies the device as Bluetooth/BLE (modality selector)."""
    dt = (device_type or "").lower()
    return "btle" in dt or "bluetooth" in dt


def rssi_band(signal: Optional[float]) -> str:
    """Coarse proximity band label for a signal reading.

    ``None`` or ``0`` is a Kismet placeholder (no real measurement — see the project's
    zero-RSSI note), not a distance, so it maps to ``"unknown"`` rather than a band.
    """
    if signal is None:
        return "unknown"
    try:
        dbm = float(signal)
    except (TypeError, ValueError):
        return "unknown"
    if dbm == 0.0:
        return "unknown"
    if dbm >= _BAND_VCLOSE:
        return "vclose"
    if dbm >= _BAND_CLOSE:
        return "close"
    return "far"


def is_rotating_identity(mac_type: Optional[str], fingerprint: Optional[str]) -> bool:
    """True if this contact has no rotation-stable key: a *randomized* MAC that fell
    back to a ``mac:`` scoring key (no strong ``wifi-fp:``/``ble-fp:`` fingerprint), so
    its identity changes every address rotation and can't anchor a cooldown."""
    return mac_type == "randomized" and (fingerprint or "").startswith("mac:")


def cooldown_key(
    *,
    fingerprint: Optional[str],
    mac: str,
    mac_type: Optional[str],
    device_type: Optional[str],
    signal: Optional[float],
) -> str:
    """The rate-limiter cooldown key for a WiFi/BT persistence alert.

    Rotation-stable contacts (static MAC, or ``wifi-fp:``/``ble-fp:`` fingerprint) keep
    their per-identity key ``persist:<fingerprint-or-mac>`` — unchanged from before, so
    a distinct real device still pages individually. An un-fingerprintable *randomized*
    device collapses into a coarse ``proximity:<modality>:<band>`` bucket so its
    address rotations share one cooldown instead of one page each.
    """
    if is_rotating_identity(mac_type, fingerprint):
        modality = "ble" if _is_ble(device_type) else "wifi"
        return f"proximity:{modality}:{rssi_band(signal)}"
    return f"persist:{fingerprint or mac}"
