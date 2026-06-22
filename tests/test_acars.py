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
