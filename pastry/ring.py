"""Helper functions for tests and experiments with Pastry.

Like chord/ring.py these look at all the nodes at once, so only tests and
experiment scripts use them, never the nodes.

  create_nodes   - make n Pastry nodes
  nearest_node   - the live node with lowest latency to a new node (the paper
                   assumes a new node knows a nearby node to join through)
  Oracle         - the real owner of a key (numerically closest live node)
  build_static   - fill all tables directly. Two options:
                     "optimal": every table entry is the closest possible node
                     "random":  a random node with the right prefix
                   Comparing the two shows how much locality helps.
  check_leafsets, table_quality - check how correct/good the state is
"""

import bisect

import numpy as np

from pastry.node import ID_BITS, PastryNode
from sim.ids import hash_to_id
from sim.overlay import NodeRef
from sim.rng import make_rng


def create_nodes(net, n, **node_args):
    nodes = []
    used = set()
    for i in range(n):
        node = net.create_node(PastryNode, **node_args)
        salt = 0
        # with 128 bit IDs this basically never happens
        while node.id in used:
            salt += 1
            text = "pastry-node:" + str(net.seed) + ":" + str(node.address) + ":" + str(salt)
            node.id = hash_to_id(text, ID_BITS)
            node.ref = NodeRef(node.id, node.address)
        used.add(node.id)
        nodes.append(node)
    return nodes


def nearest_node(newcomer, candidates):
    """Live node from candidates with the lowest latency to newcomer."""
    best = None
    best_latency = None
    for c in candidates:
        if not c.alive or c is newcomer:
            continue
        lat = newcomer.network.latency(newcomer.address, c.address)
        if best is None or lat < best_latency:
            best = c
            best_latency = lat
    return best


class Oracle:
    """Knows the numerically closest live node for any ID."""

    def __init__(self, nodes):
        live = [n for n in nodes if n.alive]
        if len(live) == 0:
            raise ValueError("no live nodes")
        live.sort(key=lambda n: n.id)
        self.ring = live
        self.ids = [n.id for n in live]
        self.space = 2 ** ID_BITS

    def dist(self, a, c):
        d = abs(a - c)
        return min(d, self.space - d)

    def owner(self, ident):
        """Only the two nodes around ident on the ring can be the closest.
        If both are at the same distance the smaller ID wins (same as the nodes)."""
        i = bisect.bisect_left(self.ids, ident)
        a = self.ring[i % len(self.ring)]
        b = self.ring[(i - 1) % len(self.ring)]
        if (self.dist(b.id, ident), b.id) < (self.dist(a.id, ident), a.id):
            return b
        return a

    def owner_of_key(self, key):
        return self.owner(hash_to_id(key, ID_BITS))

    def check(self, record):
        record.correct = record.success and record.owner == self.owner_of_key(record.key).address
        return record.correct

    def expected_leaves(self, node):
        """The correct (leaf_left, leaf_right) of a node, closest first."""
        i = self.ids.index(node.id)
        n = len(self.ring)
        k = min(node.half_leaf, n - 1)
        right = []
        left = []
        for j in range(1, k + 1):
            right.append(self.ring[(i + j) % n].ref)
            left.append(self.ring[(i - j) % n].ref)
        return left, right


def build_static(nodes, proximity="optimal", seed=0):
    """Fill the leaf set, routing table and neighborhood set of every live
    node as if the network had been running for a long time."""
    if proximity != "optimal" and proximity != "random":
        raise ValueError("proximity must be 'optimal' or 'random'")
    oracle = Oracle(nodes)
    ring = oracle.ring
    n = len(ring)
    b = ring[0].b
    digits = ring[0].digits
    rng = make_rng(seed, "pastry-static-" + proximity)
    xs = np.array([nd.position[0] for nd in ring])
    ys = np.array([nd.position[1] for nd in ring])
    ids = [nd.id for nd in ring]

    # groups[level] = {prefix of length level: list of node indexes}
    # we stop when every prefix has only one node
    groups = [{0: list(range(n))}]
    while True:
        level = len(groups)
        if level > digits:
            break
        biggest = 0
        for members in groups[-1].values():
            biggest = max(biggest, len(members))
        if biggest <= 1:
            break
        g = {}
        for idx in range(n):
            prefix = ids[idx] >> (b * (digits - level))
            if prefix not in g:
                g[prefix] = []
            g[prefix].append(idx)
        groups.append(g)
    # numpy arrays so we can index the latency vector with them
    groups_np = []
    for g in groups:
        g_np = {}
        for prefix in g:
            g_np[prefix] = np.array(g[prefix])
        groups_np.append(g_np)

    for i in range(n):
        node = ring[i]
        left, right = oracle.expected_leaves(node)
        node.leaf_left = left
        node.leaf_right = right
        node.table = []
        for r in range(digits):
            node.table.append([None] * node.base)
        lat = node.network.topology.latencies_from(node.position, xs, ys)
        lat[i] = np.inf   # don't pick ourself
        # stable sort so that equal latencies keep the same order every run
        order = np.argsort(lat, kind="stable")[:min(node.m_size, n - 1)]
        node.neighborhood = [ring[j].ref for j in order]
        for row in range(len(groups) - 1):
            my_prefix = node.id >> (b * (digits - row))
            if len(groups[row].get(my_prefix, [])) <= 1:
                break   # no other node has this prefix
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


def check_leafsets(nodes):
    """List of live nodes whose leaf set is not correct."""
    oracle = Oracle(nodes)
    errors = []
    for node in oracle.ring:
        left, right = oracle.expected_leaves(node)
        if node.leaf_left != left or node.leaf_right != right:
            errors.append("%s: leaf set wrong" % node)
    return errors


def table_quality(nodes):
    """How good the routing tables are compared to a perfect build.

    completeness  - fraction of entries that could be filled (some live node
                    has that prefix) and are filled with a live node
    stale         - fraction of filled entries that point to a dead node
    latency_ratio - average of latency(entry) / latency(best possible entry),
                    1.0 means every entry is the closest possible node
    """
    oracle = Oracle(nodes)
    ring = oracle.ring
    alive = set(nd.address for nd in ring)
    possible = 0
    filled = 0
    stale = 0
    ratios = []
    b = ring[0].b
    digits = ring[0].digits
    # find how many rows can have entries (until every prefix is unique)
    max_level = 0
    while max_level < digits:
        prefixes = set()
        for nd in ring:
            prefixes.add(nd.id >> (b * (digits - max_level - 1)))
        if len(prefixes) == len(ring):
            break
        max_level += 1
    by_prefix = {}
    for nd in ring:
        for level in range(1, max_level + 2):
            k = (level, nd.id >> (b * (digits - level)))
            if k not in by_prefix:
                by_prefix[k] = []
            by_prefix[k].append(nd)
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
                best = None
                for c in cands:
                    lat = nd.network.latency(nd.address, c.address)
                    if best is None or lat < best:
                        best = lat
                got = nd.network.latency(nd.address, entry.addr)
                if best > 0:
                    ratios.append(got / best)
                else:
                    ratios.append(1.0)
    result = {}
    if possible > 0:
        result["completeness"] = filled / possible
    else:
        result["completeness"] = 1.0
    if filled + stale > 0:
        result["stale"] = stale / (filled + stale)
    else:
        result["stale"] = 0.0
    if len(ratios) > 0:
        result["latency_ratio"] = float(np.mean(ratios))
    else:
        result["latency_ratio"] = float("nan")
    return result
