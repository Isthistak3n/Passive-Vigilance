"""KML writer — Google Earth / Google Maps visualization of session detections.

Writes a single richly-styled KML file per session alongside the existing
shapefiles.  Three folders are created: WiFi/BT Detections, Aircraft, and
Drone RF.  WiFi devices are color-coded by alert level; devices seen at
two or more GPS locations get a LineString track showing their movement.
Aircraft are placed at actual altitude (feet → metres).

Pure Python — no additional dependencies beyond the standard library.
"""

import logging
import os
from pathlib import Path
from typing import Optional
from xml.sax.saxutils import escape as _xe

from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

_DEFAULT_OUTPUT_DIR = os.getenv("SESSION_OUTPUT_DIR", "data/sessions")

_ICON_BASE = "http://maps.google.com/mapfiles/kml"

# icon_href, KML color (aabbggrr)
_POINT_STYLE_DEFS: dict[str, tuple[str, str]] = {
    "wifi-new":           (f"{_ICON_BASE}/pushpin/wht-pushpin.png",    "ffffffff"),
    "wifi-suspicious":    (f"{_ICON_BASE}/pushpin/ylw-pushpin.png",    "ff00ffff"),
    "wifi-likely":        (f"{_ICON_BASE}/pushpin/orange-pushpin.png", "ff00a5ff"),
    "wifi-high":          (f"{_ICON_BASE}/pushpin/red-pushpin.png",    "ff0000ff"),
    "aircraft-normal":    (f"{_ICON_BASE}/shapes/airports.png",        "ffff8800"),
    "aircraft-emergency": (f"{_ICON_BASE}/pushpin/red-pushpin.png",    "ff0000ff"),
    "drone":              (f"{_ICON_BASE}/shapes/radio.png",            "ff0080ff"),
}

# KML color (aabbggrr), line width
_LINE_STYLE_DEFS: dict[str, tuple[str, int]] = {
    "wifi-track-new":        ("ffffffff", 1),
    "wifi-track-suspicious": ("ff00ffff", 2),
    "wifi-track-likely":     ("ff00a5ff", 2),
    "wifi-track-high":       ("ff0000ff", 3),
}


def _wifi_style_id(alert_level: str) -> str:
    return {
        "suspicious": "wifi-suspicious",
        "likely":     "wifi-likely",
        "high":       "wifi-high",
    }.get(alert_level, "wifi-new")


def _track_style_id(alert_level: str) -> str:
    return {
        "suspicious": "wifi-track-suspicious",
        "likely":     "wifi-track-likely",
        "high":       "wifi-track-high",
    }.get(alert_level, "wifi-track-new")


def _kml_ts(ts_str: str) -> str:
    """Normalize an ISO timestamp to KML-compatible form (Z suffix)."""
    if not ts_str:
        return ""
    return str(ts_str).replace("+00:00", "Z")


class KMLWriter:
    """Writes session detection events to a richly-styled KML file.

    Call :meth:`write_session` once at session end to produce
    ``{session_id}/detections.kml``.  Call :meth:`write_session_summary_overlay`
    afterwards to insert a ScreenOverlay legend with session statistics.

    The output directory defaults to the ``SESSION_OUTPUT_DIR`` environment
    variable (``data/sessions`` if unset).
    """

    def __init__(self, output_dir: Optional[str] = None) -> None:
        self._output_dir = output_dir or _DEFAULT_OUTPUT_DIR

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def write_session(
        self,
        session_id: str,
        wifi_events: list,
        aircraft_events: list,
        drone_events: list,
    ) -> str:
        """Write a KML file for the session with all three detection layers.

        Args:
            session_id:       Unique session identifier (used as sub-directory).
            wifi_events:      WiFi/BT persistence detections — dicts with ``mac``,
                              ``score``, ``alert_level``, ``lat``, ``lon``,
                              ``first_seen``, ``last_seen``, ``observation_count``,
                              ``manufacturer``, ``device_type``, ``mac_type``, and
                              optionally ``locations`` (list of ``{lat, lon, count}``).
            aircraft_events:  ADS-B aircraft dicts with ``icao``, ``callsign``,
                              ``registration``, ``operator``, ``country``,
                              ``altitude``, ``lat``, ``lon``, ``emergency``,
                              ``timestamp``.
            drone_events:     Drone RF detection dicts with ``freq_mhz``,
                              ``power_db``, ``lat``, ``lon``, ``timestamp``.

        Returns:
            Absolute path to the written ``.kml`` file.
        """
        session_dir = self._session_dir(session_id)
        kml_path = str(session_dir / "detections.kml")

        lines: list[str] = []
        lines += self._kml_header(session_id)
        lines += self._style_elements()

        lines += ["  <Folder>", "    <name>WiFi/BT Detections</name>"]
        for event in wifi_events:
            lines += self._wifi_placemark(event)
        lines.append("  </Folder>")

        lines += ["  <Folder>", "    <name>Aircraft</name>"]
        for event in aircraft_events:
            lines += self._aircraft_placemark(event)
        lines.append("  </Folder>")

        lines += ["  <Folder>", "    <name>Drone RF</name>"]
        for event in drone_events:
            lines += self._drone_placemark(event)
        lines.append("  </Folder>")

        lines += self._kml_footer()

        with open(kml_path, "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines))

        logger.info(
            "Wrote KML: %d WiFi / %d aircraft / %d drone → %s",
            len(wifi_events), len(aircraft_events), len(drone_events), kml_path,
        )
        return kml_path

    def write_session_summary_overlay(self, session_id: str, summary: dict) -> str:
        """Insert a ScreenOverlay legend with session statistics into the KML file.

        Reads the existing ``detections.kml`` (created by :meth:`write_session`),
        inserts a ScreenOverlay element positioned at the top-left of the
        Google Earth view, and rewrites the file.  Creates a minimal KML if the
        file does not yet exist.

        Args:
            session_id: Unique session identifier.
            summary:    Session summary dict (same as written to ``summary.json``).

        Returns:
            Absolute path to the (rewritten) ``.kml`` file.
        """
        session_dir = self._session_dir(session_id)
        kml_path = str(session_dir / "detections.kml")

        overlay_xml = "\n".join(self._screen_overlay(summary))

        if os.path.exists(kml_path):
            content = Path(kml_path).read_text(encoding="utf-8")
            content = content.replace("</Document>", overlay_xml + "\n</Document>", 1)
        else:
            lines = self._kml_header(session_id) + [overlay_xml] + self._kml_footer()
            content = "\n".join(lines)

        with open(kml_path, "w", encoding="utf-8") as fh:
            fh.write(content)
        return kml_path

    # ------------------------------------------------------------------
    # KML document skeleton
    # ------------------------------------------------------------------

    def _session_dir(self, session_id: str) -> Path:
        path = Path(self._output_dir) / session_id
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _kml_header(self, session_id: str) -> list[str]:
        return [
            '<?xml version="1.0" encoding="UTF-8"?>',
            '<kml xmlns="http://www.opengis.net/kml/2.2">',
            "<Document>",
            f"  <name>Passive Vigilance \u2014 {_xe(session_id)}</name>",
        ]

    def _kml_footer(self) -> list[str]:
        return ["</Document>", "</kml>"]

    def _style_elements(self) -> list[str]:
        lines: list[str] = []
        for sid, (href, color) in _POINT_STYLE_DEFS.items():
            lines += [
                f'  <Style id="{sid}">',
                "    <IconStyle>",
                f"      <color>{color}</color>",
                "      <Icon>",
                f"        <href>{href}</href>",
                "      </Icon>",
                "    </IconStyle>",
                "  </Style>",
            ]
        for sid, (color, width) in _LINE_STYLE_DEFS.items():
            lines += [
                f'  <Style id="{sid}">',
                "    <LineStyle>",
                f"      <color>{color}</color>",
                f"      <width>{width}</width>",
                "    </LineStyle>",
                "  </Style>",
            ]
        return lines

    # ------------------------------------------------------------------
    # Placemark builders
    # ------------------------------------------------------------------

    def _build_placemark(
        self,
        name: str,
        description: str,
        lat: float,
        lon: float,
        alt: float,
        style_id: str,
        timestamp: str = "",
    ) -> list[str]:
        """Return KML lines for a single Point Placemark."""
        lines: list[str] = [
            "    <Placemark>",
            f"      <name>{_xe(name)}</name>",
            "      <description>",
            f"        <![CDATA[{description}]]>",
            "      </description>",
        ]
        ts = _kml_ts(timestamp)
        if ts:
            lines += [
                "      <TimeStamp>",
                f"        <when>{ts}</when>",
                "      </TimeStamp>",
            ]
        lines += [
            f"      <styleUrl>#{style_id}</styleUrl>",
            "      <Point>",
            f"        <coordinates>{lon},{lat},{alt}</coordinates>",
            "      </Point>",
            "    </Placemark>",
        ]
        return lines

    def _html_table(self, fields: dict) -> str:
        """Return an HTML table string for use inside a KML description CDATA block."""
        rows = "".join(
            f"<tr><th align='left' style='padding-right:8px'>{k}</th>"
            f"<td>{'' if v is None else v}</td></tr>"
            for k, v in fields.items()
        )
        return f"<table border='1' cellpadding='3'>{rows}</table>"

    def _wifi_placemark(self, event: dict) -> list[str]:
        mac = event.get("mac", "")
        alert_level = event.get("alert_level", "")
        mac_type = event.get("mac_type", "static")
        style_id = _wifi_style_id(alert_level)

        name = f"{mac} ({mac_type})"
        desc = self._html_table({
            "MAC":               mac,
            "Type":              event.get("device_type", ""),
            "Manufacturer":      event.get("manufacturer", ""),
            "Persistence Score": f"{event.get('score', 0):.2f}",
            "Alert Level":       alert_level,
            "First Seen":        event.get("first_seen", ""),
            "Last Seen":         event.get("last_seen", ""),
            "Observations":      event.get("observation_count", ""),
            "MAC Type":          mac_type,
        })
        lat = float(event.get("lat") or 0.0)
        lon = float(event.get("lon") or 0.0)
        ts = event.get("first_seen") or event.get("timestamp", "")

        lines = self._build_placemark(name, desc, lat, lon, 0.0, style_id, ts)

        # Track line for devices seen at 2+ distinct GPS locations
        locations = event.get("locations") or []
        if len(locations) >= 2:
            lines += self._track_linestring(mac, alert_level, locations)

        return lines

    def _track_linestring(
        self,
        mac: str,
        alert_level: str,
        locations: list,
    ) -> list[str]:
        """Return KML lines for a LineString track connecting observation clusters."""
        coord_parts = [
            f"{loc['lon']},{loc['lat']},0"
            for loc in locations
            if loc.get("lat") is not None and loc.get("lon") is not None
        ]
        if len(coord_parts) < 2:
            return []
        coords = " ".join(coord_parts)
        style_id = _track_style_id(alert_level)
        return [
            "    <Placemark>",
            f"      <name>Track \u2014 {_xe(mac)}</name>",
            f"      <styleUrl>#{style_id}</styleUrl>",
            "      <LineString>",
            "        <tessellate>1</tessellate>",
            f"        <coordinates>{coords}</coordinates>",
            "      </LineString>",
            "    </Placemark>",
        ]

    def _aircraft_placemark(self, event: dict) -> list[str]:
        callsign = event.get("callsign") or event.get("icao", "unknown")
        reg = event.get("registration", "N/A")
        emergency = bool(event.get("emergency", False))
        style_id = "aircraft-emergency" if emergency else "aircraft-normal"

        name = f"{callsign} ({reg})"
        desc = self._html_table({
            "ICAO":          event.get("icao", ""),
            "Callsign":      callsign,
            "Registration":  reg,
            "Operator":      event.get("operator", "N/A"),
            "Country":       event.get("country", "N/A"),
            "Altitude (ft)": event.get("altitude", "N/A"),
            "Speed":         event.get("speed", "N/A"),
            "Emergency":     str(emergency),
        })
        lat = float(event.get("lat") or 0.0)
        lon = float(event.get("lon") or 0.0)
        # Convert altitude from feet to metres for KML Point altitudeMode
        try:
            alt_m = float(event.get("altitude") or 0) * 0.3048
        except (TypeError, ValueError):
            alt_m = 0.0
        ts = event.get("timestamp", "")
        return self._build_placemark(name, desc, lat, lon, alt_m, style_id, ts)

    def _drone_placemark(self, event: dict) -> list[str]:
        freq = event.get("freq_mhz", 0)
        name = f"Drone RF \u2014 {freq} MHz"
        desc = self._html_table({
            "Frequency (MHz)": freq,
            "Power (dBm)":     event.get("power_db", ""),
            "Timestamp":       event.get("timestamp", ""),
            "Location":        f"{event.get('lat', 'N/A')}, {event.get('lon', 'N/A')}",
        })
        lat = float(event.get("lat") or 0.0)
        lon = float(event.get("lon") or 0.0)
        ts = event.get("timestamp", "")
        return self._build_placemark(name, desc, lat, lon, 0.0, "drone", ts)

    # ------------------------------------------------------------------
    # ScreenOverlay
    # ------------------------------------------------------------------

    def _screen_overlay(self, summary: dict) -> list[str]:
        session_id = summary.get("session_id", "")
        duration = int(summary.get("duration_seconds", 0))
        h, rem = divmod(duration, 3600)
        m, s = divmod(rem, 60)
        duration_str = f"{h}h {m:02d}m {s:02d}s"
        desc = (
            f"Session: {_xe(str(session_id))} | Duration: {duration_str} | "
            f"WiFi: {summary.get('persistent_detections', 0)} | "
            f"Aircraft: {summary.get('aircraft_detected', 0)} | "
            f"Drone: {summary.get('drone_detections', 0)}"
        )
        return [
            "  <ScreenOverlay>",
            f"    <name>Session Summary \u2014 {_xe(str(session_id))}</name>",
            f"    <description>{desc}</description>",
            "    <overlayXY x='0' y='1' xunits='fraction' yunits='fraction'/>",
            "    <screenXY x='0' y='1' xunits='fraction' yunits='fraction'/>",
            "    <size x='0' y='0' xunits='fraction' yunits='fraction'/>",
            "  </ScreenOverlay>",
        ]
