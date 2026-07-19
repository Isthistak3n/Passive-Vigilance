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


# ---------------------------------------------------------------------------
# AOC position-report breakout (compact implied-decimal position + fix/time/level)
# ---------------------------------------------------------------------------

def test_compact_position_report_parses_and_breaks_out():
    d = _parsed(tail="N57869", flight="UA0638", label="H1",
                text="POSN21207W157466,CKH,073636,80,ALANA,074034,YEPGU,"
                     "P14,06727,133,/TS073636,0707262BDD")
    assert d["category"] == "Position report"
    # implied-decimal degrees, NOT degree-minutes
    assert abs(d["lat"] - 21.207) < 1e-6 and abs(d["lon"] + 157.466) < 1e-6
    names = {f["name"]: f["value"] for f in d["fields"]}
    assert names["Position"] == "N21.207, W157.466"
    assert names["Over"] == "CKH at 07:36:36"
    assert names["Flight level"] == "FL080"
    assert names["Next"] == "ALANA · ETA 07:40:34"
    assert names["Then"] == "YEPGU"
    assert d["text"].startswith("POSN21207W157466")   # raw still intact


def test_compact_position_with_subtype_prefix():
    # "POSA1N..." — a 2-char report subtype sits before the hemisphere; and 157.701
    # must read as decimal degrees (as minutes it would be an impossible 70.1').
    d = _parsed(tail="N1", flight="AA0115", label="H1",
                text="POSA1N21318W157701,GRITL  ,214315, 96,SELIC  ,215025,,10.43")
    assert d["category"] == "Position report"
    assert abs(d["lon"] + 157.701) < 1e-6
    names = {f["name"]: f["value"] for f in d["fields"]}
    assert names["Over"] == "GRITL at 21:43:15"
    assert names["Flight level"] == "FL096"
    assert names["Next"] == "SELIC · ETA 21:50:25"
    assert "Then" not in names                          # trailing empty → degrades cleanly


def test_compact_position_requires_pos_anchor():
    # A bare digit run without the POS prefix must NOT be read as a position.
    d = _parsed(tail="N2", label="H1", text="SEQ N21207W157466 COUNTER")
    assert d["lat"] is None and d["category"] != "Position report"


def test_reclassify_backfills_old_record_without_mutating_it():
    from modules.acars import reclassify
    # An old stored record: raw text + null position, no category (pre-classifier).
    old = {"tail": "N1", "flight_id": "UA0638", "label": "H1", "lat": None, "lon": None,
           "text": "POSN21207W157466,CKH,073636,80,ALANA,074034,YEPGU,P14"}
    new = reclassify(old)
    assert new["category"] == "Position report"
    assert abs(new["lat"] - 21.207) < 1e-6
    names = {f["name"]: f["value"] for f in new["fields"]}
    assert names["Over"] == "CKH at 07:36:36" and names["Flight level"] == "FL080"
    assert old.get("category") is None and old["lat"] is None   # original untouched


def test_reclassify_is_idempotent():
    from modules.acars import reclassify
    rec = {"label": "H1", "text": "hello", "category": "Free text / other", "fields": []}
    assert reclassify(rec) is rec                    # already classified → unchanged


# ---------------------------------------------------------------------------
# Application-layer decode (CPDLC / ADS-C / MIAM / media advisory)
# ---------------------------------------------------------------------------

def _parsed_nested(acars_block):
    """Parse a dumpvdl2-shaped record with a nested libacars app tree."""
    m = _mod()
    m._ingest(json.dumps({"vdl2": {"avlc": {"acars": acars_block}}}))
    return m.drain_detections()[0]


def test_cpdlc_uplink_decoded_to_category_and_elements():
    d = _parsed_nested({
        "reg": "N827DN", "flight": "DAL123", "label": "H1", "msg_text": "/AA",
        "arinc622": {"gs_addr": "KZAK", "cpdlc": {
            "atc_uplink_msg": {"header": {"msg_id": 12},
                               "msg_data": {"msg_element": [
                                   {"choice": "climb_to_level", "data": {"level": "350"}}]}}}},
    })
    assert d["category"] == "CPDLC (controller/pilot)"
    names = {f["name"]: f["value"] for f in d["fields"]}
    assert names["Direction"].startswith("Uplink")
    assert names["Message"] == "Climb to level"
    assert names["Flight"] == "DAL123"               # identity fields preserved


def test_cpdlc_choiceless_element_uses_alternative_name():
    d = _parsed_nested({
        "reg": "N1", "label": "H1", "msg_text": "x",
        "cpdlc": {"atc_downlink_msg": {"msg_data": {"msg_element": [{"wilco": {}}]}}},
    })
    assert d["category"] == "CPDLC (controller/pilot)"
    msgs = [f["value"] for f in d["fields"] if f["name"] == "Message"]
    assert msgs == ["Wilco"]
    assert any(f["value"].startswith("Downlink") for f in d["fields"])


def test_adsc_position_becomes_the_contact_fix():
    d = _parsed_nested({
        "reg": "N2", "label": "H1", "msg_text": "x",
        "arinc622": {"adsc": {"adsc_msg": {"tags": [
            {"basic_report": {"lat": 47.101, "lon": -122.301, "alt": 35000}}]}}},
    })
    assert d["category"] == "ADS-C (surveillance)"
    assert d["lat"] == 47.101 and d["lon"] == -122.301
    names = {f["name"]: f["value"] for f in d["fields"]}
    assert names["Position"] == "N47.101, W122.301"
    assert names["Altitude"] == "35000"


def test_media_advisory_link_state_and_type():
    d = _parsed_nested({
        "reg": "N3", "label": "SA", "msg_text": "x",
        "media_adv": {"version": 0, "state": "E", "current_link": "V",
                      "available_links": ["V", "2"]},
    })
    assert d["category"] == "Link status (media advisory)"
    names = {f["name"]: f["value"] for f in d["fields"]}
    assert names["Link state"] == "Established"
    assert names["Current link"] == "VHF ACARS"
    assert names["Available"] == "VHF ACARS, VDL Mode 2"


def test_miam_named_even_when_fields_sparse():
    d = _parsed_nested({"reg": "N4", "label": "H1", "msg_text": "x",
                        "miam": {"single_transfer": {"hdr": {}}}})
    assert d["category"] == "MIAM (file transfer)"


def test_plain_acars_has_no_app_decode():
    from modules.acars import _decode_app
    assert _decode_app({"reg": "N5", "label": "H1", "msg_text": "hello"}) is None
    d = _parsed_nested({"reg": "N5", "label": "H1", "msg_text": "hello world"})
    assert d["category"] == "Free text / other"      # unchanged for plain messages


def test_app_decode_failure_falls_back_to_text(monkeypatch):
    """A decode crash on an unforeseen structure degrades to text classification and
    never drops the message — the ACARS UDP callback must stay alive on the live node."""
    import modules.acars as A

    def _boom(_acars):
        raise RuntimeError("unexpected libacars shape")

    monkeypatch.setattr(A, "_decode_app", _boom)
    m = _mod()
    m._ingest(json.dumps({"reg": "N6", "label": "H1", "text": "hello world"}))
    d = m.drain_detections()[0]
    assert d["category"] == "Free text / other" and d["text"] == "hello world"
