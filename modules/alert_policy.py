"""alert_policy — the single "should this event page?" decision.

Every sensor path used to make its page/suppress decision inline in its own
orchestrator poll loop: the WiFi threshold gate and cooldown-key selection, the
aircraft emergency-vs-of-interest precedence, the drone band key, the Remote ID
key. Auditing or changing alerting policy meant reading every poll method
(#191 — productizing the alerting layer). This module centralizes that decision:
the orchestrator hands each candidate event to :class:`AlertPolicy` and acts on
the verdict. Dispatch, stats, and the durable alert record stay in the
orchestrator — policy decides, it does not send.

The policy composes the two existing pieces rather than replacing them:

- :func:`modules.alert_suppression.cooldown_key` — the rotation-stable /
  proximity-bucketed cooldown key for WiFi/BT persistence pages (the #193 and
  #241 flood containment). The invariant it enforces (no paged alert is ever
  keyed on a rotating identity) is unchanged here.
- :class:`modules.alerts.RateLimiter` — the shared cooldown tracker. The policy
  holds a reference and consults it; keys and windows are byte-identical to the
  pre-consolidation inline code, so persisted cooldown state carries over.

Verdict semantics: exactly one limiter check happens per verdict, and only when
the event is otherwise pageable — a below-threshold event never touches the
limiter (so it cannot start a cooldown window it never paged for).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from modules import alert_suppression

logger = logging.getLogger(__name__)

# Verdict reasons — why the policy did (or did not) page.
PAGE = "page"                          # send it
BELOW_THRESHOLD = "below_threshold"    # under the paging bar; display-only
COOLDOWN = "cooldown"                  # suppressed by an active cooldown window
DISPLAY_ONLY = "display_only"          # path never pages (e.g. transit aircraft)


@dataclass(frozen=True)
class PageVerdict:
    """The policy's answer for one candidate event."""

    page: bool
    reason: str
    key: Optional[str] = None  # the cooldown key consulted (None if none was)


class AlertPolicy:
    """Central page/suppress decision for every alerting path.

    Holds the paging thresholds and cooldown windows that used to be scattered
    across the orchestrator's poll methods. All methods are async because the
    decision may consult the (asyncio-locked) rate limiter.
    """

    def __init__(
        self,
        rate_limiter,
        *,
        wifi_page_min_score: float,
        egregious_cooldown_seconds: float,
        wids_cooldown_seconds: float = 600.0,
    ) -> None:
        self._limiter = rate_limiter
        self._wifi_page_min_score = wifi_page_min_score
        self._egregious_cooldown = egregious_cooldown_seconds
        self._wids_cooldown = wids_cooldown_seconds

    # ------------------------------------------------------------------
    # WiFi / BT persistence
    # ------------------------------------------------------------------

    async def wifi_persistence(self, event, *, signal: Optional[float]) -> PageVerdict:
        """Decide a WiFi/BT persistence :class:`DetectionEvent`.

        Threshold gate: a detection below ``WIFI_ALERT_MIN_SCORE`` is shown in
        the WiFi panel but not paged — keeps low-confidence suspicious flags
        visible without drowning the operator. ``force_page`` events
        (egregious-during-baseline, design 5.2) are the deliberate exception: a
        single egregious signal scores 0.5, which never clears the bar, but it
        is a safety-net alert that must page.

        Cooldown key: the rotation-stable scoring fingerprint (``fp:``/``mac:``),
        NOT the raw MAC — keying on the address gave every randomized-MAC
        rotation a fresh bucket and defeated the cooldown entirely (#193: one
        logical contact fired 3,385 alerts across 65 MACs). An un-fingerprintable
        *randomized* device collapses into a coarse ``proximity:<modality>:<band>``
        bucket (#241) so a dense-node learning window can't flood per rotation,
        while every rotation-stable contact keeps its per-identity key.

        Cooldown window: ``force_page`` events use the longer egregious window
        (~once/hour) instead of the limiter default.
        """
        if event.score < self._wifi_page_min_score and not event.force_page:
            return PageVerdict(page=False, reason=BELOW_THRESHOLD)
        key = alert_suppression.cooldown_key(
            fingerprint=event.fingerprint, mac=event.mac,
            mac_type=event.mac_type, device_type=event.device_type,
            signal=signal,
        )
        allowed = await self._limiter.is_allowed(
            key,
            cooldown_override=(self._egregious_cooldown if event.force_page else None),
        )
        return PageVerdict(page=allowed, reason=PAGE if allowed else COOLDOWN, key=key)

    # ------------------------------------------------------------------
    # Aircraft (ADS-B)
    # ------------------------------------------------------------------

    async def aircraft(self, icao: str, *, emergency: bool, of_interest: bool) -> PageVerdict:
        """Decide an ADS-B contact. Emergency wins over of-interest.

        Emergencies rate-limit on their OWN key so a routine of-interest alert
        can never suppress the first emergency page — but a squawk held in view
        must not re-page on every 5 s poll. An of-interest contact (loiterer /
        orbiter / returner, P7) pages on its per-ICAO key; routine transit is
        display-only and never reaches the limiter.
        """
        if emergency:
            key = f"aircraft-emergency:{icao}"
        elif of_interest:
            key = f"aircraft:{icao}"
        else:
            return PageVerdict(page=False, reason=DISPLAY_ONLY)
        allowed = await self._limiter.is_allowed(key)
        return PageVerdict(page=allowed, reason=PAGE if allowed else COOLDOWN, key=key)

    # ------------------------------------------------------------------
    # Drone RF
    # ------------------------------------------------------------------

    async def drone(self, freq_mhz: float) -> PageVerdict:
        """Decide a drone-RF band detection (persistence-gated by the caller)."""
        key = f"drone:{int(freq_mhz)}mhz"
        allowed = await self._limiter.is_allowed(key)
        return PageVerdict(page=allowed, reason=PAGE if allowed else COOLDOWN, key=key)

    # ------------------------------------------------------------------
    # FAA Remote ID
    # ------------------------------------------------------------------

    async def remote_id(self, uas_id: str) -> PageVerdict:
        """Decide a Remote ID detection — one page per UAS ID per window."""
        key = f"remote_id:{uas_id}"
        allowed = await self._limiter.is_allowed(key)
        return PageVerdict(page=allowed, reason=PAGE if allowed else COOLDOWN, key=key)

    # ------------------------------------------------------------------
    # Kismet WIDS
    # ------------------------------------------------------------------

    async def wids(self, header: str, transmitter_mac: str) -> PageVerdict:
        """Decide a consumed Kismet WIDS alert (evil-twin direction, §III.1).

        Keyed per alert type per transmitter BSSID: one CRYPTODROP page per AP
        per window, while a *different* AP raising the same alert still pages.
        An AP's BSSID does not randomize, so the key is rotation-stable by
        nature. The window is longer than the limiter default — a WIDS
        condition (a downgraded AP, a clone) persists for minutes-to-hours and
        Kismet re-raises it; the operator needs one page, not a feed.
        """
        key = f"wids:{header}:{transmitter_mac}"
        allowed = await self._limiter.is_allowed(
            key, cooldown_override=self._wids_cooldown)
        return PageVerdict(page=allowed, reason=PAGE if allowed else COOLDOWN, key=key)
