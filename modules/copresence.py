"""copresence — link contacts that travel together into one "person" (P4 phase C).

A person carries several radios (phone Wi-Fi + phone BLE + a wearable), and those
radios appear and disappear *together*. If two rotation-stable contact identities
are present in nearly the same set of polls, they are probably one person — and
grouping them makes the person recognisable even when one radio rotates its
fingerprint away.

The whole risk here is OVER-MERGE. A fixed node sees the same ambient devices
(neighbour APs, always-on IoT) in almost every poll, so a naive "seen together"
would fuse the whole neighbourhood into one entity. Three guards make it safe, and
none touches scoring — this is display/identity only:

  * **Presence overlap (Jaccard).** Two contacts link only if the polls they share
    are a large fraction of the polls *either* was seen in. An always-present
    ambient device has near-zero Jaccard with a short-lived visitor (10 shared
    polls out of the ambient's 1000 ≈ 0.01), so it never links to one.
  * **Transience gate.** Only contacts that come and go (present in well under all
    polls) are link-eligible, so two unrelated fixtures that happen to both be
    always-on are not fused either.
  * **Signal-motion correlation (this file's addition).** Two radios carried by one
    moving person rise and fall in signal *together* as the person walks around;
    two unrelated devices that merely co-occur in a room vary independently. When a
    pair has moved enough for the correlation to be meaningful (enough joint samples
    AND real signal variance on both), a low/negative correlation VETOES the link —
    catching the co-occurring-but-unrelated case that presence overlap alone can't.
    Deliberately one-directional: it only ever vetoes on positive evidence of
    independent motion. A stationary pair (both signals flat) has no motion evidence,
    so the gate abstains and the presence/Jaccard guards stand alone — a still person's
    devices are never rejected for failing to move.

Everything is bounded (candidate set per poll, total tracked pairs, six running
sums per pair for the streaming correlation) so it cannot grow without limit or
stall the poll loop. Pure logic, no I/O — the orchestrator feeds it the present set
(and, optionally, each present contact's signal) each poll and reads back the clusters.
"""
from __future__ import annotations

from itertools import combinations
from math import sqrt
from typing import Iterable, Optional


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
        rssi_gate:        enable the signal-motion correlation veto (default on).
        min_rssi_corr:    when the gate applies, a pair must have at least this
                          Pearson correlation between its two signal series to link.
        min_corr_samples: joint valid-signal samples a pair needs before the gate
                          can apply (below this the gate abstains — too little motion
                          data to judge).
        min_rssi_std:     both series must vary by at least this many dB (std-dev)
                          for the correlation to be meaningful; a flat (stationary)
                          pair fails this, so the gate abstains rather than vetoing.
    """

    def __init__(self, *, min_polls: int = 12, min_jaccard: float = 0.6,
                 fixture_fraction: float = 0.5, min_obs_polls: int = 6,
                 min_fixture_polls: int = 20, max_present: int = 60,
                 max_pairs: int = 50000, rssi_gate: bool = True,
                 min_rssi_corr: float = 0.2, min_corr_samples: int = 10,
                 min_rssi_std: float = 2.0) -> None:
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
        self._rssi_gate = bool(rssi_gate)
        self._min_rssi_corr = float(min_rssi_corr)
        self._min_corr_samples = int(min_corr_samples)
        self._min_rssi_std = float(min_rssi_std)
        self._present_count: dict[str, int] = {}
        self._copresent: dict[tuple, int] = {}
        # Streaming Pearson accumulator per co-present pair: [n, Σa, Σb, Σaa, Σbb, Σab]
        # over the polls where BOTH radios reported a valid (non-placeholder) signal.
        # O(1) per pair per poll, six floats per pair — bounded with _copresent.
        self._rssi_acc: dict[tuple, list] = {}
        # Snapshot of the gate's verdicts on the LAST established_links() pass (a
        # fresh count each call, so repeated calls in one poll don't inflate it —
        # read by the overnight watch to quantify the gate's effect).
        self.gate_snapshot: dict = {"confirmed": 0, "vetoed": 0, "abstained": 0}
        self._total_polls = 0
        # Pairs known-linked from a prior session (loaded durable links). They are
        # treated as established immediately once BOTH are seen again this run.
        self._prior_links: set = set()

    def load_prior_links(self, pairs: Iterable[tuple]) -> None:
        """Seed links established in previous sessions (durable ``contact_links``)."""
        for a, b in pairs:
            if a and b:
                self._prior_links.add(_pair(a, b))

    def observe(self, present_keys: Iterable[str],
                signals: Optional[dict] = None) -> None:
        """Record one poll's set of currently-present contact identities.

        ``present_keys`` should already exclude un-trackable (``mac:``) identities —
        a rotating address can't anchor a person. A poll with more than
        ``max_present`` candidates skips pairing (still counts presence) so the
        pairwise step can't blow up in a crowd.

        ``signals`` is an optional ``{key: rssi_dBm}`` for the present contacts this
        poll; when given, each co-present pair whose BOTH radios reported a valid
        signal folds one joint sample into its streaming correlation. A signal of
        ``None`` or ``0`` is a Kismet placeholder (not a real measurement — see the
        project's zero-RSSI note) and is skipped, so the correlation is built only
        from genuine readings.
        """
        keys = sorted({k for k in present_keys if k and not k.startswith("mac:")})
        self._total_polls += 1
        for k in keys:
            self._present_count[k] = self._present_count.get(k, 0) + 1
        if len(keys) > self._max_present:
            return
        sig = signals or {}
        for a, b in combinations(keys, 2):
            # Don't start tracking a pair that already involves an obvious fixture —
            # keeps the pair table dominated by transient candidates.
            if self._is_fixture(a) or self._is_fixture(b):
                continue
            p = (a, b)
            self._copresent[p] = self._copresent.get(p, 0) + 1
            sa, sb = sig.get(a), sig.get(b)
            if sa not in (None, 0) and sb not in (None, 0):
                acc = self._rssi_acc.get(p)
                if acc is None:
                    acc = self._rssi_acc[p] = [0, 0.0, 0.0, 0.0, 0.0, 0.0]
                fa, fb = float(sa), float(sb)
                acc[0] += 1
                acc[1] += fa
                acc[2] += fb
                acc[3] += fa * fa
                acc[4] += fb * fb
                acc[5] += fa * fb
        if len(self._copresent) > self._max_pairs:
            self._evict_pairs()

    def _evict_pairs(self) -> None:
        keep = sorted(self._copresent.items(), key=lambda kv: kv[1],
                      reverse=True)[: self._max_pairs * 4 // 5]
        self._copresent = dict(keep)
        # Keep the correlation accumulator aligned with the surviving pairs.
        self._rssi_acc = {p: self._rssi_acc[p] for p in self._copresent
                          if p in self._rssi_acc}

    def rssi_correlation(self, a: str, b: str) -> Optional[tuple]:
        """Pearson correlation of a pair's two signal series → ``(r, std_a, std_b,
        n)``, or ``None`` if fewer than ``min_corr_samples`` joint readings exist.
        Public so the orchestrator can surface "moves together (r=…)" and the
        overnight watch can quantify the gate. ``r`` is clamped to [-1, 1]."""
        acc = self._rssi_acc.get(_pair(a, b))
        if acc is None or acc[0] < self._min_corr_samples:
            return None
        n, sa, sb, saa, sbb, sab = acc
        var_a = saa - sa * sa / n
        var_b = sbb - sb * sb / n
        if var_a <= 0 or var_b <= 0:
            return (0.0, 0.0, 0.0, int(n))
        cov = sab - sa * sb / n
        r = cov / sqrt(var_a * var_b)
        r = max(-1.0, min(1.0, r))
        return (r, sqrt(var_a / n), sqrt(var_b / n), int(n))

    def _rssi_gate_verdict(self, a: str, b: str) -> str:
        """The signal-motion gate for one pair. Pure (no side effects):
          * ``"confirmed"`` — meaningful motion AND correlated (allow, with evidence).
          * ``"vetoed"``    — meaningful motion but signals move independently (block).
          * ``"abstained"`` — gate off, too few joint samples, or too flat to judge
                              (allow; the presence/Jaccard guards stand alone).
        Only ``"vetoed"`` blocks a link."""
        if not self._rssi_gate:
            return "abstained"
        stats = self.rssi_correlation(a, b)
        if stats is None:
            return "abstained"
        r, std_a, std_b, _n = stats
        if std_a < self._min_rssi_std or std_b < self._min_rssi_std:
            return "abstained"              # too flat — no motion evidence either way
        return "confirmed" if r >= self._min_rssi_corr else "vetoed"

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

        NOTHING links until at least ``min_fixture_polls`` polls have been seen — until
        then we can't tell an always-on ambient device from a visitor present throughout
        a still-short window, and linking on that thin/early data fuses the whole
        persistent background into one false "person" (observed live: 12 ambient devices
        in the first few minutes). The FIXTURE exclusion applies to EVERY link, including
        one restored from a prior session: an always-on device is never part of a person,
        so a stale false link between two fixtures never re-clusters. A prior link still
        skips the co-presence/Jaccard *thresholds* (it was established before), so a
        genuinely returning person — transient, present together again — re-links quickly.
        """
        out = []
        snap = {"confirmed": 0, "vetoed": 0, "abstained": 0}
        if self._total_polls < self._min_fixture_polls:
            self.gate_snapshot = snap
            return out
        for (a, b), co in self._copresent.items():
            if self._is_fixture(a) or self._is_fixture(b):
                continue
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
            # Signal-motion gate: a pair that co-occurs but whose signals move
            # independently (they don't travel together) is vetoed. A prior link
            # is NOT exempt — if a returning "pair" now demonstrably moves apart,
            # that's real evidence they were never one person, worth catching.
            verdict = self._rssi_gate_verdict(a, b)
            snap[verdict] += 1
            if verdict == "vetoed":
                continue
            out.append((a, b, co, round(jac, 3)))
        self.gate_snapshot = snap
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
