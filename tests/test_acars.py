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


# ---------------------------------------------------------------------------
# Human-friendly classification (category + broken-out fields)
# ---------------------------------------------------------------------------

def _parsed(**payload):
    m = _mod()
    m._ingest(json.dumps(payload))
    return m.drain_detections()[0]


def test_category_position_report_with_fields():
    d = _parsed(tail="N1", flight="UAL638", label="H1", text="POSN",
                lat=21.207, lon=-157.466)
    assert d["category"] == "Position report"
    names = {f["name"]: f["value"] for f in d["fields"]}
    assert names["Position"] == "N21.207, W157.466"
    assert names["Flight"] == "UAL638"


def test_category_performance_engine():
    d = _parsed(tail="N807AA", flight="AAL115", label="H1",
                text="APM    5 N807AA  AAL115  KDFWPHNL020726205041 .853")
    assert d["category"] == "Performance / engine"


def test_category_oooi_extracts_event_times():
    d = _parsed(tail="N2", label="SB", text="OUT1230 OFF1245 ON1502 IN1515")
    assert d["category"] == "Flight progress (OOOI)"
    names = {f["name"]: f["value"] for f in d["fields"]}
    assert names["OUT"] == "12:30" and names["IN"] == "15:15"


def test_category_route_dispatch():
    d = _parsed(tail="N3", label="H1", depa="KLAX", dsta="PHNL",
                text="dispatch remarks with no numbers")
    assert d["category"] == "Route / dispatch"
    names = {f["name"]: f["value"] for f in d["fields"]}
    assert names["Route"] == "KLAX→PHNL"


def test_category_link_management_from_label():
    d = _parsed(tail="N4", label="_d", text="")
    assert d["category"] == "Link management"


def test_category_free_text_keeps_raw_and_does_not_fake_decode():
    raw = "014F63N )4D:Z4D0EZ0IONMPP ZHN1SMS13ZU1P"
    d = _parsed(tail="N5", label="37", text=raw)
    assert d["category"] == "Free text / other"
    assert d["text"] == raw                       # raw preserved verbatim
    # No position/route/flight to fabricate → no invented fields.
    assert all(f["name"] not in ("Position", "Route") for f in d["fields"])


def test_incidental_on_in_not_misread_as_oooi():
    # A single OOOI-looking token in prose must not trigger the OOOI category.
    d = _parsed(tail="N9", label="H1", text="TURN ON1234 THE SYSTEM")
    assert d["category"] != "Flight progress (OOOI)"


def test_label_name_known_and_fallback():
    assert _parsed(tail="N1", label="H1", text="x")["label_name"] == "Message / airline data"
    assert _parsed(tail="N1", label="ZZ", text="x")["label_name"] == "Airline / other"
