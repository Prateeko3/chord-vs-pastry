"""A Pastry node, written from Rowstron & Druschel (Middleware 2001).

THE IDEA IN FOUR LINES
----------------------
- IDs are 128-bit numbers written as 32 digits in base 2^b (b = 4: hex).
- Key k belongs to the live node whose ID is *numerically closest* to k
  (on a circular ID space, like Chord's ring).
- Each hop forwards to a node sharing at least one more leading digit with
  the key, so a route takes about log_16(N) hops.
- Among all nodes that could fill a routing-table slot, a node keeps the one
  that is physically closest (lowest latency). So short hops come first, and
  the expensive long-distance hops are few.

STATE (paper section 2.1)
-------------------------
  routing table R  32 rows x 16 columns. Row l holds nodes that share the
                   first l digits with me and whose digit l is the column
                   number. (My own digit's column stays empty.)
  leaf set L       the |L|/2 numerically closest smaller IDs (leaf_left) and
                   |L|/2 closest larger IDs (leaf_right). Used for the last
                   hop(s) and for correctness.
  neighborhood M   the |M| physically closest nodes I know. Not used for
                   normal routing; used to keep locality.

ROUTING (paper Table 1) -> next_hop()
-------------------------------------
  if key within leaf-set range:  forward to the leaf (or me) numerically
                                 closest to the key
  elif R[l][key digit l] exists: forward there (l = shared prefix length)
  else ("rare case"):            forward to any known node T with
                                 shl(T, key) >= l and |T - key| < |me - key|
The forwarding mechanics (acks, timeouts, route_done) come from
sim/overlay.py, the same code Chord uses.

Every simplification relative to the paper is listed in
docs/LEARNING_GUIDE.md, Phase 3, section 3.8.
"""

from __future__ import annotations

from typing import Any, Generator, Iterable

from sim.ids import hash_to_id
from sim.node import RpcTimeout
from sim.overlay import NodeRef, OverlayNode

ID_BITS = 128


class PastryNode(OverlayNode):
    OVERLAY_NAME = "pastry"
    MAX_REPAIR_TRIES = 4      # nodes asked when repairing one routing-table slot
    FAILED_MEMORY = 30_000.0  # ms to refuse re-learning a node I saw fail

    def __init__(self, network, address, position, *, b: int = 4, leaf_size: int = 16,
                 neighborhood_size: int = 32, node_id: int | None = None,
                 maintenance_period: float = 1000.0, join_refine: bool = True):
        super().__init__(network, address, position)
        if ID_BITS % b:
            raise ValueError("b must divide 128")
        if leaf_size < 2 or leaf_size % 2:
            raise ValueError("leaf_size must be an even number >= 2")
        self.b = b
        self.base = 1 << b                 # digits per position: 16 for b = 4
        self.digits = ID_BITS // b         # digits per ID: 32 for b = 4
        self.space = 1 << ID_BITS          # size of the circular ID space
        self.half_leaf = leaf_size // 2
        self.m_size = neighborhood_size
        self.period = maintenance_period
        self.join_refine = join_refine
        if node_id is None:
            node_id = hash_to_id(f"pastry-node:{network.seed}:{address}", ID_BITS)
        self.id = node_id
        self.ref = NodeRef(node_id, address)
        self.table: list[list[NodeRef | None]] = [[None] * self.base for _ in range(self.digits)]
        self.leaf_left: list[NodeRef] = []     # smaller IDs, nearest first
        self.leaf_right: list[NodeRef] = []    # larger IDs, nearest first
        self.neighborhood: list[NodeRef] = []  # physically nearest first
        self.joined = False
        self._maintenance_started = False
        self._probe_leaf = 0                   # round-robin positions for probing
        self._probe_neigh = 0
        self._repairs_running: set = set()     # avoid duplicate repair jobs
        # Nodes I detected as failed (address -> time). Other nodes may still
        # gossip them to me for a while; without this memory a dead node
        # keeps being re-added to my leaf set and never goes away.
        self._failed: dict[int, float] = {}
        self._verifying: set[int] = set()      # gossip candidates being pinged

    def __repr__(self) -> str:
        return (f"<PastryNode addr={self.address} id={self.id:032x}"
                f" {'alive' if self.alive else 'dead'}>")

    # ------------------------------------------------------------------
    # ID arithmetic
    # ------------------------------------------------------------------
    def key_id(self, key: str) -> int:
        return hash_to_id(key, ID_BITS)

    def digit(self, ident: int, row: int) -> int:
        """Digit number `row` of an ID (row 0 = most significant digit)."""
        return (ident >> (self.b * (self.digits - 1 - row))) & (self.base - 1)

    def shl(self, a: int, c: int) -> int:
        """Length of the shared prefix of a and c, in digits (paper: shl)."""
        x = a ^ c
        return self.digits if x == 0 else (ID_BITS - x.bit_length()) // self.b

    def dist(self, a: int, c: int) -> int:
        """Numerical distance on the circular ID space."""
        d = abs(a - c)
        return min(d, self.space - d)

    def _cw(self, a: int, c: int) -> int:
        """Clockwise distance from a to c (how far c is 'above' a)."""
        return (c - a) % self.space

    def _closeness(self, ref: NodeRef, key: int) -> tuple[int, int]:
        """Sort key for "numerically closest to key". Ties (two nodes exactly
        equidistant) are broken by smaller ID, the same everywhere."""
        return (self.dist(ref.id, key), ref.id)

    def proximity(self, ref: NodeRef) -> float:
        """The proximity metric: network latency to that node. (Simplification:
        read from the simulator instead of measured by pinging.)"""
        return self.network.latency(self.address, ref.addr)

    # ------------------------------------------------------------------
    # Views of my state
    # ------------------------------------------------------------------
    def leaves(self) -> list[NodeRef]:
        """All leaf-set members, without duplicates (in a small network the
        same node can be on both sides)."""
        return list({r.addr: r for r in (*self.leaf_left, *self.leaf_right)}.values())

    def table_entries(self) -> list[NodeRef]:
        return [e for row in self.table for e in row if e is not None]

    def known_nodes(self) -> list[NodeRef]:
        """L u R u M, the set the paper's rare case searches."""
        found = {r.addr: r for r in (*self.leaves(), *self.table_entries(), *self.neighborhood)}
        return list(found.values())

    def _leaf_set_wraps(self) -> bool:
        """True if my leaf set covers the whole ring: I know every node
        (no leaves at all, or the two sides overlap)."""
        if not self.leaf_left or not self.leaf_right:
            return True
        return bool({r.addr for r in self.leaf_left} & {r.addr for r in self.leaf_right})

    def _in_leaf_range(self, key: int) -> bool:
        """Is key between my farthest left leaf and my farthest right leaf?"""
        if self._leaf_set_wraps():
            return True
        return (self._cw(self.id, key) <= self._cw(self.id, self.leaf_right[-1].id)
                or self._cw(key, self.id) <= self._cw(self.leaf_left[-1].id, self.id))

    # ------------------------------------------------------------------
    # Learning about nodes
    # ------------------------------------------------------------------
    def consider(self, refs: Iterable[NodeRef | None], *, leaf: bool = True) -> None:
        """Offer some nodes to all three parts of my state. Each part keeps
        a node only if it is better than what is there:
          leaf set     numerically closer   (skipped if leaf=False)
          table slot   physically closer (proximity neighbour selection)
          neighborhood physically closer
        Use leaf=False for second-hand news; the leaf set only takes
        second-hand nodes through offer_gossip(), which pings them first.
        """
        fresh = [r for r in {r.addr: r for r in refs if r is not None}.values()
                 if r.addr != self.address and not self._recently_failed(r.addr)]
        if not fresh:
            return
        # Leaf set: keep the half_leaf nearest on each side.
        if leaf:
            pool = {r.addr: r for r in (*self.leaf_left, *self.leaf_right, *fresh)}.values()
            self.leaf_right = sorted(pool, key=lambda r: self._cw(self.id, r.id))[:self.half_leaf]
            self.leaf_left = sorted(pool, key=lambda r: self._cw(r.id, self.id))[:self.half_leaf]
        # Routing table: slot (shared prefix length, next digit).
        for r in fresh:
            row = self.shl(self.id, r.id)
            if row >= self.digits:
                continue                       # same ID as me (cannot happen)
            col = self.digit(r.id, row)
            cur = self.table[row][col]
            if cur is None or (cur.addr != r.addr and self.proximity(r) < self.proximity(cur)):
                self.table[row][col] = r
        # Neighborhood set: the m_size physically closest.
        pool = {r.addr: r for r in (*self.neighborhood, *fresh)}.values()
        self.neighborhood = sorted(pool, key=self.proximity)[:self.m_size]

    def offer_gossip(self, refs: Iterable[NodeRef | None]) -> None:
        """Second-hand news about nodes (from another node's leaf set).

        It may be stale: the other node might not have noticed yet that some
        of these nodes have died. So a node that would *enter* my leaf set is
        pinged first and only added if it answers. Without this, dead nodes
        keep being passed around and leaf sets never become clean.
        """
        fresh = {r.addr: r for r in refs if r is not None and r.addr != self.address
                 and not self._recently_failed(r.addr)}
        current = {r.addr for r in self.leaves()}
        pool = {**{r.addr: r for r in self.leaves()}, **fresh}.values()
        right = sorted(pool, key=lambda r: self._cw(self.id, r.id))[:self.half_leaf]
        left = sorted(pool, key=lambda r: self._cw(r.id, self.id))[:self.half_leaf]
        for r in {x.addr: x for x in (*left, *right)}.values():
            if r.addr not in current and r.addr not in self._verifying:
                self._verifying.add(r.addr)
                self.spawn(self._verify_then_add(r))

    def _verify_then_add(self, ref: NodeRef) -> Generator:
        try:
            yield from self.rpc(ref.addr, "ping", tag="maintenance", timeout=self.HOP_TIMEOUT)
            self.consider([ref])
        except RpcTimeout:
            self._failed[ref.addr] = self.env.now    # dead: don't ask again soon
        finally:
            self._verifying.discard(ref.addr)

    # ------------------------------------------------------------------
    # Routing: the paper's Table 1
    # ------------------------------------------------------------------
    def next_hop(self, p: dict):
        key = p["key"]
        if key == self.id:
            return None
        # (1) Key within leaf-set range: go to the numerically closest leaf.
        if self._in_leaf_range(key):
            best = min([self.ref, *self.leaves()], key=lambda r: self._closeness(r, key))
            return None if best.addr == self.address else (best, {})
        # (2) Routing table: a node sharing one more digit with the key.
        row = self.shl(key, self.id)
        entry = self.table[row][self.digit(key, row)]
        if entry is not None:
            return entry, {}
        # (3) Rare case: any node at least as good in prefix, and numerically
        #     closer. We pick the numerically closest such node.
        my_dist = self.dist(self.id, key)
        best = None
        for r in self.known_nodes():
            if self.shl(r.id, key) >= row and self.dist(r.id, key) < my_dist:
                if best is None or self._closeness(r, key) < self._closeness(best, key):
                    best = r
        return (best, {}) if best is not None else None

    # ------------------------------------------------------------------
    # Joining (paper section 2.4, "Node arrival")
    # ------------------------------------------------------------------
    def join(self, bootstrap: int | None) -> Generator:
        """Join through `bootstrap`, which should be physically close to me.

        1. Route a "join" message with key = my ID, starting at the bootstrap
           A. Every node on the path adds the routing-table rows I can use
           (rows 0..shl(it, me)) and its neighborhood set (on_visit). The
           final node Z, numerically closest to me, adds its leaf set
           (deliver).
        2. Build my state from everything collected (consider()).
        3. (Optional, join_refine) Ask every node in my routing table and
           neighborhood set for its state, and keep any physically closer
           nodes (the paper's locality improvement step).
        4. Announce myself to every node in my state.
        """
        tag = "join"
        if bootstrap is None:
            self.joined = True
            return True
        res = yield from self.route(self.id, tag=tag, purpose="join",
                                    data={"joiner": self.ref, "cands": ()}, via=bootstrap)
        if res is None or not res["success"]:
            return False
        self.consider(res["result"]["cands"])
        if self.join_refine:
            targets = [r.addr for r in {r.addr: r for r in
                       (*self.table_entries(), *self.neighborhood)}.values()]
            for state in (yield from self._ask_all(targets, "get_state", None, tag)):
                if state is not None:
                    self.consider([state[0]])           # the node that answered
                    self.consider(state, leaf=False)    # what it knows: second-hand
                    self.offer_gossip(state)
        announcement = {"node": self.ref, "leaves": tuple(self.leaves())}
        for r in self.known_nodes():
            self.send(r.addr, "announce", announcement, tag=tag)
        self.joined = True
        return True

    def on_visit(self, p: dict) -> dict:
        """Each node on a join route contributes state the joiner can use."""
        if p["purpose"] != "join":
            return p
        joiner: NodeRef = p["data"]["joiner"]
        usable_rows = min(self.shl(self.id, joiner.id), self.digits - 1)
        gift = [self.ref, *self.neighborhood]
        for row in range(usable_rows + 1):
            gift.extend(e for e in self.table[row] if e is not None)
        data = {**p["data"], "cands": p["data"]["cands"] + tuple(gift)}
        return {**p, "data": data}

    def deliver(self, p: dict) -> Any:
        if p["purpose"] == "join":
            # I am Z, numerically closest to the joiner: add my leaf set.
            return {"cands": p["data"]["cands"] + (self.ref, *self.leaves())}
        return None

    def handle_announce(self, msg) -> None:
        self.consider([msg.payload["node"]])       # the sender is clearly alive
        self.offer_gossip(msg.payload["leaves"])

    def handle_get_state(self, msg) -> tuple:
        return (self.ref, *self.known_nodes())

    def handle_get_leafset(self, msg) -> tuple:
        return (self.ref, *self.leaves())

    def handle_get_neighborhood(self, msg) -> tuple:
        return (self.ref, *self.neighborhood)

    def handle_get_table_entry(self, msg):
        row, col = msg.payload
        return self.table[row][col]

    def handle_ping(self, msg) -> str:
        return "pong"

    def _ask_all(self, addrs: list[int], kind: str, payload: Any, tag: str) -> Generator:
        """Send the same RPC to many nodes in parallel; return their answers
        (None for any that timed out; those nodes are forgotten)."""
        def ask(addr):
            try:
                return (yield from self.rpc(addr, kind, payload, tag=tag,
                                            timeout=self.HOP_TIMEOUT))
            except RpcTimeout:
                self.forget(addr)
                return None
        procs = [self.spawn(ask(a)) for a in addrs]
        if procs:
            yield self.env.all_of(procs)
        return [p.value for p in procs]

    # ------------------------------------------------------------------
    # Failure handling (paper section 2.4, "Node departure")
    # ------------------------------------------------------------------
    def _recently_failed(self, addr: int) -> bool:
        when = self._failed.get(addr)
        if when is None:
            return False
        if self.env.now - when > self.FAILED_MEMORY:
            del self._failed[addr]
            return False
        return True

    def forget(self, addr: int, *, repair: bool = True) -> None:
        """Remove a node believed dead from all my state, and start the
        paper's repair procedure for whatever it occupied."""
        self._failed[addr] = self.env.now
        side = None
        if any(r.addr == addr for r in self.leaf_left):
            side = "left"
        if any(r.addr == addr for r in self.leaf_right):
            side = "right" if side is None else "both"
        self.leaf_left = [r for r in self.leaf_left if r.addr != addr]
        self.leaf_right = [r for r in self.leaf_right if r.addr != addr]
        in_neigh = any(r.addr == addr for r in self.neighborhood)
        self.neighborhood = [r for r in self.neighborhood if r.addr != addr]
        slots = []
        for row_i, row in enumerate(self.table):
            for col, e in enumerate(row):
                if e is not None and e.addr == addr:
                    row[col] = None
                    slots.append((row_i, col))
        if not repair or not self.alive:
            return
        if side in ("left", "both"):
            self._start_repair(("leaf", "left"), self._repair_leaf("left", addr))
        if side in ("right", "both"):
            self._start_repair(("leaf", "right"), self._repair_leaf("right", addr))
        for slot in slots:
            self._start_repair(("table", slot), self._repair_table_slot(*slot, addr))
        if in_neigh:
            self._start_repair(("neigh",), self._repair_neighborhood(addr))

    def _start_repair(self, job: tuple, gen: Generator) -> None:
        if job in self._repairs_running:
            gen.close()
            return
        self._repairs_running.add(job)

        def run():
            try:
                yield from gen
            finally:
                self._repairs_running.discard(job)
        self.spawn(run())

    def _repair_leaf(self, side: str, dead: int) -> Generator:
        """Paper: contact the live leaf with the largest index on the side of
        the failed node and ask for its leaf set."""
        members = self.leaf_left if side == "left" else self.leaf_right
        target = members[-1] if members else (self.leaves() or [None])[-1]
        if target is None:
            return
        try:
            leafset = yield from self.rpc(target.addr, "get_leafset", tag="maintenance",
                                          timeout=self.HOP_TIMEOUT)
        except RpcTimeout:
            self.forget(target.addr)           # that one is dead too: repeats
            return
        self.consider([leafset[0]])                # the node that just answered
        self.offer_gossip(r for r in leafset if r.addr != dead)

    def _repair_table_slot(self, row: int, col: int, dead: int) -> Generator:
        """Paper: ask another node in the same row for its entry R[row][col];
        if none helps, cast a wider net with the nodes in row + 1."""
        asked = 0
        for r in range(row, self.digits):
            for c, entry in enumerate(self.table[r]):
                if entry is None or (r == row and c == col):
                    continue
                if asked >= self.MAX_REPAIR_TRIES or self.table[row][col] is not None:
                    return
                asked += 1
                try:
                    found = yield from self.rpc(entry.addr, "get_table_entry", (row, col),
                                                tag="maintenance", timeout=self.HOP_TIMEOUT)
                except RpcTimeout:
                    self.forget(entry.addr)
                    continue
                if found is not None and found.addr not in (dead, self.address):
                    self.consider([found], leaf=False)

    def _repair_neighborhood(self, dead: int) -> Generator:
        """Paper: ask other neighborhood members for their neighborhood sets
        and keep the closest nodes (we ask the nearest remaining member)."""
        if not self.neighborhood:
            return
        target = self.neighborhood[0]
        try:
            theirs = yield from self.rpc(target.addr, "get_neighborhood", tag="maintenance",
                                         timeout=self.HOP_TIMEOUT)
        except RpcTimeout:
            self.forget(target.addr)
            return
        self.consider([theirs[0]])
        self.consider((r for r in theirs if r.addr != dead), leaf=False)

    # ------------------------------------------------------------------
    # Periodic maintenance
    # ------------------------------------------------------------------
    def start_maintenance(self) -> None:
        if self._maintenance_started:
            return
        self._maintenance_started = True
        self.run_periodically(self.period, self.maintain)

    def maintain(self) -> Generator:
        """One keep-alive round (the paper says leaf-set neighbours exchange
        keep-alives but doesn't fix a protocol; this is ours):
          1. swap leaf sets with my immediate left and right neighbours
             (detects their failure, and merges in nodes they know about)
          2. ping one other leaf-set member (round-robin)
          3. ping one neighborhood-set member (round-robin)
        """
        tag = "maintenance"
        neighbours = {r.addr: r for r in (self.leaf_left[:1] + self.leaf_right[:1])}
        for r in neighbours.values():
            try:
                leafset = yield from self.rpc(r.addr, "get_leafset", tag=tag,
                                              timeout=self.HOP_TIMEOUT)
                self.offer_gossip(leafset)
            except RpcTimeout:
                self.forget(r.addr)
        for members, attr in ((self.leaves(), "_probe_leaf"), (self.neighborhood, "_probe_neigh")):
            if not members:
                continue
            i = getattr(self, attr) % len(members)
            setattr(self, attr, i + 1)
            target = members[i]
            try:
                yield from self.rpc(target.addr, "ping", tag=tag, timeout=self.HOP_TIMEOUT)
            except RpcTimeout:
                self.forget(target.addr)

    # ------------------------------------------------------------------
    # Leaving (an addition: the paper treats every departure as a failure)
    # ------------------------------------------------------------------
    def leave(self) -> Generator:
        """Tell my leaf set I'm going, and hand them my leaf set so they can
        replace me at once. Routing-table entries elsewhere that point to me
        are repaired lazily, as for a failure."""
        if not self.alive:
            return
        notice = {"node": self.ref, "leaves": tuple(self.leaves())}
        for r in self.leaves():
            self.send(r.addr, "leaving", notice, tag="leave")
        self.alive = False
        self._pending.clear()
        yield self.env.timeout(0)

    def handle_leaving(self, msg) -> None:
        gone: NodeRef = msg.payload["node"]
        self.forget(gone.addr, repair=False)
        self.offer_gossip(r for r in msg.payload["leaves"] if r.addr != gone.addr)

    # ------------------------------------------------------------------
    # Measurements
    # ------------------------------------------------------------------
    def routing_state_size(self) -> int:
        """Distinct other nodes in my routing table, leaf set and
        neighborhood set (everything I maintain)."""
        return len(self.known_nodes())
