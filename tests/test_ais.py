"""Tests for modules.ais.AISModule — parsing the AIS-catcher JSON feed.

The UDP transport isn't exercised (no hardware/socket); we drive the ingest path
directly, which is what the datagram protocol calls per line.
"""

import json

from modules.ais import AISModule


def _mod():
    return AISModule()


def test_position_report_parses():
    m = _mod()
    m._ingest(json.dumps({"mmsi": 366000001, "lat": 21.30, "lon": -157.86,
                          "shipname": "ALOHA", "shiptype": 70}))
    out = m.drain_detections()
    assert len(out) == 1
    d = out[0]
    assert d["mmsi"] == 366000001 and d["lat"] == 21.30 and d["lon"] == -157.86
    assert d["name"] == "ALOHA" and d["ship_type"] == 70 and d["timestamp"]


def test_drain_clears_buffer():
    m = _mod()
    m._ingest(json.dumps({"mmsi": 1, "lat": 1.0, "lon": 2.0}))
    assert len(m.drain_detections()) == 1
    assert m.drain_detections() == []          # second drain is empty


def test_position_not_available_sentinels_become_none():
    m = _mod()
    m._ingest(json.dumps({"mmsi": 2, "lat": 91.0, "lon": 181.0, "name": "DOCKED"}))
    d = m.drain_detections()[0]
    assert d["lat"] is None and d["lon"] is None    # 91/181 = "not available"
    assert d["name"] == "DOCKED"                     # static report still kept


def test_static_report_without_position_kept():
    m = _mod()
    m._ingest(json.dumps({"mmsi": 3, "shipname": "TUG ONE", "shiptype": 52}))
    d = m.drain_detections()[0]
    assert d["mmsi"] == 3 and d["lat"] is None and d["name"] == "TUG ONE"


def test_missing_mmsi_dropped():
    m = _mod()
    m._ingest(json.dumps({"lat": 1.0, "lon": 2.0}))   # no MMSI
    assert m.drain_detections() == []


def test_malformed_json_and_non_object_ignored():
    m = _mod()
    m._ingest("not json at all")
    m._ingest("[1,2,3]")          # valid JSON, but not an object
    assert m.drain_detections() == []


def test_uppercase_mmsi_key_accepted():
    m = _mod()
    m._ingest(json.dumps({"MMSI": "366000002", "lat": "21.4", "lon": "-157.7"}))
    d = m.drain_detections()[0]
    assert d["mmsi"] == 366000002 and d["lat"] == 21.4 and d["lon"] == -157.7
