"""Tests for scripts/generate_apspoof.py — Kismet APSPOOF allowlist generation."""

import importlib.util
import sqlite3
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "generate_apspoof.py"
_spec = importlib.util.spec_from_file_location("generate_apspoof", _SCRIPT)
gen = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gen)


@pytest.fixture()
def db(tmp_path):
    """A minimal entities.db with just the beacon_evidence table."""
    path = tmp_path / "entities.db"
    conn = sqlite3.connect(path)
    conn.execute(
        """
        CREATE TABLE beacon_evidence (
            bssid        TEXT    NOT NULL,
            ssid         TEXT    NOT NULL,
            channel      INTEGER,
            crypt        INTEGER,
            first_seen   TEXT    NOT NULL,
            last_seen    TEXT    NOT NULL,
            beacon_count INTEGER NOT NULL DEFAULT 1,
            sig_n        INTEGER NOT NULL DEFAULT 0,
            sig_mean     REAL    NOT NULL DEFAULT 0.0,
            sig_m2       REAL    NOT NULL DEFAULT 0.0,
            PRIMARY KEY (bssid, ssid)
        )
        """
    )
    conn.commit()
    conn.close()
    return path


def _insert(path, bssid, ssid, beacon_count=100):
    conn = sqlite3.connect(path)
    conn.execute(
        "INSERT INTO beacon_evidence "
        "(bssid, ssid, first_seen, last_seen, beacon_count) "
        "VALUES (?, ?, '2026-01-01', '2026-01-02', ?)",
        (bssid, ssid, beacon_count),
    )
    conn.commit()
    conn.close()


class TestGenerateRules:
    def test_one_rule_per_ssid_with_all_its_bssids(self, db):
        _insert(db, "aa:bb:cc:00:00:01", "HomeNet")
        _insert(db, "aa:bb:cc:00:00:02", "HomeNet")  # mesh node — same SSID
        _insert(db, "dd:ee:ff:00:00:01", "OfficeNet")
        rules = gen.generate_rules(str(db))
        assert len(rules) == 2
        home = next(r for r in rules if "HomeNet" in r)
        assert 'validmacs="AA:BB:CC:00:00:01,AA:BB:CC:00:00:02"' in home
        assert home.startswith("apspoof=pvbase")

    def test_ssid_is_anchored_and_regex_escaped(self, db):
        _insert(db, "aa:bb:cc:00:00:01", "Bob's Net (5G)+")
        rules = gen.generate_rules(str(db))
        assert len(rules) == 1
        # Metacharacters escaped, whole-string anchored.
        assert 'ssid="^' in rules[0]
        assert "\\(5G\\)\\+$" in rules[0]

    def test_thin_evidence_is_not_trusted(self, db):
        _insert(db, "aa:bb:cc:00:00:01", "HomeNet", beacon_count=100)
        _insert(db, "aa:bb:cc:00:00:99", "HomeNet", beacon_count=2)  # one-beacon wonder
        rules = gen.generate_rules(str(db), min_beacons=10)
        assert len(rules) == 1
        assert "AA:BB:CC:00:00:99" not in rules[0]

    def test_wifi_direct_ssids_are_excluded_by_default(self, db):
        _insert(db, "aa:bb:cc:00:00:01", "DIRECT-xy-Printer")
        _insert(db, "dd:ee:ff:00:00:01", "HomeNet")
        rules = gen.generate_rules(str(db))
        assert len(rules) == 1
        assert "DIRECT" not in rules[0]

    def test_hidden_ap_empty_ssid_is_excluded(self, db):
        _insert(db, "aa:bb:cc:00:00:01", "")
        assert gen.generate_rules(str(db)) == []

    def test_rule_names_are_unique(self, db):
        for i in range(5):
            _insert(db, f"aa:bb:cc:00:00:0{i}", f"Net{i}")
        rules = gen.generate_rules(str(db))
        names = [r.split(":", 1)[0] for r in rules]
        assert len(set(names)) == 5


class TestMain:
    def test_writes_fragment_with_header(self, db, tmp_path, capsys):
        _insert(db, "aa:bb:cc:00:00:01", "HomeNet")
        out = tmp_path / "apspoof.conf"
        rc = gen.main(["--db", str(db), "--output", str(out)])
        assert rc == 0
        text = out.read_text()
        assert text.startswith("# Kismet APSPOOF allowlist")
        assert "apspoof=pvbase0" in text

    def test_stdout_by_default(self, db, capsys):
        _insert(db, "aa:bb:cc:00:00:01", "HomeNet")
        gen.main(["--db", str(db)])
        assert "apspoof=pvbase0" in capsys.readouterr().out

    def test_missing_db_is_an_argument_error(self, tmp_path):
        with pytest.raises(SystemExit):
            gen.main(["--db", str(tmp_path / "nope.db")])

    def test_include_prefix_overrides_the_exclusion(self, db):
        _insert(db, "aa:bb:cc:00:00:01", "DIRECT-xy-Printer")
        rc = gen.main(["--db", str(db), "--include-prefix", "DIRECT-"])
        assert rc == 0
