"""aircraft_registry — connectivity-adaptive ICAO → registration/type resolution.

ACARS messages carry a tail number; to tie a decoded message to a live ADS-B contact
we need the contact's registration for its ICAO hex. Per the project's
augments-never-gates / opsec philosophy (cf. WiGLE §10, the offline basemap):

- **Offline (default, zero network):** a local SQLite built OFF-NODE from a public
  aircraft database (``scripts/build_aircraft_registry.py``) and copied on. Pure local
  lookups — a deployed node never reveals which airframes it's investigating.
- **Online (augment when an enrich callable is provided):** adsb.lol enrichment
  (``ADSBModule.enrich_aircraft``) wins when reachable; falls back to the offline DB.

Both are cached per ICAO (including negatives) so each airframe resolves at most once.
Correlation never *depends* on this: a missing registration just falls back to
callsign ↔ flight-id matching at the call site.
"""

import logging
import os
import sqlite3
from typing import Awaitable, Callable, Optional

logger = logging.getLogger(__name__)

DEFAULT_DB_PATH = "data/registry/aircraft.sqlite"


class AircraftRegistry:
    """Local ICAO→registration store with an optional online-enrich augmentation."""

    def __init__(self, db_path: Optional[str] = None) -> None:
        self.db_path = (db_path or os.getenv("AIRCRAFT_REGISTRY_DB") or DEFAULT_DB_PATH).strip()
        self._conn: Optional[sqlite3.Connection] = None
        self._cache: dict[str, dict] = {}
        if self.db_path and os.path.isfile(self.db_path):
            try:
                self._conn = sqlite3.connect(
                    f"file:{self.db_path}?mode=ro", uri=True, check_same_thread=False
                )
                self._conn.row_factory = sqlite3.Row
                logger.info("Aircraft registry: offline DB at %s", self.db_path)
            except sqlite3.Error as exc:
                logger.warning("Aircraft registry: %s unreadable (%s) — offline lookups off",
                               self.db_path, exc)
                self._conn = None
        else:
            logger.info("Aircraft registry: no offline DB at %s — build one off-node with "
                        "scripts/build_aircraft_registry.py (online enrich still works if "
                        "ADSBXLOL_API_KEY is set)", self.db_path)

    @property
    def offline_available(self) -> bool:
        return self._conn is not None

    def lookup(self, icao: str) -> Optional[dict]:
        """Offline-only lookup → {registration, aircraft_type, operator} or None."""
        if self._conn is None or not icao:
            return None
        try:
            row = self._conn.execute(
                "SELECT registration, aircraft_type, operator FROM aircraft WHERE icao=?",
                (icao.lower(),),
            ).fetchone()
        except sqlite3.Error as exc:
            logger.debug("registry lookup error for %s: %s", icao, exc)
            return None
        if row is None or not row["registration"]:
            return None
        return {
            "registration": row["registration"],
            "aircraft_type": row["aircraft_type"] or "",
            "operator": row["operator"] or "",
        }

    async def resolve(
        self,
        icao: str,
        online_enrich: Optional[Callable[[str], Awaitable[dict]]] = None,
    ) -> dict:
        """Connectivity-adaptive resolve → a (possibly empty) dict, cached per ICAO.

        ``online_enrich(icao)`` (e.g. ``ADSBModule.enrich_aircraft``) is awaited when
        provided and wins if it yields a registration; otherwise the offline DB is
        used. Pass ``None`` (e.g. no API key) to stay fully offline — no network.
        """
        if not icao:
            return {}
        key = icao.lower()
        if key in self._cache:
            return self._cache[key]

        rec: dict = {}
        if online_enrich is not None:
            try:
                enr = await online_enrich(icao)
            except Exception as exc:
                logger.debug("online enrich failed for %s: %s", icao, exc)
                enr = {}
            if enr and enr.get("registration"):
                rec = {
                    "registration": enr.get("registration", ""),
                    "aircraft_type": enr.get("aircraft_type", ""),
                    "operator": enr.get("operator", ""),
                    "military": bool(enr.get("military", False)),
                    "source": "online",
                }
        if not rec:
            off = self.lookup(icao)
            if off:
                rec = {**off, "source": "offline"}

        self._cache[key] = rec  # cache negatives too (empty dict)
        return rec

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except sqlite3.Error:
                pass
            self._conn = None
