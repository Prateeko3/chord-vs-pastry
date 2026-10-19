"""Helpers for building and checking Pastry networks. EXPERIMENT/TEST CODE ONLY.

Like chord/ring.py, these use a god's-eye view that real nodes never have,
and PastryNode never calls them.

  create_nodes()      N PastryNodes with distinct IDs
  build_static()      fill every node's state directly. Unlike Chord, Pastry
                      has a *choice* in its routing table (any node with the
                      right prefix may fill a slot), so there are two modes:
                        proximity="optimal": each slot holds the physically
                          closest qualifying node (the ideal that Pastry's
                          locality heuristics approximate)
                        proximity="random": a random qualifying node
                          (Pastry with locality switched off)
                      Comparing the two isolates the effect of locality.
  nearest_node()      the physically closest live node (the paper assumes
                      a joining node knows one)
  Oracle              true owner of a key = numerically closest live node
  check_leafsets()    every live node's leaf set compared with the truth
  table_quality()     how complete and how close-to-optimal routing tables are
"""

from __future__ import annotations

import bisect
from collections import defaultdict

import numpy as np

from pastry.node import ID_BITS, PastryNode
from sim.ids import hash_to_id
from sim.metrics import LookupRecord
from sim.network import Network
from sim.overlay import NodeRef
from sim.rng import make_rng


def create_nodes(net: Network, n: int, **node_kwargs) -> list[PastryNode]:
    nodes, used = [], set()
    for _ in range(n):
        node = net.create_node(PastryNode, **node_kwargs)
        salt = 0
        while node.id in used:                 # astronomically unlikely at 128 bits
            salt += 1
            node.id = hash_to_id(f"pastry-node:{net.seed}:{node.address}:{salt}", ID_BITS)
            node.ref = NodeRef(node.id, node.address)
        used.add(node.id)
        nodes.append(node)
    return nodes


def nearest_node(newcomer: PastryNode, candidates: list[PastryNode]) -> PastryNode:
    """The live candidate with the lowest latency to `newcomer`."""
    live = [c for c in candidates if c.alive and c is not newcomer]
    return min(live, key=lambda c: newcomer.network.latency(newcomer.address, c.address))


class Oracle:
    """Ground truth: the numerically closest live node to any ID."""

    def __init__(self, nodes):
        live = sorted((n for n in nodes if n.alive), key=lambda n: n.id)
        if not live:
            raise ValueError("no live nodes")
        self.ring = live
        self.ids = [n.id for n in live]
        self.space = 1 << ID_BITS

    def _dist(self, a: int, c: int) -> int:
        d = abs(a - c)
        return min(d, self.space - d)

    def owner(self, ident: int) -> PastryNode:
        """Only the two ring neighbours of `ident` can be closest. Ties go to
        the smaller ID, matching PastryNode._closeness."""
        i = bisect.bisect_left(self.ids, ident)
        cands = [self.ring[i % len(self.ring)], self.ring[(i - 1) % len(self.ring)]]
        return min(cands, key=lambda n: (self._dist(n.id, ident), n.id))

    def owner_of_key(self, key: str) -> PastryNode:
        return self.owner(hash_to_id(key, ID_BITS))

    def check(self, record: LookupRecord) -> bool:
        record.correct = record.success and record.owner == self.owner_of_key(record.key).address
        return record.correct

    def expected_leaves(self, node: PastryNode) -> tuple[list[NodeRef], list[NodeRef]]:
        """The true (leaf_left, leaf_right) of a live node, nearest first."""
        i = self.ids.index(node.id)
        n = len(self.ring)
        k = min(node.half_leaf, n - 1)
        right = [self.ring[(i + j) % n].ref for j in range(1, k + 1)]
        left = [self.ring[(i - j) % n].ref for j in range(1, k + 1)]
        return left, right


def _latency_vector(node: PastryNode, xs: np.ndarray, ys: np.ndarray) -> np.ndarray:
    return node.network.topology.latencies_from(node.position, xs, ys)


def build_static(nodes: list[PastryNode], proximity: str = "optimal", seed: int = 0) -> None:
    """Fill every live node's leaf set, routing table and neighborhood set as
    if the network had been running for ever."""
    if proximity not in ("optimal", "random"):
        raise ValueError("proximity must be 'optimal' or 'random'")
    oracle = Oracle(nodes)
    ring = oracle.ring
    n = len(ring)
    b, digits = ring[0].b, ring[0].digits
    rng = make_rng(seed, f"pastry-static-{proximity}")
    xs = np.array([nd.position[0] for nd in ring])
    ys = np.array([nd.position[1] for nd in ring])
    ids = [nd.id for nd in ring]

    # Group node indices by ID prefix, for prefix lengths 1, 2, ... as needed.
    groups: list[dict[int, list[int]]] = [{0: list(range(n))}]   # length-0 prefix
    while True:
        level = len(groups)
        if level > digits or max(len(g) for g in groups[-1].values()) <= 1:
            break
        g = defaultdict(list)
        for idx, ident in enumerate(ids):
            g[ident >> (b * (digits - level))].append(idx)
        groups.append(dict(g))
    groups_np = [{k: np.array(v) for k, v in g.items()} for g in groups]

    for i, node in enumerate(ring):
        left, right = oracle.expected_leaves(node)
        node.leaf_left, node.leaf_right = left, right
        node.table = [[None] * node.base for _ in range(digits)]
        lat = _latency_vector(node, xs, ys)
        lat[i] = np.inf                       # never pick myself
        order = np.argsort(lat, kind="stable")[:min(node.m_size, n - 1)]
        node.neighborhood = [ring[j].ref for j in order]
        for row in range(len(groups) - 1):
            my_prefix = node.id >> (b * (digits - row))
            if len(groups[row].get(my_prefix, ())) <= 1:
                break                         # nobody else shares this prefix
            my_digit = node.digit(node.id, row)
            for col in range(node.base):
                if col == my_digit:
                    continue
                members = groups_np[row + 1].get((my_prefix << b) | col)
                if members is None:
                    continue
                if proximity == "optimal":
                    pick = members[np.argmin(lat[members])]
                else:
                    pick = members[rng.randrange(len(members))]
                node.table[row][col] = ring[pick].ref
        node.joined = True


def check_leafsets(nodes) -> list[str]:
    """Every live node whose leaf set differs from the truth."""
    oracle = Oracle(nodes)
    errors = []
    for node in oracle.ring:
        left, right = oracle.expected_leaves(node)
        if node.leaf_left != left or node.leaf_right != right:
            errors.append(f"{node}: leaf set wrong")
    return errors


def table_quality(nodes) -> dict[str, float]:
    """How good are the routing tables, compared with a perfect build?

    completeness  fraction of slots that *could* be filled (some live node
                  has that prefix) and are filled with a live node
    stale         fraction of filled slots pointing to a dead node
    latency_ratio mean over filled live slots of
                  latency(entry) / latency(physically closest possible entry)
                  (1.0 = optimal proximity choice everywhere)
    """
    oracle = Oracle(nodes)
    ring = oracle.ring
    alive = {nd.address for nd in ring}
    possible = filled = stale = 0
    ratios = []
    by_prefix: dict[tuple[int, int], list[PastryNode]] = defaultdict(list)
    sample = ring[0]
    b, digits = sample.b, sample.digits
    max_level = 0
    while max_level < digits and len({nd.id >> (b * (digits - max_level - 1)) for nd in ring}) < len(ring):
        max_level += 1
    for nd in ring:
        for level in range(1, max_level + 2):
            by_prefix[(level, nd.id >> (b * (digits - level)))].append(nd)
    for nd in ring:
        for row in range(max_level + 1):
            my_prefix = nd.id >> (b * (digits - row))
            for col in range(nd.base):
                if col == nd.digit(nd.id, row):
                    continue
                cands = by_prefix.get((row + 1, (my_prefix << b) | col))
                entry = nd.table[row][col]
                if entry is not None and entry.addr not in alive:
                    stale += 1
                if not cands:
                    continue
                possible += 1
                if entry is None or entry.addr not in alive:
                    continue
                filled += 1
                best = min(nd.network.latency(nd.address, c.address) for c in cands)
                got = nd.network.latency(nd.address, entry.addr)
                ratios.append(got / best if best > 0 else 1.0)
    total_filled = filled + stale
    return {
        "completeness": filled / possible if possible else 1.0,
        "stale": stale / total_filled if total_filled else 0.0,
        "latency_ratio": float(np.mean(ratios)) if ratios else float("nan"),
    }
