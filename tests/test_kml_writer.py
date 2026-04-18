"""Unit tests for modules/kml_writer.py — KMLWriter class."""

import os
import tempfile
import unittest
import xml.etree.ElementTree as ET

import modules.kml_writer  # noqa: F401 — ensure module importable

from modules.kml_writer import KMLWriter

_KML_NS = "http://www.opengis.net/kml/2.2"


def _tag(name):
    return f"{{{_KML_NS}}}{name}"


# ---------------------------------------------------------------------------
# Sample events
# ---------------------------------------------------------------------------

def _wifi_event(mac="aa:bb:cc:dd:ee:ff", alert_level="suspicious", score=0.65,
                lat=51.5074, lon=-0.1278, locations=None):
    return {
        "event_type":        "wifi",
        "mac":               mac,
        "score":             score,
        "alert_level":       alert_level,
        "manufacturer":      "TestCo",
        "device_type":       "Wi-Fi Device",
        "mac_type":          "static",
        "first_seen":        "2024-01-01T12:00:00+00:00",
        "last_seen":         "2024-01-01T12:30:00+00:00",
        "observation_count": 10,
        "lat":               lat,
        "lon":               lon,
        "locations":         locations or [],
    }


def _aircraft_event(callsign="TEST1", reg="G-ABCD", emergency=False,
                    lat=51.5, lon=-0.1):
    return {
        "event_type":    "aircraft",
        "icao":          "abc123",
        "callsign":      callsign,
        "registration":  reg,
        "operator":      "TestAir",
        "country":       "UK",
        "altitude":      10000,
        "speed":         450,
        "emergency":     emergency,
        "lat":           lat,
        "lon":           lon,
        "timestamp":     "2024-01-01T12:00:00+00:00",
    }


def _drone_event(freq=2400.0, lat=51.5, lon=-0.1):
    return {
        "event_type": "drone",
        "freq_mhz":   freq,
        "power_db":   -30.0,
        "lat":        lat,
        "lon":        lon,
        "timestamp":  "2024-01-01T12:00:00+00:00",
    }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestKMLWriterSession(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.kw = KMLWriter(output_dir=self.tmp)
        self.session_id = "20240101_120000"

    def test_write_session_creates_kml_file(self):
        """write_session() must create a .kml file on disk."""
        path = self.kw.write_session(self.session_id, [], [], [])
        self.assertTrue(os.path.exists(path))
        self.assertTrue(path.endswith(".kml"))

    def test_write_session_produces_valid_xml(self):
        """write_session() output must be parseable as XML."""
        path = self.kw.write_session(self.session_id, [], [], [])
        try:
            ET.parse(path)
        except ET.ParseError as exc:
            self.fail(f"KML is not valid XML: {exc}")

    def test_write_session_creates_three_folders(self):
        """write_session() KML Document must contain exactly three Folders."""
        path = self.kw.write_session(
            self.session_id,
            [_wifi_event()],
            [_aircraft_event()],
            [_drone_event()],
        )
        tree = ET.parse(path)
        root = tree.getroot()
        folders = root.findall(f".//{_tag('Folder')}")
        self.assertEqual(len(folders), 3)
        names = [f.findtext(_tag("name")) for f in folders]
        self.assertIn("WiFi/BT Detections", names)
        self.assertIn("Aircraft", names)
        self.assertIn("Drone RF", names)

    def test_write_session_handles_empty_event_lists(self):
        """write_session() must not raise when all event lists are empty."""
        try:
            path = self.kw.write_session(self.session_id, [], [], [])
        except Exception as exc:
            self.fail(f"write_session() raised with empty events: {exc}")
        self.assertTrue(os.path.exists(path))

    def test_output_path_contains_session_id(self):
        """The returned path must contain the session_id."""
        sid = "test_session_999"
        path = self.kw.write_session(sid, [], [], [])
        self.assertIn(sid, path)


class TestKMLWriterWifiPlacemark(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.kw = KMLWriter(output_dir=self.tmp)
        self.session_id = "test_wifi"

    def test_wifi_placemark_contains_mac_in_name(self):
        """WiFi Placemark name must contain the MAC address."""
        mac = "de:ad:be:ef:00:01"
        path = self.kw.write_session(self.session_id, [_wifi_event(mac=mac)], [], [])
        content = open(path).read()
        self.assertIn(mac, content)

    def test_wifi_placemark_description_contains_score(self):
        """WiFi Placemark description must include the persistence score."""
        event = _wifi_event(score=0.82)
        path = self.kw.write_session(self.session_id, [event], [], [])
        content = open(path).read()
        self.assertIn("0.82", content)

    def test_track_line_added_for_multi_location_device(self):
        """A LineString track must be added for devices with 2+ GPS locations."""
        locations = [
            {"lat": 51.5074, "lon": -0.1278, "count": 3},
            {"lat": 51.5124, "lon": -0.1278, "count": 3},
        ]
        event = _wifi_event(locations=locations)
        path = self.kw.write_session(self.session_id, [event], [], [])
        content = open(path).read()
        self.assertIn("LineString", content)
        self.assertIn("Track", content)

    def test_track_line_not_added_for_single_location_device(self):
        """No LineString track for a device with fewer than 2 GPS locations."""
        event = _wifi_event(locations=[{"lat": 51.5074, "lon": -0.1278, "count": 5}])
        path = self.kw.write_session(self.session_id, [event], [], [])
        content = open(path).read()
        self.assertNotIn("LineString", content)


class TestKMLWriterAircraftPlacemark(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.kw = KMLWriter(output_dir=self.tmp)
        self.session_id = "test_aircraft"

    def test_aircraft_placemark_contains_callsign_in_name(self):
        """Aircraft Placemark name must contain the callsign."""
        event = _aircraft_event(callsign="BA001", reg="G-EUUU")
        path = self.kw.write_session(self.session_id, [], [event], [])
        content = open(path).read()
        self.assertIn("BA001", content)

    def test_aircraft_placemark_uses_emergency_style_for_emergencies(self):
        """Emergency aircraft must reference the aircraft-emergency style."""
        event = _aircraft_event(emergency=True)
        path = self.kw.write_session(self.session_id, [], [event], [])
        content = open(path).read()
        self.assertIn("aircraft-emergency", content)

    def test_aircraft_non_emergency_uses_normal_style(self):
        """Non-emergency aircraft Placemark styleUrl must reference aircraft-normal."""
        event = _aircraft_event(emergency=False)
        path = self.kw.write_session(self.session_id, [], [event], [])
        tree = ET.parse(path)
        root = tree.getroot()
        # Find all styleUrl elements inside Placemarks (not Style id attributes)
        style_urls = [
            el.text for el in root.findall(f".//{_tag('styleUrl')}")
        ]
        self.assertTrue(
            any("aircraft-normal" in (u or "") for u in style_urls),
            f"aircraft-normal not found in styleUrls: {style_urls}",
        )
        self.assertFalse(
            any("aircraft-emergency" in (u or "") for u in style_urls),
            f"aircraft-emergency found in styleUrls for non-emergency: {style_urls}",
        )


class TestKMLWriterDronePlacemark(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.kw = KMLWriter(output_dir=self.tmp)
        self.session_id = "test_drone"

    def test_drone_placemark_contains_frequency_in_name(self):
        """Drone Placemark name must include the frequency."""
        event = _drone_event(freq=433.0)
        path = self.kw.write_session(self.session_id, [], [], [event])
        content = open(path).read()
        self.assertIn("433", content)


class TestKMLWriterHTMLTable(unittest.TestCase):

    def setUp(self):
        self.kw = KMLWriter()

    def test_html_table_returns_valid_html_string(self):
        """_html_table() must return a string containing <table> and <tr> tags."""
        result = self.kw._html_table({"Key": "Value", "Score": "0.75"})
        self.assertIsInstance(result, str)
        self.assertIn("<table", result)
        self.assertIn("<tr>", result)
        self.assertIn("Key", result)
        self.assertIn("0.75", result)


if __name__ == "__main__":
    unittest.main()
