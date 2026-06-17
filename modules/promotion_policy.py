"""promotion_policy — the swappable criterion for rolling-baseline promotion (P3).

A fixed node freezes its baseline, then flags any device first seen after the
freeze as novel forever (design 5.5 / roadmap P3). That floods over a multi-day
run as benign newcomers (a neighbour's new phone, a visitor that stays) never
stop reading novel. Rolling adaptation lets a device *earn* its way into the
baseline by demonstrated sustained presence — and lose it again on prolonged
absence.

This module is the **promotion decision seam**. The promotion criterion is a
swappable :class:`PromotionPolicy` (one method): :class:`SustainedPresencePolicy`
ships; :class:`ConsistencyPatternPolicy` is the designed-for, not-yet-implemented
stronger criterion that slots in behind the same interface with a one-class change
— the sweep, the store write path, and the schema are untouched.

Demotion is deliberately NOT a policy: it is a fixed mechanism (a promoted entry
absent past the demotion window drops out), because the **slow-in / fast-out**
asymmetry is a design invariant, not a tunable — it is the defense against a
patient adversary who promotes, then leaves just under a symmetric window.

Operator posture (:func:`resolve_adaptation`) selects PARAMETERS only, never
mechanism, so it stays valid when the consistency policy is swapped in. ``off`` is
the default and the fail-safe: unset / unrecognised / garbage posture resolves to
``off`` (frozen forever — today's behaviour); a *recognised* posture whose
parameters are misconfigured fails loud.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Mapping, Optional

# Postures select parameter bundles only. "off" is the fail-safe default and is
# handled by absence from this tuple (anything not here resolves to off).
_TUNABLE_POSTURES = ("conservative", "permissive")


@dataclass(frozen=True)
class AdaptationParams:
    """The soak-tunable knobs a posture selects. Mechanism never lives here."""

    promo_min_days: int               # distinct days present (post-freeze) to promote
    promo_min_span: timedelta         # first->last post-freeze sighting must span this
    promo_min_distinct_hours: int     # distinct hours-of-day present (spread, not volume)
    demote_after: timedelta           # absence past this demotes a promoted entry

    def validate(self) -> None:
        """Enforce the slow-in / fast-out invariant. Fail loud if violated.

        Demotion must be strictly faster than promotion — a promoted entry has to
        fall out well before it could have been re-promoted, or a patient adversary
        could park just under a symmetric window. This is an invariant, not a
        tunable, so a misconfigured posture raises rather than running unsafely.
        """
        if self.demote_after >= self.promo_min_span:
            raise ValueError(
                "adaptation params violate the slow-in/fast-out invariant: "
                f"demote_after={self.demote_after} must be < "
                f"promo_min_span={self.promo_min_span}"
            )
        if self.promo_min_days < 1 or self.promo_min_distinct_hours < 1:
            raise ValueError("promo_min_days and promo_min_distinct_hours must be >= 1")


# Illustrative, soak-tunable presets. Each obeys slow-in/fast-out (demote_after <
# promo_min_span). Calibrated for real against the post-freeze FP read; these are
# starting points, not final.
_POSTURE_PRESETS = {
    "conservative": AdaptationParams(
        promo_min_days=5, promo_min_span=timedelta(days=5),
        promo_min_distinct_hours=6, demote_after=timedelta(hours=24),
    ),
    "permissive": AdaptationParams(
        promo_min_days=3, promo_min_span=timedelta(days=3),
        promo_min_distinct_hours=4, demote_after=timedelta(hours=48),
    ),
}

# Per-field env overrides (soak tuning). A present-but-malformed override under a
# recognised posture fails loud — a deliberate posture must not silently run with
# default windows.
_OVERRIDES = {
    "promo_min_days": ("ADAPT_PROMO_MIN_DAYS", "int"),
    "promo_min_span": ("ADAPT_PROMO_MIN_SPAN_HOURS", "hours"),
    "promo_min_distinct_hours": ("ADAPT_PROMO_MIN_DISTINCT_HOURS", "int"),
    "demote_after": ("ADAPT_DEMOTE_AFTER_HOURS", "hours"),
}


def _apply_overrides(params: AdaptationParams, env: Mapping[str, str]) -> AdaptationParams:
    values = {
        "promo_min_days": params.promo_min_days,
        "promo_min_span": params.promo_min_span,
        "promo_min_distinct_hours": params.promo_min_distinct_hours,
        "demote_after": params.demote_after,
    }
    for field, (var, kind) in _OVERRIDES.items():
        raw = env.get(var)
        if raw is None or not str(raw).strip():
            continue
        try:
            if kind == "int":
                values[field] = int(raw)
            else:  # hours -> timedelta
                values[field] = timedelta(hours=float(raw))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"malformed {var}={raw!r}: {exc}") from exc
    return AdaptationParams(**values)


def resolve_adaptation(env: Mapping[str, str]) -> "tuple[str, Optional[AdaptationParams]]":
    """Resolve ``(posture, params)`` from the environment.

    - Absent / ``off`` / unrecognised / garbage posture -> ``("off", None)`` —
      the fail-safe: the baseline stays frozen forever (today's behaviour).
    - A recognised posture (``conservative`` / ``permissive``) returns its preset
      with any env overrides applied and the slow-in/fast-out invariant validated.
      A malformed override or invariant violation **raises** (fail loud) — a
      deliberate posture must never silently degrade.
    """
    raw = (env.get("ADAPTATION_POSTURE") or "").strip().lower()
    if raw not in _TUNABLE_POSTURES:
        return "off", None
    params = _apply_overrides(_POSTURE_PRESETS[raw], env)
    params.validate()
    return raw, params


@dataclass(frozen=True)
class PresenceRecord:
    """The banked-presence view of one promotion candidate, built by the sweep
    from BaselineStore's post-freeze adaptation accumulator. A policy reads only
    these fields, never the store — so a new policy needs no new plumbing."""

    key: str                    # rotation-stable fingerprint (wifi-fp:/ble-fp:) or mac:
    mac_type: str
    pf_first: datetime          # first post-freeze sighting
    pf_last: datetime           # most recent post-freeze sighting
    distinct_days: int          # distinct UTC days seen post-freeze
    adapt_hour_mask: int        # 24-bit hours-of-day seen post-freeze
    observation_count: int
    now: datetime


class PromotionPolicy(ABC):
    """The promotion criterion. One method: promote or hold."""

    @abstractmethod
    def should_promote(self, rec: PresenceRecord, params: AdaptationParams) -> bool:
        ...


class SustainedPresencePolicy(PromotionPolicy):
    """Ship criterion: promote on sustained, spread-out presence.

    Reads banked presence (distinct days + first->last span + distinct hours-of-day
    spread), never a naive cumulative poll count — so a single busy block (many
    polls, one day) does not trivially qualify. All three conditions must hold.
    """

    def should_promote(self, rec: PresenceRecord, params: AdaptationParams) -> bool:
        span_ok = (rec.pf_last - rec.pf_first) >= params.promo_min_span
        days_ok = rec.distinct_days >= params.promo_min_days
        hours_ok = bin(rec.adapt_hour_mask).count("1") >= params.promo_min_distinct_hours
        return span_ok and days_ok and hours_ok


class ConsistencyPatternPolicy(PromotionPolicy):
    """DESIGNED, NOT IMPLEMENTED — the stronger, harder-to-game criterion.

    Same interface and the same :class:`PresenceRecord` inputs, but judges
    hour-of-day *pattern stability* (a recurring daily shape — low cross-day
    entropy in the hour mask) rather than raw presence. A flat always-on blob or
    a constantly-present device shows sustained presence but not the recurring,
    distinct daily pattern a real resident device does. Slotting it in is a
    one-class change at engine init; the sweep, store write path, and schema are
    untouched. To implement it the :class:`PresenceRecord` would carry the
    per-day hour masks (the accumulator already has the raw material).
    """

    def should_promote(self, rec: PresenceRecord, params: AdaptationParams) -> bool:
        raise NotImplementedError(
            "ConsistencyPatternPolicy is designed-for but not implemented; "
            "SustainedPresencePolicy is the shipping criterion."
        )
