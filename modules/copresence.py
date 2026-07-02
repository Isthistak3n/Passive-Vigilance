"""copresence — link contacts that travel together into one "person" (P4 phase C).

A person carries several radios (phone Wi-Fi + phone BLE + a wearable), and those
radios appear and disappear *together*. If two rotation-stable contact identities
are present in nearly the same set of polls, they are probably one person — and
grouping them makes the person recognisable even when one radio rotates its
fingerprint away.

The whole risk here is OVER-MERGE. A fixed node sees the same ambient devices
(neighbour APs, always-on IoT) in almost every poll, so a naive "seen together"
would fuse the whole neighbourhood into one entity. Two guards make it safe, and
neither touches scoring — this is display/identity only:

  * **Presence overlap (Jaccard).** Two contacts link only if the polls they share
    are a large fraction of the polls *either* was seen in. An always-present
    ambient device has near-zero Jaccard with a short-lived visitor (10 shared
    polls out of the ambient's 1000 ≈ 0.01), so it never links to one.
  * **Transience gate.** Only contacts that come and go (present in well under all
    polls) are link-eligible, so two unrelated fixtures that happen to both be
    always-on are not fused either.

Everything is bounded (candidate set per poll, total tracked pairs) so it cannot
grow without limit or stall the poll loop. Pure logic, no I/O — the orchestrator
feeds it the present set each poll and reads back the clusters.
"""
from __future__ import annotations

from itertools import combinations
from typing import Iterable


def _pair(a: str, b: str) -> tuple:
    """Order-independent key for a contact pair."""
    return (a, b) if a <= b else (b, a)


class CoPresenceLinker:
    """Accumulates co-presence between contact identities and clusters the ones
    that consistently travel together into person groups.

    Args (all env-overridable at the call site):
        min_polls:        a pair must be co-present in at least this many polls.
        min_jaccard:      shared / union of their present-polls must be >= this.
        fixture_fraction: a contact present in more than this fraction of all polls
                          is a FIXTURE (always-on) and is not link-eligible.
        min_obs_polls:    each contact must have been seen at least this many polls
                          (don't link on thin data).
        max_present:      cap on contacts considered in one poll's pairing (a burst
                          bigger than this skips pairing that poll — bounds O(n^2)).
        max_pairs:        cap on distinct tracked pairs (lowest-count evicted).
    """

    def __init__(self, *, min_polls: int = 12, min_jaccard: float = 0.6,
                 fixture_fraction: float = 0.5, min_obs_polls: int = 6,
                 min_fixture_polls: int = 20, max_present: int = 60,
                 max_pairs: int = 50000) -> None:
        self._min_polls = int(min_polls)
        self._min_jaccard = float(min_jaccard)
        self._fixture_fraction = float(fixture_fraction)
        self._min_obs_polls = int(min_obs_polls)
        # The fixture gate needs enough polls to tell an always-on ambient device
        # from a visitor who is simply present throughout a still-short window;
        # below this many total polls nothing is treated as a fixture and Jaccard
        # alone guards over-merge.
        self._min_fixture_polls = int(min_fixture_polls)
        self._max_present = int(max_present)
        self._max_pairs = int(max_pairs)
        self._present_count: dict[str, int] = {}
        self._copresent: dict[tuple, int] = {}
        self._total_polls = 0
        # Pairs known-linked from a prior session (loaded durable links). They are
        # treated as established immediately once BOTH are seen again this run.
        self._prior_links: set = set()

    def load_prior_links(self, pairs: Iterable[tuple]) -> None:
        """Seed links established in previous sessions (durable ``contact_links``)."""
        for a, b in pairs:
            if a and b:
                self._prior_links.add(_pair(a, b))

    def observe(self, present_keys: Iterable[str]) -> None:
        """Record one poll's set of currently-present contact identities.

        ``present_keys`` should already exclude un-trackable (``mac:``) identities —
        a rotating address can't anchor a person. A poll with more than
        ``max_present`` candidates skips pairing (still counts presence) so the
        pairwise step can't blow up in a crowd.
        """
        keys = sorted({k for k in present_keys if k and not k.startswith("mac:")})
        self._total_polls += 1
        for k in keys:
            self._present_count[k] = self._present_count.get(k, 0) + 1
        if len(keys) > self._max_present:
            return
        for a, b in combinations(keys, 2):
            # Don't start tracking a pair that already involves an obvious fixture —
            # keeps the pair table dominated by transient candidates.
            if self._is_fixture(a) or self._is_fixture(b):
                continue
            p = (a, b)
            self._copresent[p] = self._copresent.get(p, 0) + 1
        if len(self._copresent) > self._max_pairs:
            self._evict_pairs()

    def _evict_pairs(self) -> None:
        keep = sorted(self._copresent.items(), key=lambda kv: kv[1],
                      reverse=True)[: self._max_pairs * 4 // 5]
        self._copresent = dict(keep)

    def _is_fixture(self, key: str) -> bool:
        """True if ``key`` is present in more than ``fixture_fraction`` of all polls
        (an always-on ambient device), so it is not link-eligible. Dormant until at
        least ``min_fixture_polls`` polls have been seen — before that a visitor
        present throughout a short window is indistinguishable from a fixture, and
        the Jaccard gate is the guard against merging into ambient devices."""
        if self._total_polls < self._min_fixture_polls:
            return False
        return self._present_count.get(key, 0) > self._fixture_fraction * self._total_polls

    def established_links(self) -> list:
        """Pairs that meet the co-presence bar: ``[(a, b, co_polls, jaccard), ...]``.

        A pair known from a prior session (``load_prior_links``) qualifies as soon as
        both have been co-present at all this run, so a returning person re-links fast.
        """
        out = []
        for (a, b), co in self._copresent.items():
            ca = self._present_count.get(a, 0)
            cb = self._present_count.get(b, 0)
            union = ca + cb - co
            jac = (co / union) if union > 0 else 0.0
            prior = _pair(a, b) in self._prior_links
            if not prior:
                if co < self._min_polls or jac < self._min_jaccard:
                    continue
                if min(ca, cb) < self._min_obs_polls:
                    continue
                if self._is_fixture(a) or self._is_fixture(b):
                    continue
            out.append((a, b, co, round(jac, 3)))
        return out

    def clusters(self) -> dict:
        """Union-find the established links into person groups → ``{key: person_id}``.

        The person id is stable within a run: the lexicographically smallest contact
        key in the group, so the same set of contacts always maps to the same id.
        """
        parent: dict[str, str] = {}

        def find(x: str) -> str:
            parent.setdefault(x, x)
            root = x
            while parent[root] != root:
                root = parent[root]
            while parent[x] != root:
                parent[x], x = root, parent[x]
            return root

        def union(x: str, y: str) -> None:
            rx, ry = find(x), find(y)
            if rx == ry:
                return
            hi, lo = (rx, ry) if rx > ry else (ry, rx)
            parent[hi] = lo  # attach toward the smaller key so the root is stable

        links = self.established_links()
        for a, b, _co, _jac in links:
            union(a, b)
        groups: dict[str, set] = {}
        for k in list(parent):
            groups.setdefault(find(k), set()).add(k)
        out: dict[str, str] = {}
        for root, members in groups.items():
            if len(members) < 2:
                continue
            pid = min(members)
            for m in members:
                out[m] = pid
        return out
