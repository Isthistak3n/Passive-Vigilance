"""Shapefile writer — appends sensor events to a point shapefile for GIS analysis."""

import logging

logger = logging.getLogger(__name__)


class ShapefileWriter:
    """Writes sensor events as point features to a shapefile via GeoPandas / Fiona."""

    def write_event(self, event: dict) -> None:
        """Append a single event dict (must contain lat, lon, and metadata) to the shapefile."""
        raise NotImplementedError
