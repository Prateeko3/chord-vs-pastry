"""Helpers for building and checking Chord rings. EXPERIMENT/TEST CODE ONLY.

These functions look at *every* node at once (a "god's-eye view"). Real Chord
nodes never can, and ChordNode never calls anything in this file. We use the
global view for three things:

  1. create_nodes()  make N nodes with guaranteed-unique IDs.
  2. build_static()  give every node perfectly correct state instantly, as if
                     the ring had been stable forever. That lets us measure
                     lookup performance on 5,000 nodes without simulating
                     5,000 joins. (Dynamic joins are tested separately.)
  3. Oracle / check_ring() / finger_accuracy(): the ground truth to compare
                     lookup answers and routing state against.
"""

from __future__ import annotations

import bisect
from typing import Iterable

from chord.node import ChordNode, NodeRef
from sim.ids import hash_to_id
from sim.metrics import LookupRecord
from sim.network import Network


def create_nodes(net: Network, n: int, **node_kwargs) -> list[ChordNode]:
    """Create n ChordNodes with distinct IDs. With m=32 and a few thousand
    nodes a SHA-1 collision is very unlikely, but if one happens we re-hash
    with a salt rather than silently putting two nodes on one ID."""
    nodes: list[ChordNode] = []
    used: set[int] = set()
    for _ in range(n):
        node = net.create_node(ChordNode, **node_kwargs)
        salt = 0
        while node.id in used:
            salt += 1
            node.set_id(hash_to_id(f"chord-node:{net.seed}:{node.address}:{salt}", node.m))
        used.add(node.id)
        nodes.append(node)
    return nodes


class Oracle:
    """Ground truth: which live node really owns each key."""

    def __init__(self, nodes: Iterable[ChordNode]):
        live = sorted((n for n in nodes if n.alive), key=lambda n: n.id)
        if not live:
            raise ValueError("no live nodes")
        self.ring = live
        self.ids = [n.id for n in live]
        self.m = live[0].m

    def successor(self, ident: int) -> ChordNode:
        """First node with ID >= ident, wrapping round to the smallest ID."""
        i = bisect.bisect_left(self.ids, ident)
        return self.ring[i % len(self.ring)]

    def owner_of_key(self, key: str) -> ChordNode:
        return self.successor(hash_to_id(key, self.m))

    def index(self, node: ChordNode) -> int:
        return bisect.bisect_left(self.ids, node.id)

    def check(self, record: LookupRecord) -> bool:
        """Mark a LookupRecord correct/incorrect and return the verdict."""
        record.correct = record.success and record.owner == self.owner_of_key(record.key).address
        return record.correct


def build_static(nodes: list[ChordNode]) -> None:
    """Give every live node the exact state of a fully stabilized ring."""
    oracle = Oracle(nodes)
    ring, n = oracle.ring, len(oracle.ring)
    for idx, node in enumerate(ring):
        node.predecessor = ring[idx - 1].ref if n > 1 else None
        node.successors = ([ring[(idx + k) % n].ref for k in range(1, min(node.r, n - 1) + 1)]
                           or [node.ref])
        for i in range(node.m):
            f = oracle.successor(node.finger_start(i))
            node.fingers[i] = f.ref if f is not node else None
        node.joined = True


def check_ring(nodes: list[ChordNode]) -> list[str]:
    """List every live node whose successor or predecessor is wrong.
    An empty list means the ring is fully correct."""
    oracle = Oracle(nodes)
    ring, n = oracle.ring, len(oracle.ring)
    errors = []
    for idx, node in enumerate(ring):
        want_succ = ring[(idx + 1) % n].ref
        want_pred = ring[idx - 1].ref if n > 1 else None
        if n > 1 and node.successor != want_succ:
            errors.append(f"{node}: successor {node.successor} should be {want_succ}")
        if node.predecessor != want_pred:
            errors.append(f"{node}: predecessor {node.predecessor} should be {want_pred}")
        want_list = [ring[(idx + k) % n].ref for k in range(1, min(node.r, n - 1) + 1)]
        if n > 1 and node.successors != want_list:
            errors.append(f"{node}: successor list wrong")
    return errors


def finger_accuracy(nodes: list[ChordNode]) -> float:
    """Fraction of finger entries (over all live nodes) that equal the true
    successor(n + 2^i). A None entry counts as correct only if the true
    answer is the node itself."""
    oracle = Oracle(nodes)
    good = total = 0
    for node in oracle.ring:
        for i in range(node.m):
            true = oracle.successor(node.finger_start(i))
            expected: NodeRef | None = None if true is node else true.ref
            good += node.fingers[i] == expected
            total += 1
    return good / total
