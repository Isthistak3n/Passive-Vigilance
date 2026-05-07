"""Shapefile writer — writes sensor session events to GIS formats via geopandas/fiona."""

import json
import logging
import os
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

from modules.kml_writer import KMLWriter

load_dotenv()

logger = logging.getLogger(__name__)

_DEFAULT_OUTPUT_DIR = os.getenv("SESSION_OUTPUT_DIR", "data/sessions")


class ShapefileWriter:
    """Writes detection events as GIS point features using geopandas and fiona.

    Each session gets its own subdirectory under *output_dir*.  Three separate
    layers are written when data is present:

    - ``detections_wifi.shp``     — persistence engine WiFi/BT detections
    - ``detections_aircraft.shp`` — ADS-B aircraft tracks
    - ``detections_drone.shp``    — drone RF hits

    A combined ``detections.geojson`` is also written for easy web use.
    """

    def __init__(self, output_dir: Optional[str] = None) -> None:
        self._output_dir = output_dir or _DEFAULT_OUTPUT_DIR
        self._kml_writer = KMLWriter(output_dir=self._output_dir)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _session_dir(self, session_id: str) -> Path:
        path = Path(self._output_dir) / session_id
        path.mkdir(parents=True, exist_ok=True)
        return path

    @staticmethod
    def _make_gdf(records: list[dict], geom_col: list):
        """Return a GeoDataFrame from *records* with pre-built geometries."""
        import geopandas as gpd
        return gpd.GeoDataFrame(records, geometry=geom_col, crs="EPSG:4326")

    # ------------------------------------------------------------------
    # Shapefile output
    # ------------------------------------------------------------------

    def write_session(self, session_id: str, events: list[dict]) -> str:
        """Write all events to per-type shapefiles.

        Args:
            session_id: Unique session identifier (used as sub-directory name).
            events: List of event dicts; each must have an ``event_type`` key
                    (``'wifi'``, ``'aircraft'``, ``'drone'``, or ``'remote_id'``).

        Returns:
            Absolute path to the WiFi shapefile (even if no WiFi events were
            written, the path reflects where the file *would* be written).
        """
        from shapely.geometry import Point

        session_dir = self._session_dir(session_id)
        shp_path = str(session_dir / "detections_wifi.shp")

        if not events:
            logger.debug("write_session: no events — nothing written")
            return shp_path

        wifi_events = [e for e in events if e.get("event_type") == "wifi"]
        aircraft_events = [e for e in events if e.get("event_type") == "aircraft"]
        drone_events = [e for e in events if e.get("event_type") == "drone"]
        remote_id_events = [e for e in events if e.get("event_type") == "remote_id"]

        # WiFi / persistence detections
        if wifi_events:
            geometries = [Point(e.get("lon") or 0.0, e.get("lat") or 0.0) for e in wifi_events]
            records = [
                {
                    "mac":        str(e.get("mac", "")),
                    "dev_type":   str(e.get("device_type", "")),
                    "score":      float(e.get("score", 0.0)),
                    "alert_lvl":  str(e.get("alert_level", "")),
                    "manuf":      str(e.get("manufacturer", "")),
                    "first_seen": str(e.get("first_seen", "")),
                    "last_seen":  str(e.get("last_seen", "")),
                    "obs_count":  int(e.get("observation_count", 0)),
                }
                for e in wifi_events
            ]
            gdf = self._make_gdf(records, geometries)
            gdf.to_file(shp_path)
            logger.info("Wrote %d WiFi detections → %s", len(wifi_events), shp_path)

        # Aircraft detections
        if aircraft_events:
            ac_path = str(session_dir / "detections_aircraft.shp")
            geometries = [Point(e.get("lon") or 0.0, e.get("lat") or 0.0) for e in aircraft_events]
            records = [
                {
                    "icao":         str(e.get("icao", "")),
                    "callsign":     str(e.get("callsign", "")),
                    "reg":          str(e.get("registration", "")),
                    "operator":     str(e.get("operator", "")),
                    "country":      str(e.get("country", "")),
                    "altitude":     int(e.get("altitude") or 0),
                    "timestamp":    str(e.get("timestamp", "")),
                }
                for e in aircraft_events
            ]
            gdf = self._make_gdf(records, geometries)
            gdf.to_file(ac_path)
            logger.info("Wrote %d aircraft detections → %s", len(aircraft_events), ac_path)

        # Drone RF detections
        if drone_events:
            drone_path = str(session_dir / "detections_drone.shp")
            geometries = [Point(e.get("lon") or 0.0, e.get("lat") or 0.0) for e in drone_events]
            records = [
                {
                    "freq_mhz":  float(e.get("freq_mhz", 0.0)),
                    "power_db":  float(e.get("power_db", 0.0)),
                    "timestamp": str(e.get("timestamp", "")),
                }
                for e in drone_events
            ]
            gdf = self._make_gdf(records, geometries)
            gdf.to_file(drone_path)
            logger.info("Wrote %d drone detections → %s", len(drone_events), drone_path)

        # Remote ID detections — one point per frame at drone_lat/drone_lon
        if remote_id_events:
            rid_path = str(session_dir / "detections_remote_id.shp")
            geometries = [
                Point(e.get("drone_lon") or 0.0, e.get("drone_lat") or 0.0)
                for e in remote_id_events
            ]
            records = [
                {
                    "uas_id":      str(e.get("uas_id") or ""),
                    "ua_type":     str(e.get("ua_type") or ""),
                    "status":      str(e.get("status") or ""),
                    "operator_id": str(e.get("operator_id") or ""),
                    "op_lat":      float(e.get("operator_lat") or 0.0),
                    "op_lon":      float(e.get("operator_lon") or 0.0),
                    "alt_m":       float(e.get("drone_alt_m") or 0.0),
                    "src_phy":     str(e.get("source_phy") or ""),
                    "src_mac":     str(e.get("source_mac") or ""),
                    "rssi":        int(e.get("rssi") or 0),
                    "timestamp":   str(e.get("timestamp") or ""),
                }
                for e in remote_id_events
            ]
            gdf = self._make_gdf(records, geometries)
            gdf.to_file(rid_path)
            logger.info("Wrote %d Remote ID detections → %s", len(remote_id_events), rid_path)

        # KML output — written alongside shapefiles
        try:
            kml_path = self._kml_writer.write_session(
                session_id, wifi_events, aircraft_events, drone_events
            )
            logger.info("Wrote KML → %s", kml_path)
        except Exception as exc:
            logger.error("KML write error: %s", exc)

        return shp_path

    # ------------------------------------------------------------------
    # GeoJSON output
    # ------------------------------------------------------------------

    def write_geojson(self, session_id: str, events: list[dict]) -> str:
        """Write all events to a single GeoJSON FeatureCollection.

        Args:
            session_id: Unique session identifier.
            events: All event dicts regardless of type.

        Returns:
            Absolute path to the written ``.geojson`` file.
        """
        from shapely.geometry import Point

        session_dir = self._session_dir(session_id)
        geojson_path = str(session_dir / "detections.geojson")

        if not events:
            logger.debug("write_geojson: no events — writing empty FeatureCollection")
            with open(geojson_path, "w", encoding="utf-8") as fh:
                json.dump({"type": "FeatureCollection", "features": []}, fh)
            return geojson_path

        geometries = [Point(e.get("lon") or 0.0, e.get("lat") or 0.0) for e in events]
        # Flatten all event fields as string properties (except lat/lon)
        records = [
            {
                k: (v if isinstance(v, (int, float, bool, type(None))) else str(v))
                for k, v in e.items()
                if k not in ("lat", "lon")
            }
            for e in events
        ]

        import geopandas as gpd
        gdf = gpd.GeoDataFrame(records, geometry=geometries, crs="EPSG:4326")
        gdf.to_file(geojson_path, driver="GeoJSON")
        logger.info("Wrote %d events → %s", len(events), geojson_path)
        return geojson_path
