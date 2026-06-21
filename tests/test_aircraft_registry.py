"""Tests for modules.aircraft_registry.AircraftRegistry — offline DB + adaptive resolve."""

import sqlite3

import pytest

from modules.aircraft_registry import AircraftRegistry


def _make_db(path, rows):
    conn = sqlite3.connect(path)
    conn.executescript(
        "CREATE TABLE aircraft (icao TEXT PRIMARY KEY, registration TEXT, "
        "aircraft_type TEXT, operator TEXT);"
    )
    conn.executemany("INSERT INTO aircraft VALUES (?,?,?,?)", rows)
    conn.commit()
    conn.close()


def test_offline_lookup_hit_and_miss(tmp_path):
    db = tmp_path / "reg.sqlite"
    _make_db(db, [("a12345", "N12345", "B738", "United")])
    r = AircraftRegistry(str(db))
    assert r.offline_available is True
    hit = r.lookup("A12345")                       # case-insensitive
    assert hit["registration"] == "N12345" and hit["aircraft_type"] == "B738"
    assert r.lookup("ffffff") is None


def test_missing_db_is_offline_unavailable(tmp_path):
    r = AircraftRegistry(str(tmp_path / "nope.sqlite"))
    assert r.offline_available is False
    assert r.lookup("a12345") is None


@pytest.mark.asyncio
async def test_resolve_offline_only_no_network(tmp_path):
    db = tmp_path / "reg.sqlite"
    _make_db(db, [("a12345", "N12345", "B738", "United")])
    r = AircraftRegistry(str(db))
    rec = await r.resolve("a12345", online_enrich=None)   # None → never touches network
    assert rec["registration"] == "N12345" and rec["source"] == "offline"


@pytest.mark.asyncio
async def test_resolve_online_wins_and_caches(tmp_path):
    db = tmp_path / "reg.sqlite"
    _make_db(db, [("a12345", "N-OFFLINE", "B738", "United")])
    r = AircraftRegistry(str(db))
    calls = []

    async def fake_enrich(icao):
        calls.append(icao)
        return {"registration": "N-ONLINE", "aircraft_type": "A320",
                "operator": "Delta", "military": False}

    rec = await r.resolve("A12345", online_enrich=fake_enrich)
    assert rec["registration"] == "N-ONLINE" and rec["source"] == "online"
    # Second call is served from cache — no second enrich.
    rec2 = await r.resolve("A12345", online_enrich=fake_enrich)
    assert rec2["registration"] == "N-ONLINE"
    assert calls == ["A12345"]


@pytest.mark.asyncio
async def test_resolve_online_failure_falls_back_offline(tmp_path):
    db = tmp_path / "reg.sqlite"
    _make_db(db, [("a12345", "N-OFFLINE", "B738", "")])
    r = AircraftRegistry(str(db))

    async def boom(icao):
        raise RuntimeError("network down")

    rec = await r.resolve("a12345", online_enrich=boom)
    assert rec["registration"] == "N-OFFLINE" and rec["source"] == "offline"


@pytest.mark.asyncio
async def test_resolve_negative_cached(tmp_path):
    r = AircraftRegistry(str(tmp_path / "nope.sqlite"))  # no DB
    rec = await r.resolve("ffffff", online_enrich=None)
    assert rec == {}                                     # negative
    assert "ffffff" in r._cache
