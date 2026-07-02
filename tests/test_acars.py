"""Tests for modules.acars.ACARSModule — parsing acarsdec / dumpvdl2 JSON."""

import json

from modules.acars import ACARSModule


def _mod():
    return ACARSModule()


def test_acarsdec_flat_parse():
    m = _mod()
    m._ingest(json.dumps({"tail": "N12345", "flight": "UAL123", "label": "H1",
                          "text": "HELLO"}))
    d = m.drain_detections()[0]
    assert d["tail"] == "N12345" and d["flight_id"] == "UAL123"
    assert d["label"] == "H1" and d["text"] == "HELLO" and d["timestamp"]


def test_dumpvdl2_nested_parse():
    m = _mod()
    m._ingest(json.dumps({"vdl2": {"avlc": {"acars": {
        "reg": "N777AA", "flight": "AAL55", "label": "5Z", "msg_text": "OPS"}}}}))
    d = m.drain_detections()[0]
    assert d["tail"] == "N777AA" and d["flight_id"] == "AAL55"
    assert d["label"] == "5Z" and d["text"] == "OPS"


def test_reg_alias_and_strip():
    m = _mod()
    m._ingest(json.dumps({"reg": "  N9  ", "text": "  hi  "}))
    d = m.drain_detections()[0]
    assert d["tail"] == "N9" and d["text"] == "hi" and d["flight_id"] is None


def test_drain_clears():
    m = _mod()
    m._ingest(json.dumps({"tail": "N1", "text": "x"}))
    assert len(m.drain_detections()) == 1
    assert m.drain_detections() == []


def test_identityless_and_contentless_dropped():
    m = _mod()
    m._ingest(json.dumps({"label": "_d"}))      # no tail/flight/text
    m._ingest(json.dumps({"vdl2": {"avlc": {}}}))  # no acars block
    assert m.drain_detections() == []


def test_malformed_ignored():
    m = _mod()
    m._ingest("nope")
    m._ingest("[1,2]")
    assert m.drain_detections() == []


# ---------------------------------------------------------------------------
# Enrichment — origin/destination + position parsing
# ---------------------------------------------------------------------------


def test_origin_destination_structured_fields():
    m = _mod()
    m._ingest(json.dumps({"tail": "N1", "text": "x", "depa": "KJFK", "dsta": "KLAX"}))
    d = m.drain_detections()[0]
    assert d["origin"] == "KJFK" and d["destination"] == "KLAX"


def test_origin_destination_dumpvdl2_aliases():
    m = _mod()
    m._ingest(json.dumps({"vdl2": {"avlc": {"acars": {
        "reg": "N2", "msg_text": "y", "dep": "EGLL", "dst": "LFPG"}}}}))
    d = m.drain_detections()[0]
    assert d["origin"] == "EGLL" and d["destination"] == "LFPG"


def test_position_from_structured_lat_lon():
    m = _mod()
    m._ingest(json.dumps({"tail": "N3", "text": "z", "lat": 51.5, "lon": -0.12}))
    d = m.drain_detections()[0]
    assert d["lat"] == 51.5 and d["lon"] == -0.12


def test_position_from_decimal_text():
    m = _mod()
    m._ingest(json.dumps({"tail": "N4", "text": "POS N51.5074 W000.1278 FL350"}))
    d = m.drain_detections()[0]
    assert abs(d["lat"] - 51.5074) < 1e-4
    assert abs(d["lon"] - (-0.1278)) < 1e-4


def test_position_from_degree_minute_text():
    m = _mod()
    # N51 30.0' , W000 07.0'  ->  51.5 , -0.1167
    m._ingest(json.dumps({"tail": "N5", "text": "/POS N5130.0W00007.0/"}))
    d = m.drain_detections()[0]
    assert abs(d["lat"] - 51.5) < 1e-3
    assert abs(d["lon"] - (-0.11667)) < 1e-3


def test_no_position_when_text_has_none():
    m = _mod()
    m._ingest(json.dumps({"tail": "N6", "text": "OPS NORMAL NO NUMBERS"}))
    d = m.drain_detections()[0]
    assert d["lat"] is None and d["lon"] is None


def test_out_of_range_position_rejected():
    m = _mod()
    m._ingest(json.dumps({"tail": "N7", "text": "x", "lat": 999.0, "lon": 5.0}))
    d = m.drain_detections()[0]
    assert d["lat"] is None and d["lon"] is None


def test_enrichment_fields_present_even_when_absent():
    m = _mod()
    m._ingest(json.dumps({"tail": "N8", "text": "plain"}))
    d = m.drain_detections()[0]
    for k in ("origin", "destination", "lat", "lon"):
        assert k in d and d[k] is None
