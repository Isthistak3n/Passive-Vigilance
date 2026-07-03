"""Tests for modules/copresence.py — co-presence linking (P4 phase C).

The headline tests are the OVER-MERGE guards: an always-present ambient device
must never link to a visitor, and two unrelated always-on fixtures must not fuse.
"""
from modules.copresence import CoPresenceLinker


def _linker(**kw):
    kw.setdefault("min_polls", 5)
    kw.setdefault("min_jaccard", 0.6)
    kw.setdefault("fixture_fraction", 0.5)
    kw.setdefault("min_obs_polls", 3)
    kw.setdefault("min_fixture_polls", 20)
    return CoPresenceLinker(**kw)


def _visit(lk, keys, start, length, total):
    """Run *total* polls; *keys* are present only for polls [start, start+length)."""
    for i in range(total):
        lk.observe(set(keys) if start <= i < start + length else set())


def test_two_contacts_always_together_link():
    lk = _linker()
    # A person's two radios present together for a 15-poll visit inside a 60-poll run.
    _visit(lk, {"wifi-fp:a", "ble-fp:b"}, start=5, length=15, total=60)
    links = lk.established_links()
    assert len(links) == 1
    a, b, co, jac = links[0]
    assert {a, b} == {"wifi-fp:a", "ble-fp:b"}
    assert co >= 14 and jac >= 0.8      # present together throughout the visit
    assert lk.clusters() == {"wifi-fp:a": "ble-fp:b", "ble-fp:b": "ble-fp:b"}


def test_ambient_fixture_never_links_to_visitor():
    """An always-present ambient contact co-occurs with a brief visitor, but its
    Jaccard is tiny AND it is a fixture — so no link. THE over-merge guard."""
    lk = _linker(min_polls=3)
    for i in range(60):
        present = {"wifi-fp:ambient"}          # in every poll
        if 10 <= i < 16:
            present.add("wifi-fp:visitor")     # a 6-poll visit
        lk.observe(present)
    assert lk.established_links() == []
    assert lk.clusters() == {}


def test_two_unrelated_fixtures_do_not_fuse():
    lk = _linker(min_polls=3)
    for _ in range(60):
        lk.observe({"wifi-fp:apX", "wifi-fp:apY"})  # both always on (100% of polls)
    # Both are fixtures over a long-enough window -> not link-eligible.
    assert lk.established_links() == []
    assert lk.clusters() == {}


def test_occasional_coincidence_below_threshold_does_not_link():
    lk = _linker(min_polls=8, min_jaccard=0.6)
    for _ in range(3):
        lk.observe({"wifi-fp:p", "ble-fp:q"})       # together 3x
    for _ in range(10):
        lk.observe({"wifi-fp:p"})
    for _ in range(10):
        lk.observe({"ble-fp:q"})
    assert lk.established_links() == []


def test_mac_identities_ignored():
    lk = _linker()
    _visit(lk, {"mac:aa:bb", "wifi-fp:a"}, start=5, length=15, total=60)
    assert all("mac:" not in a and "mac:" not in b
               for a, b, *_ in lk.established_links())


def test_thin_data_not_linked():
    lk = _linker(min_polls=2, min_obs_polls=5)
    _visit(lk, {"wifi-fp:a", "ble-fp:b"}, start=0, length=2, total=60)
    assert lk.established_links() == []   # only 2 obs each, below min_obs_polls


def test_three_devices_one_person_cluster():
    lk = _linker(min_polls=3)
    _visit(lk, {"wifi-fp:phone", "ble-fp:phone", "ble-fp:watch"},
           start=5, length=12, total=60)
    clusters = lk.clusters()
    assert len(set(clusters.values())) == 1
    assert set(clusters) == {"wifi-fp:phone", "ble-fp:phone", "ble-fp:watch"}


def test_prior_link_relinks_when_present_again():
    """A pair known from a prior session skips the co-presence THRESHOLDS, so a
    genuinely returning (transient) person re-links without re-earning the bar."""
    lk = _linker(min_polls=100, min_fixture_polls=20)   # never link on its own
    lk.load_prior_links([("wifi-fp:a", "ble-fp:b")])
    # Present together for an 11-poll visit inside a 30-poll run (transient, gate active).
    _visit(lk, {"wifi-fp:a", "ble-fp:b"}, start=5, length=11, total=30)
    links = lk.established_links()
    assert len(links) == 1 and {links[0][0], links[0][1]} == {"wifi-fp:a", "ble-fp:b"}


def test_no_link_before_fixture_gate_engages():
    """Regression (caught live): before min_fixture_polls, NOTHING links — otherwise
    the persistent ambient background fuses into one false 'person' in the first minutes."""
    lk = _linker(min_polls=3, min_fixture_polls=30)
    for _ in range(20):                       # 20 polls < 30 -> gate dormant
        lk.observe({"wifi-fp:a", "ble-fp:b", "wifi-fp:c"})
    assert lk.established_links() == []
    assert lk.clusters() == {}


def test_prior_link_between_fixtures_does_not_reestablish():
    """A stale false link (two always-on devices) must NOT re-cluster on reload —
    the fixture exclusion applies to prior links too."""
    lk = _linker(min_polls=3, min_fixture_polls=20)
    lk.load_prior_links([("wifi-fp:apish", "wifi-fp:iot")])
    for _ in range(40):                       # both present in 100% of polls -> fixtures
        lk.observe({"wifi-fp:apish", "wifi-fp:iot"})
    assert lk.established_links() == []


def test_burst_over_cap_skips_pairing_but_counts_presence():
    lk = _linker(max_present=3)
    lk.observe({"wifi-fp:a", "wifi-fp:b", "wifi-fp:c", "wifi-fp:d"})  # 4 > cap 3
    assert lk._copresent == {}
    assert lk._present_count["wifi-fp:a"] == 1


def test_pair_table_bounded():
    lk = _linker(max_pairs=10)
    for i in range(40):
        lk.observe({f"wifi-fp:{i}", f"ble-fp:{i}"})
    assert len(lk._copresent) <= 10
