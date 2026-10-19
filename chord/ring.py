"""Helper functions for tests and experiments with Chord.

These look at all nodes at once, which a real node can't do. The nodes never
call anything from this file, only the tests and experiment scripts do.

  create_nodes   - make n nodes with different IDs
  build_static   - set up correct fingers/successors for every node directly,
                   as if the ring was already stable. This is much faster than
                   doing thousands of joins when we only want to test lookups.
  Oracle         - knows the real owner of every key, to check lookups
  check_ring, finger_accuracy - check if the routing state is correct
"""

import bisect

from chord.node import ChordNode
from sim.ids import hash_to_id


def create_nodes(net, n, **node_args):
    """Create n Chord nodes. If two nodes get the same ID (very unlikely
    with m = 32) we hash again with a salt."""
    nodes = []
    used = set()
    for i in range(n):
        node = net.create_node(ChordNode, **node_args)
        salt = 0
        while node.id in used:
            salt += 1
            text = "chord-node:" + str(net.seed) + ":" + str(node.address) + ":" + str(salt)
            node.set_id(hash_to_id(text, node.m))
        used.add(node.id)
        nodes.append(node)
    return nodes


class Oracle:
    """Knows which live node really owns each key."""

    def __init__(self, nodes):
        live = [n for n in nodes if n.alive]
        if len(live) == 0:
            raise ValueError("no live nodes")
        live.sort(key=lambda n: n.id)
        self.ring = live
        self.ids = [n.id for n in live]
        self.m = live[0].m

    def successor(self, ident):
        """First node with ID >= ident (goes back to the start if needed)."""
        i = bisect.bisect_left(self.ids, ident)
        return self.ring[i % len(self.ring)]

    def owner_of_key(self, key):
        return self.successor(hash_to_id(key, self.m))

    def index(self, node):
        return bisect.bisect_left(self.ids, node.id)

    def check(self, record):
        """Set record.correct and return it."""
        record.correct = record.success and record.owner == self.owner_of_key(record.key).address
        return record.correct


def build_static(nodes):
    """Give every live node the correct state of a stable ring."""
    oracle = Oracle(nodes)
    ring = oracle.ring
    n = len(ring)
    for idx in range(n):
        node = ring[idx]
        if n > 1:
            node.predecessor = ring[idx - 1].ref
        else:
            node.predecessor = None
        succ_list = []
        for k in range(1, min(node.r, n - 1) + 1):
            succ_list.append(ring[(idx + k) % n].ref)
        if len(succ_list) == 0:
            succ_list = [node.ref]
        node.successors = succ_list
        for i in range(node.m):
            f = oracle.successor(node.finger_start(i))
            if f is node:
                node.fingers[i] = None
            else:
                node.fingers[i] = f.ref
        node.joined = True


def check_ring(nodes):
    """Returns a list of problems (wrong successor / predecessor / successor
    list). Empty list = ring is correct."""
    oracle = Oracle(nodes)
    ring = oracle.ring
    n = len(ring)
    errors = []
    for idx in range(n):
        node = ring[idx]
        want_succ = ring[(idx + 1) % n].ref
        if n > 1:
            want_pred = ring[idx - 1].ref
        else:
            want_pred = None
        if n > 1 and node.successor != want_succ:
            errors.append("%s: successor %s should be %s" % (node, node.successor, want_succ))
        if node.predecessor != want_pred:
            errors.append("%s: predecessor %s should be %s" % (node, node.predecessor, want_pred))
        want_list = []
        for k in range(1, min(node.r, n - 1) + 1):
            want_list.append(ring[(idx + k) % n].ref)
        if n > 1 and node.successors != want_list:
            errors.append("%s: successor list wrong" % node)
    return errors


def finger_accuracy(nodes):
    """Fraction of finger entries that are correct. A None entry is correct
    only when the real successor(n + 2^i) is the node itself."""
    oracle = Oracle(nodes)
    good = 0
    total = 0
    for node in oracle.ring:
        for i in range(node.m):
            real = oracle.successor(node.finger_start(i))
            if real is node:
                expected = None
            else:
                expected = real.ref
            if node.fingers[i] == expected:
                good += 1
            total += 1
    return good / total
