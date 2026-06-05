"""ScoringEngine — the strategy interface that forks fixed vs. mobile scoring.

One capture pipeline feeds exactly one ScoringEngine implementation, selected at
startup from ``NODE_MODE`` (see ``main.resolve_node_mode``). The orchestrator
calls :meth:`update` on every Kismet poll result and knows nothing about which
concrete engine is attached.

Implementations:
- :class:`~modules.persistence.PersistenceEngine` (aliased ``MobileScoring``) —
  the existing location-diversity model, unchanged.
- :class:`~modules.fixed_scoring.FixedScoring` — baseline-deviation model.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:  # avoid a runtime circular import with modules.persistence
    from modules.persistence import DetectionEvent


class ScoringEngine(ABC):
    """Common interface for every scoring strategy.

    The public surface is intentionally just two methods: :meth:`update` (the
    orchestrator entry point, matching the call site in
    ``modules/orchestrator.py``) and :meth:`status` (health / GUI framing).
    """

    @abstractmethod
    def update(
        self,
        devices: list,
        *,
        gps_fix: Optional[dict] = None,
    ) -> "list[DetectionEvent]":
        """Ingest one ``poll_devices()`` result; return any ``DetectionEvent``s.

        Must accept ``devices`` positionally and ``gps_fix`` by keyword so the
        single orchestrator call site need not change between engines.
        """
        raise NotImplementedError

    @abstractmethod
    def status(self) -> dict:
        """Return a dict describing engine state for health logging / the GUI."""
        raise NotImplementedError
