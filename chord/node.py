"""A Chord node, written from Stoica et al. (SIGCOMM 2001; IEEE/ACM ToN 2003).

THE IDEA IN FOUR LINES
----------------------
- Node IDs and key IDs live on a circle of 2^m positions (m-bit SHA-1).
- Key k belongs to successor(k): the first node at or after k, clockwise.
- Each node keeps its successor list (next r nodes) and a finger table:
  finger[i] = successor(my_id + 2^i), i = 0..m-1 (halfway, quarter, ...).
- A lookup jumps to the finger that gets closest to the key *without
  passing it*; every jump at least halves the remaining distance.

Paper pseudocode -> methods in this file
----------------------------------------
  n.find_successor(id)            _find_successor() -> OverlayNode.route(),
                                  with next_hop() deciding each step
  n.closest_preceding_node(id)    _closest_preceding()
  n.join(n')                      join()
  n.stabilize(), n.notify(n')     stabilize(), handle_notify()
  n.fix_fingers()                 fix_fingers()
  n.check_predecessor()           check_predecessor()
  successor list (ToN §IV-E)      self.successors, _set_successors()
  voluntary departure (ToN §IV-E) leave()

DESIGN CHOICE: RECURSIVE ROUTING WITH PER-HOP ACKNOWLEDGEMENTS
--------------------------------------------------------------
The paper's pseudocode can run iteratively (the origin asks each hop in
turn) or recursively (each hop forwards). We route *recursively*, with the
same code as Pastry (sim/overlay.py), so the latency comparison is fair:

  origin -> hop1 -> hop2 -> ... -> predecessor(k) -> owner = successor(k)
     ^                                                      |
     +-------------------- "route_done" --------------------+

Each forward is an RPC whose reply is an immediate "ack". If no ack arrives
within HOP_TIMEOUT, the forwarder assumes the next hop is dead, removes it
from its tables, and tries the next best hop. The acks travel back in
parallel, so they add messages but not lookup latency.

HOP COUNTING: `hops` = number of forwarding steps until the message reaches
the owner (the node that will actually store/serve the key). This is the
number that matters to an application and the one we compare with Pastry.
See docs/LEARNING_GUIDE.md (Phase 2, §3) for how this relates to the
paper's "path length".
"""

from __future__ import annotations

from typing import Generator

from sim.ids import hash_to_id, in_interval
from sim.node import RpcTimeout
from sim.overlay import NodeRef, OverlayNode

__all__ = ["ChordNode", "NodeRef"]


class ChordNode(OverlayNode):
    """Routing mechanics (route / ack / timeouts / route_done / lookup) are
    inherited from OverlayNode; this class supplies Chord's next_hop()."""

    OVERLAY_NAME = "chord"

    def __init__(self, network, address, position, *, m: int = 32,
                 node_id: int | None = None, successor_list_len: int = 8,
                 stabilize_period: float = 1000.0, fix_fingers_period: float = 1000.0,
                 check_predecessor_period: float = 2000.0):
        super().__init__(network, address, position)
        if not 1 <= m <= 160:
            raise ValueError("m must be between 1 and 160")
        if successor_list_len < 1:
            raise ValueError("successor list needs at least one entry")
        self.m = m
        self.ring_size = 1 << m          # 2^m positions on the circle
        self.r = successor_list_len
        # The paper hashes the node's IP address; we hash the experiment seed
        # and the simulated address, so each seed gives a different ring.
        if node_id is None:
            node_id = hash_to_id(f"chord-node:{network.seed}:{address}", m)
        self.set_id(node_id)
        self.periods = {"stabilize": stabilize_period, "fix_fingers": fix_fingers_period,
                        "check_predecessor": check_predecessor_period}
        self.joined = False
        self._maintenance_started = False
        self._next_finger = 0

    def set_id(self, node_id: int) -> None:
        """(Re)assign this node's ring ID and reset its routing state."""
        if not 0 <= node_id < self.ring_size:
            raise ValueError("node id outside the ring")
        self.id = node_id
        self.ref = NodeRef(node_id, self.address)
        self.predecessor: NodeRef | None = None
        # Alone on the ring: my successor is myself.
        self.successors: list[NodeRef] = [self.ref]
        # finger[i] = successor(id + 2^i). None = unknown (or would be me).
        self.fingers: list[NodeRef | None] = [None] * self.m

    def __repr__(self) -> str:
        return f"<ChordNode addr={self.address} id={self.id} {'alive' if self.alive else 'dead'}>"

    # ------------------------------------------------------------------
    # Small helpers
    # ------------------------------------------------------------------
    def key_id(self, key: str) -> int:
        """Where a key sits on the ring."""
        return hash_to_id(key, self.m)

    def finger_start(self, i: int) -> int:
        """The point finger[i] is responsible for: (n + 2^i) mod 2^m."""
        return (self.id + (1 << i)) % self.ring_size

    @property
    def successor(self) -> NodeRef:
        return self.successors[0]

    def _alone(self) -> bool:
        return self.successor.addr == self.address

    def _is_responsible(self, key: int) -> bool:
        """Do *I* own `key`? True if key is in (predecessor, me].
        This check is a small addition to the paper's pseudocode (which only
        asks "is key in (me, successor]?"). It lets a node answer 0-hop
        lookups for its own keys, as a Pastry node does."""
        if self.predecessor is None:
            return self._alone()
        return in_interval(key, self.predecessor.id, self.id, inclusive_right=True)

    def _closest_preceding(self, key: int) -> NodeRef | None:
        """Paper: closest_preceding_node(id). Of all nodes I know (fingers and
        successor list), pick the one strictly between me and the key that is
        furthest round the ring from me, i.e. closest to the key without
        passing it."""
        best, best_dist = None, 0
        for ref in (*self.fingers, *self.successors):
            if ref is None or ref.addr == self.address:
                continue
            if in_interval(ref.id, self.id, key):
                dist = (ref.id - self.id) % self.ring_size
                if dist > best_dist:
                    best, best_dist = ref, dist
        return best

    def _set_successors(self, candidates) -> None:
        """Rebuild the successor list from an ordered list of candidates:
        drop duplicates, stop if the list wraps round to me, keep r entries."""
        out, seen = [], set()
        for ref in candidates:
            if ref is None:
                continue
            if ref.addr == self.address:
                break                      # wrapped all the way round the ring
            if ref.addr in seen:
                continue
            seen.add(ref.addr)
            out.append(ref)
            if len(out) == self.r:
                break
        self.successors = out or [self.ref]

    def _forget(self, addr: int) -> None:
        """Assume node `addr` is dead: remove it from all my routing state."""
        self.successors = [s for s in self.successors if s.addr != addr]
        self.fingers = [None if (f is not None and f.addr == addr) else f
                        for f in self.fingers]
        if self.predecessor is not None and self.predecessor.addr == addr:
            self.predecessor = None
        if not self.successors:
            # Every successor is gone. Fall back to the nearest live finger
            # clockwise; stabilize will then walk back to the true successor.
            known = [f for f in self.fingers if f is not None]
            if known:
                nearest = min(known, key=lambda f: (f.id - self.id) % self.ring_size)
                self.successors = [nearest]
            else:
                self.successors = [self.ref]   # isolated

    # ------------------------------------------------------------------
    # Routing (find_successor): Chord's rule for choosing the next hop
    # ------------------------------------------------------------------
    def next_hop(self, p: dict):
        """Called by OverlayNode at every node on the route.
        Returns None if I am the owner, else (next node, extra fields)."""
        key = p["key"]
        # Case 1: I am the owner (or the previous node determined that I am).
        if p.get("final") or self._alone() or self._is_responsible(key):
            return None
        # Case 2: key in (me, successor] -> my successor owns it.
        succ = self.successor
        if in_interval(key, self.id, succ.id, inclusive_right=True):
            return succ, {"final": True}
        # Case 3: jump to the closest preceding finger.
        return (self._closest_preceding(key) or succ), {"final": False}

    def forget(self, addr: int) -> None:
        self._forget(addr)

    def _find_successor(self, key: int, tag: str, via: int | None = None) -> Generator:
        """Paper: find_successor(id). Returns the route result dict
        (result["owner"] is successor(key)), or None on timeout."""
        return (yield from self.route(key, tag=tag, via=via))

    # ------------------------------------------------------------------
    # Joining
    # ------------------------------------------------------------------
    def join(self, bootstrap: int | None) -> Generator:
        """Paper: n.join(n'). Returns True on success.

        1. Ask the bootstrap node to find successor(my id).
        2. Copy my successor's successor list.
        3. Initialise my fingers (SIGCOMM'01 init_finger_table, minus the
           step that updates *other* nodes' fingers; the ToN version leaves
           that to their periodic fix_fingers, and so do we).
        4. Notify my successor that I might be its predecessor.
        Stabilization then finishes linking me in (my predecessor learns about
        me on its next stabilize round).
        """
        tag = "join"
        if bootstrap is None:               # the very first node: a ring of one
            self.predecessor = None
            self.successors = [self.ref]
            self.joined = True
            return True
        res = yield from self._find_successor(self.id, tag, via=bootstrap)
        if res is None or not res["success"]:
            return False
        succ: NodeRef = res["owner"]
        if succ.id == self.id:
            raise ValueError(f"ID collision: {self.id} already on the ring")
        self.successors = [succ]
        try:
            state = yield from self.rpc(succ.addr, "get_neighbors", tag=tag,
                                        timeout=self.HOP_TIMEOUT)
            self._set_successors([succ, *state["successors"]])
        except RpcTimeout:
            pass                            # stabilize will fill the list in later
        yield from self._init_fingers(tag)
        self.joined = True
        self.send(self.successor.addr, "notify", self.ref, tag=tag)
        return True

    def _init_fingers(self, tag: str) -> Generator:
        """finger[i] = successor(n + 2^i). Trick from the paper: if start_i is
        not past finger[i-1], then finger[i] = finger[i-1] with no lookup.
        Only about log2(N) of the m fingers need a real lookup."""
        prev = self.successor
        for i in range(self.m):
            start = self.finger_start(i)
            if in_interval(start, self.id, prev.id, inclusive_right=True):
                self.fingers[i] = prev
                continue
            res = yield from self._find_successor(start, tag)
            if res is None or not res["success"]:
                continue                     # leave it for fix_fingers
            owner: NodeRef = res["owner"]
            # The lookup ran on the ring *without* me. If I lie between start
            # and the answer, then I am successor(start): a useless finger.
            if in_interval(self.id, start, owner.id, inclusive_left=True):
                self.fingers[i] = None
                continue
            self.fingers[i] = prev = owner

    # ------------------------------------------------------------------
    # Periodic maintenance (stabilization)
    # ------------------------------------------------------------------
    def start_maintenance(self) -> None:
        """Start stabilize / fix_fingers / check_predecessor timers."""
        if self._maintenance_started:
            return
        self._maintenance_started = True
        self.run_periodically(self.periods["stabilize"], self.stabilize)
        self.run_periodically(self.periods["fix_fingers"], self.fix_fingers)
        self.run_periodically(self.periods["check_predecessor"], self.check_predecessor)

    def handle_get_neighbors(self, msg) -> dict:
        return {"predecessor": self.predecessor, "successors": tuple(self.successors)}

    def stabilize(self) -> Generator:
        """Paper: x = successor.predecessor; if x in (n, successor) then
        successor = x; successor.notify(n). Extended with the successor list:
        copy my successor's list, and skip successors that don't answer."""
        tag = "maintenance"
        for _attempt in range(self.r + 1):
            succ = self.successor
            if succ.addr == self.address:
                # Alone. If someone notified me, they are my successor too
                # (this is how a ring of one becomes a ring of two).
                if self.predecessor is not None:
                    self.successors = [self.predecessor]
                    continue
                return
            try:
                state = yield from self.rpc(succ.addr, "get_neighbors", tag=tag,
                                            timeout=self.HOP_TIMEOUT)
            except RpcTimeout:
                self._forget(succ.addr)
                continue
            x = state["predecessor"]
            if x is not None and x.addr != self.address and in_interval(x.id, self.id, succ.id):
                # Someone joined between me and my successor: adopt them.
                self._set_successors([x, succ, *state["successors"]])
            else:
                self._set_successors([succ, *state["successors"]])
            self.send(self.successor.addr, "notify", self.ref, tag=tag)
            return

    def handle_notify(self, msg) -> None:
        """Paper: n.notify(n'): if predecessor is nil or n' in (predecessor, n)
        then predecessor = n'."""
        cand: NodeRef = msg.payload
        if cand.addr == self.address:
            return
        if self.predecessor is None or in_interval(cand.id, self.predecessor.id, self.id):
            self.predecessor = cand

    def fix_fingers(self) -> Generator:
        """Paper: next = next + 1; finger[next] = find_successor(n + 2^next).
        Refreshes one finger per call, cycling through all m."""
        i = self._next_finger
        self._next_finger = (i + 1) % self.m
        start = self.finger_start(i)
        succ = self.successor
        if self._alone():
            self.fingers[i] = None
            return
        if in_interval(start, self.id, succ.id, inclusive_right=True):
            self.fingers[i] = succ           # answered locally, no messages
            return
        res = yield from self._find_successor(start, tag="maintenance")
        if res is not None and res["success"]:
            owner = res["owner"]
            self.fingers[i] = None if owner.addr == self.address else owner

    def check_predecessor(self) -> Generator:
        """Paper: if predecessor has failed, predecessor = nil."""
        pred = self.predecessor
        if pred is None:
            return
        try:
            yield from self.rpc(pred.addr, "ping", tag="maintenance", timeout=self.HOP_TIMEOUT)
        except RpcTimeout:
            if self.predecessor == pred:
                self.predecessor = None

    def handle_ping(self, msg) -> str:
        return "pong"

    # ------------------------------------------------------------------
    # Leaving
    # ------------------------------------------------------------------
    def leave(self) -> Generator:
        """Graceful departure (ToN §IV-E): tell my successor who its new
        predecessor is, and give my predecessor my successor list, then stop.
        (Phase 4 adds: hand my keys to my successor first.)"""
        if not self.alive:
            return
        succ, pred = self.successor, self.predecessor
        if succ.addr != self.address:
            self.send(succ.addr, "predecessor_leaving",
                      {"leaving": self.ref, "new_predecessor": pred}, tag="leave")
        if pred is not None and pred.addr != self.address:
            self.send(pred.addr, "successor_leaving",
                      {"leaving": self.ref, "successors": tuple(self.successors)}, tag="leave")
        self.alive = False
        self._pending.clear()
        yield self.env.timeout(0)   # leave() is a process, like join()

    def handle_predecessor_leaving(self, msg) -> None:
        leaving, new_pred = msg.payload["leaving"], msg.payload["new_predecessor"]
        self._forget(leaving.addr)
        if new_pred is not None and new_pred.addr != self.address:
            if self.predecessor is None or in_interval(new_pred.id, self.predecessor.id, self.id):
                self.predecessor = new_pred

    def handle_successor_leaving(self, msg) -> None:
        leaving = msg.payload["leaving"]
        self._forget(leaving.addr)
        handed_over = [s for s in msg.payload["successors"] if s.addr != leaving.addr]
        self._set_successors([*handed_over, *self.successors])

    # ------------------------------------------------------------------
    # Measurements
    # ------------------------------------------------------------------
    def routing_state_size(self) -> int:
        """Distinct other nodes in my fingers, successor list and predecessor.
        (The finger table has m slots, but only ~log2 N distinct entries.)"""
        known = {f.addr for f in self.fingers if f is not None}
        known.update(s.addr for s in self.successors)
        if self.predecessor is not None:
            known.add(self.predecessor.addr)
        known.discard(self.address)
        return len(known)
