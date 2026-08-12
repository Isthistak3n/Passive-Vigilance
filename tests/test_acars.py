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
                lat=51.507, lon=-0.127)
    assert d["category"] == "Position report"
    names = {f["name"]: f["value"] for f in d["fields"]}
    assert names["Position"] == "N51.507, W0.127"
    assert names["Flight"] == "UAL638"


def test_category_performance_engine():
    d = _parsed(tail="N807AA", flight="AAL115", label="H1",
                text="APM    5 N807AA  AAL115  KDFWEGLL020726205041 .853")
    assert d["category"] == "Performance / engine"


def test_category_oooi_extracts_event_times():
    d = _parsed(tail="N2", label="SB", text="OUT1230 OFF1245 ON1502 IN1515")
    assert d["category"] == "Flight progress (OOOI)"
    names = {f["name"]: f["value"] for f in d["fields"]}
    assert names["OUT"] == "12:30" and names["IN"] == "15:15"


def test_category_route_dispatch():
    d = _parsed(tail="N3", label="H1", depa="KLAX", dsta="EGLL",
                text="dispatch remarks with no numbers")
    assert d["category"] == "Route / dispatch"
    names = {f["name"]: f["value"] for f in d["fields"]}
    assert names["Route"] == "KLAX→EGLL"


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
                text="POSN51507W000127,XYZ,073636,80,ALPHA,074034,BRAVO,"
                     "P14,06727,133,/TS073636,0707262BDD")
    assert d["category"] == "Position report"
    # implied-decimal degrees, NOT degree-minutes
    assert abs(d["lat"] - 51.507) < 1e-6 and abs(d["lon"] + 0.127) < 1e-6
    names = {f["name"]: f["value"] for f in d["fields"]}
    assert names["Position"] == "N51.507, W0.127"
    assert names["Over"] == "XYZ at 07:36:36"
    assert names["Flight level"] == "FL080"
    assert names["Next"] == "ALPHA · ETA 07:40:34"
    assert names["Then"] == "BRAVO"
    assert d["text"].startswith("POSN51507W000127")   # raw still intact


def test_compact_position_with_subtype_prefix():
    # "POSA1N..." — a 2-char report subtype sits before the hemisphere; and 0.678
    # must read as decimal degrees (as minutes it would be an impossible 67.8').
    d = _parsed(tail="N1", flight="AA0115", label="H1",
                text="POSA1N51318W000678,CHARL  ,214315, 96,DELTA  ,215025,,10.43")
    assert d["category"] == "Position report"
    assert abs(d["lon"] + 0.678) < 1e-6
    names = {f["name"]: f["value"] for f in d["fields"]}
    assert names["Over"] == "CHARL at 21:43:15"
    assert names["Flight level"] == "FL096"
    assert names["Next"] == "DELTA · ETA 21:50:25"
    assert "Then" not in names                          # trailing empty → degrades cleanly


def test_compact_position_requires_pos_anchor():
    # A bare digit run without the POS prefix must NOT be read as a position.
    d = _parsed(tail="N2", label="H1", text="SEQ N51507W000127 COUNTER")
    assert d["lat"] is None and d["category"] != "Position report"


def test_reclassify_backfills_old_record_without_mutating_it():
    from modules.acars import reclassify
    # An old stored record: raw text + null position, no category (pre-classifier).
    old = {"tail": "N1", "flight_id": "UA0638", "label": "H1", "lat": None, "lon": None,
           "text": "POSN51507W000127,XYZ,073636,80,ALPHA,074034,BRAVO,P14"}
    new = reclassify(old)
    assert new["category"] == "Position report"
    assert abs(new["lat"] - 51.507) < 1e-6
    names = {f["name"]: f["value"] for f in new["fields"]}
    assert names["Over"] == "XYZ at 07:36:36" and names["Flight level"] == "FL080"
    assert old.get("category") is None and old["lat"] is None   # original untouched


def test_reclassify_is_idempotent_on_current_version():
    from modules.acars import reclassify, _CLASSIFY_VERSION
    rec = {"label": "H1", "text": "hello", "category": "Free text / other",
           "fields": [], "cver": _CLASSIFY_VERSION}
    assert reclassify(rec) is rec                    # current-schema record → unchanged


def test_reclassify_upgrades_record_from_older_schema():
    """A record the OLD code classified (a category but no/old cver) must be re-decoded,
    so a schema improvement reaches captured history without rewriting the logs."""
    from modules.acars import reclassify, _CLASSIFY_VERSION
    old = {"label": "H1", "category": "Free text / other", "fields": [],
           "text": "++86501,N8774Q,B7378MAX,260722,WN2879,KOAK,EGLL,0946,X\n1\n"
                   "N5130.4,W00007.6,222335,16782,-00.3,196,017,DC,00000,0,"}
    new = reclassify(old)
    assert new is not old
    assert new["category"] == "Position report"
    assert new["cver"] == _CLASSIFY_VERSION
    names = {f["name"]: f["value"] for f in new["fields"]}
    assert names["Route"] == "KOAK→EGLL" and names["Aircraft type"] == "B7378MAX"
    assert old["category"] == "Free text / other"    # original untouched


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


# ---------------------------------------------------------------------------
# Structured airline report families (APM / <701> / ++865xx track / CRUISE)
# ---------------------------------------------------------------------------
# Message shapes below are trimmed from real captures on the node.

def _fields(d):
    return {f["name"]: f["value"] for f in d["fields"]}


def test_apm_report_header_and_mach():
    text = ("APM    6 N45905         UAL345  KIADEGLL010726200452,\n"
            ",70000017,.850,,,258.6,,,-27.68,,,40000,,,363259")
    d = _parsed(tail=".N45905", flight="UA0345", label="H1", text=text)
    assert d["category"] == "Performance / engine"
    f = _fields(d)
    assert f["Report"] == "Engine performance (APM)"
    assert f["Registration"] == "N45905"
    assert f["Route"] == "KIAD→EGLL"
    assert f["Report time"] == "20:04:52Z"
    assert f["Mach"] == "0.850"          # the .850 header field, not a lat/lon decimal


def test_apm_mach_not_taken_from_position_decimals():
    # A later "51.8860" latitude must not be mistaken for a Mach number.
    text = "APM  4 N781HA  ASA121  KSEAEGLL020726042750,,X,.848,,,51.8860,,,-0.1558"
    d = _parsed(tail=".N781HA", flight="HA0121", label="H1", text=text)
    assert _fields(d)["Mach"] == "0.848"


def test_s701_engine_report_identity_and_route():
    text = ("<701>IEG\n N13013UAL219    ER 243210726215737KORDEGLL42681810C  7\n"
            " 360002876861-110 250336ENGD330NOTA")
    d = _parsed(tail=".N13013", flight="UA0219", label="H1", text=text)
    assert d["category"] == "Performance / engine"
    f = _fields(d)
    assert f["Report"] == "Engine data (<701>)"
    assert f["Registration"] == "N13013"
    assert f["Route"] == "KORD→EGLL"     # digit-bounded run, not the trailing NOTA


def test_track_report_header_position_and_summary():
    text = ("++86501,N8774Q,B7378MAX,260722,WN2879,KOAK,EGLL,0946,SMX34-2502-F320\n"
            "4\n"
            "N5130.4,W00007.6,222335,16782,-00.3,196,017,DC,00000,0,\n"
            "N5130.0,W00007.5,222336,14525, 03.8,182,008,DC,00000,0,")
    d = _parsed(tail=".N8774Q", flight="WN2879", label="H1", text=text)
    assert d["category"] == "Position report"
    # First waypoint (degree-minute, comma-separated) becomes the message fix.
    assert abs(d["lat"] - (51 + 30.4 / 60.0)) < 1e-6      # N5130.4
    assert abs(d["lon"] - -(0 + 7.6 / 60.0)) < 1e-6    # W00007.6
    f = _fields(d)
    assert f["Aircraft type"] == "B7378MAX"
    assert f["Route"] == "KOAK→EGLL"
    assert f["Fixes"] == "2 positions"
    assert f["Altitude"] == "FL145–FL168"


def test_cruise_report_state_and_per_engine_n1_n2():
    text = ("CRUISE REPORT SAGE\n"
            "535#CRUISE#27-0720:29:10#1#1#0#1#1#0#0#0#034001#-16.7#0.804#\n"
            "090.6#094.3#00704#03250#049#0100#0.61#0.35#\n"
            "090.7#094.1#00696#03206#051#0099#0.31#0.17#\n"
            "0000.26#0000.72#0064.0#0064.0#0.20")
    d = _parsed(tail=".N535AS", flight="AS0258", label="H1", text=text)
    assert d["category"] == "Performance / engine"
    f = _fields(d)
    assert f["Altitude"] == "FL340"
    assert f["OAT"] == "-16.7°C"
    assert f["Mach"] == "0.804"
    assert f["Eng 1 N1/N2"] == "90.6% / 94.3%"
    assert f["Eng 2 N1/N2"] == "90.7% / 94.1%"
    # The trailing ratio row (leading 0.26) is not a rotor-speed line → no phantom Eng 3.
    assert "Eng 3 N1/N2" not in f


def test_structured_report_does_not_fire_on_plain_free_text():
    raw = "014F63N )4D:Z4D0EZ0IONMPP ZHN1SMS13ZU1P"
    d = _parsed(tail="N5", label="37", text=raw)
    assert d["category"] == "Free text / other"
    assert d["text"] == raw
