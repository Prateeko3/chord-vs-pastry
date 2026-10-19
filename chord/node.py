"""Chord node, implemented from the Chord paper (Stoica et al., SIGCOMM 2001
and the longer IEEE/ACM ToN 2003 version).

Main idea:
- node IDs and key IDs are on a ring of size 2^m
- key k is stored at successor(k), the first node at or after k on the ring
- every node has a successor list (next r nodes) and a finger table where
  finger[i] = successor(id + 2^i)
- a lookup jumps to the finger closest to the key without passing it, so the
  distance is at least halved every hop

Functions from the paper and where they are here:
  find_successor        find_successor() (uses route() from sim/overlay.py,
                        next_hop() decides each step)
  closest_preceding_node closest_preceding_node()
  join, stabilize, notify, fix_fingers, check_predecessor - same names
  successor list and leaving (ToN paper) - set_successors(), leave()

Lookups are recursive: each node forwards the request and the owner answers
the origin directly. Pastry uses the same code so the latency comparison is
fair. hops = number of forwards until the message reaches the owner.
"""

from sim.ids import hash_to_id, in_interval
from sim.node import RpcTimeout
from sim.overlay import NodeRef, OverlayNode


class ChordNode(OverlayNode):
    OVERLAY_NAME = "chord"

    def __init__(self, network, address, position, m=32, node_id=None,
                 successor_list_len=8, stabilize_period=1000.0,
                 fix_fingers_period=1000.0, check_predecessor_period=2000.0):
        OverlayNode.__init__(self, network, address, position)
        if m < 1 or m > 160:
            raise ValueError("m must be between 1 and 160")
        if successor_list_len < 1:
            raise ValueError("successor list needs at least one entry")
        self.m = m
        self.ring_size = 2 ** m
        self.r = successor_list_len
        # The paper hashes the IP address. We hash the seed and the address
        # so that every seed gives a different ring.
        if node_id is None:
            node_id = hash_to_id("chord-node:" + str(network.seed) + ":" + str(address), m)
        self.set_id(node_id)
        self.periods = {"stabilize": stabilize_period,
                        "fix_fingers": fix_fingers_period,
                        "check_predecessor": check_predecessor_period}
        self.joined = False
        self.maintenance_started = False
        self.next_finger = 0

    def set_id(self, node_id):
        """Give the node an ID and reset its routing state."""
        if node_id < 0 or node_id >= self.ring_size:
            raise ValueError("node id outside the ring")
        self.id = node_id
        self.ref = NodeRef(node_id, self.address)
        self.predecessor = None
        # when we are alone our successor is ourself
        self.successors = [self.ref]
        # finger[i] = successor(id + 2^i), None means we don't know (or it is us)
        self.fingers = [None] * self.m

    def __repr__(self):
        if self.alive:
            state = "alive"
        else:
            state = "dead"
        return "<ChordNode addr=%d id=%d %s>" % (self.address, self.id, state)

    # ----- helpers -----

    def key_id(self, key):
        return hash_to_id(key, self.m)

    def finger_start(self, i):
        # start of finger i = (n + 2^i) mod 2^m
        return (self.id + 2 ** i) % self.ring_size

    @property
    def successor(self):
        return self.successors[0]

    def is_alone(self):
        return self.successor.addr == self.address

    def is_responsible(self, key):
        """True if key is in (predecessor, me], i.e. we store this key.
        The paper only checks (me, successor]. We added this check so a node
        can answer lookups for its own keys with 0 hops, like Pastry does."""
        if self.predecessor is None:
            return self.is_alone()
        return in_interval(key, self.predecessor.id, self.id, inclusive_right=True)

    def closest_preceding_node(self, key):
        """From the paper: the known node (finger or successor) that is
        between us and the key and closest to the key."""
        best = None
        best_dist = 0
        for ref in self.fingers + self.successors:
            if ref is None or ref.addr == self.address:
                continue
            if in_interval(ref.id, self.id, key):
                dist = (ref.id - self.id) % self.ring_size
                if dist > best_dist:
                    best = ref
                    best_dist = dist
        return best

    def set_successors(self, candidates):
        """Make the successor list from a list of nodes in ring order.
        Skips duplicates, stops when we reach ourself again, keeps r nodes."""
        new_list = []
        seen = set()
        for ref in candidates:
            if ref is None:
                continue
            if ref.addr == self.address:
                break   # we went around the whole ring
            if ref.addr in seen:
                continue
            seen.add(ref.addr)
            new_list.append(ref)
            if len(new_list) == self.r:
                break
        if len(new_list) == 0:
            new_list = [self.ref]
        self.successors = new_list

    def forget(self, addr):
        """We think node addr is dead, so remove it from everything."""
        self.successors = [s for s in self.successors if s.addr != addr]
        for i in range(len(self.fingers)):
            if self.fingers[i] is not None and self.fingers[i].addr == addr:
                self.fingers[i] = None
        if self.predecessor is not None and self.predecessor.addr == addr:
            self.predecessor = None
        if len(self.successors) == 0:
            # all successors are gone, use the nearest finger instead.
            # stabilize will then find the real successor again.
            nearest = None
            nearest_dist = None
            for f in self.fingers:
                if f is None:
                    continue
                dist = (f.id - self.id) % self.ring_size
                if nearest is None or dist < nearest_dist:
                    nearest = f
                    nearest_dist = dist
            if nearest is not None:
                self.successors = [nearest]
            else:
                self.successors = [self.ref]

    # ----- routing -----

    def next_hop(self, p):
        """Chord's routing rule. Returns None if we are the owner."""
        key = p["key"]
        # we are the owner (or the last node already told us we are)
        if p.get("final") or self.is_alone() or self.is_responsible(key):
            return None
        # key is in (me, successor] so the successor is the owner
        succ = self.successor
        if in_interval(key, self.id, succ.id, inclusive_right=True):
            return succ, {"final": True}
        # otherwise jump to the closest preceding finger
        nxt = self.closest_preceding_node(key)
        if nxt is None:
            nxt = succ
        return nxt, {"final": False}

    def find_successor(self, key, tag, via=None):
        """Returns the route result (result["owner"] = successor(key)),
        or None if there was no answer."""
        result = yield from self.route(key, tag, via=via)
        return result

    # ----- join -----

    def join(self, bootstrap):
        """Join the ring using the bootstrap node. Returns True if it worked.

        1. ask the bootstrap node to find successor(my id)
        2. copy the successor list of my successor
        3. fill my fingers (init_finger_table from the 2001 paper; we don't
           update other nodes' fingers, their fix_fingers does that later,
           like in the ToN version)
        4. notify my successor
        """
        tag = "join"
        if bootstrap is None:
            # first node, ring with only us
            self.predecessor = None
            self.successors = [self.ref]
            self.joined = True
            return True
        res = yield from self.find_successor(self.id, tag, via=bootstrap)
        if res is None or not res["success"]:
            return False
        succ = res["owner"]
        if succ.id == self.id:
            raise ValueError("ID collision: " + str(self.id) + " is already used")
        self.successors = [succ]
        try:
            state = yield from self.rpc(succ.addr, "get_neighbors", tag=tag,
                                        timeout=self.HOP_TIMEOUT)
            self.set_successors([succ] + list(state["successors"]))
        except RpcTimeout:
            pass   # stabilize will fix the list later
        yield from self.init_fingers(tag)
        self.joined = True
        self.send(self.successor.addr, "notify", self.ref, tag=tag)
        return True

    def init_fingers(self, tag):
        """Fill finger[i] = successor(n + 2^i). If start i is not after
        finger[i-1] then finger[i] is the same node and we don't need a
        lookup (trick from the paper), so only about log2(N) lookups."""
        prev = self.successor
        for i in range(self.m):
            start = self.finger_start(i)
            if in_interval(start, self.id, prev.id, inclusive_right=True):
                self.fingers[i] = prev
                continue
            res = yield from self.find_successor(start, tag)
            if res is None or not res["success"]:
                continue   # fix_fingers will try again later
            owner = res["owner"]
            # The lookup was done on the ring without us. If we are between
            # start and the answer then we are the real successor(start).
            if in_interval(self.id, start, owner.id, inclusive_left=True):
                self.fingers[i] = None
                continue
            self.fingers[i] = owner
            prev = owner

    # ----- stabilization -----

    def start_maintenance(self):
        if self.maintenance_started:
            return
        self.maintenance_started = True
        self.run_periodically(self.periods["stabilize"], self.stabilize)
        self.run_periodically(self.periods["fix_fingers"], self.fix_fingers)
        self.run_periodically(self.periods["check_predecessor"], self.check_predecessor)

    def handle_get_neighbors(self, msg):
        return {"predecessor": self.predecessor, "successors": tuple(self.successors)}

    def stabilize(self):
        """From the paper: x = successor.predecessor, if x is in
        (n, successor) then successor = x, then notify the successor.
        We also copy the successor's list and skip dead successors."""
        tag = "maintenance"
        for attempt in range(self.r + 1):
            succ = self.successor
            if succ.addr == self.address:
                # we are alone. If someone notified us, it is our successor
                # too (this is how a ring of 1 node becomes 2 nodes)
                if self.predecessor is not None:
                    self.successors = [self.predecessor]
                    continue
                return
            try:
                state = yield from self.rpc(succ.addr, "get_neighbors", tag=tag,
                                            timeout=self.HOP_TIMEOUT)
            except RpcTimeout:
                self.forget(succ.addr)
                continue
            x = state["predecessor"]
            if x is not None and x.addr != self.address and in_interval(x.id, self.id, succ.id):
                # a new node joined between us and our successor
                self.set_successors([x, succ] + list(state["successors"]))
            else:
                self.set_successors([succ] + list(state["successors"]))
            self.send(self.successor.addr, "notify", self.ref, tag=tag)
            return

    def handle_notify(self, msg):
        """From the paper: if predecessor is nil or n' is in
        (predecessor, n) then predecessor = n'."""
        cand = msg.payload
        if cand.addr == self.address:
            return
        if self.predecessor is None or in_interval(cand.id, self.predecessor.id, self.id):
            self.predecessor = cand

    def fix_fingers(self):
        """From the paper: next = next + 1, finger[next] = find_successor(n + 2^next).
        Updates one finger each time it runs."""
        i = self.next_finger
        self.next_finger = (i + 1) % self.m
        start = self.finger_start(i)
        succ = self.successor
        if self.is_alone():
            self.fingers[i] = None
            return
        if in_interval(start, self.id, succ.id, inclusive_right=True):
            # successor is the answer, no messages needed
            self.fingers[i] = succ
            return
        res = yield from self.find_successor(start, "maintenance")
        if res is not None and res["success"]:
            owner = res["owner"]
            if owner.addr == self.address:
                self.fingers[i] = None
            else:
                self.fingers[i] = owner

    def check_predecessor(self):
        """From the paper: if the predecessor has failed, set it to nil."""
        pred = self.predecessor
        if pred is None:
            return
        try:
            yield from self.rpc(pred.addr, "ping", tag="maintenance", timeout=self.HOP_TIMEOUT)
        except RpcTimeout:
            if self.predecessor == pred:
                self.predecessor = None

    def handle_ping(self, msg):
        return "pong"

    # ----- leaving -----

    def leave(self):
        """Leave the ring (ToN paper): tell the successor its new predecessor
        and give the predecessor our successor list, then stop.
        (In phase 4 we also have to give our keys to the successor.)"""
        if not self.alive:
            return
        succ = self.successor
        pred = self.predecessor
        if succ.addr != self.address:
            self.send(succ.addr, "predecessor_leaving",
                      {"leaving": self.ref, "new_predecessor": pred}, tag="leave")
        if pred is not None and pred.addr != self.address:
            self.send(pred.addr, "successor_leaving",
                      {"leaving": self.ref, "successors": tuple(self.successors)}, tag="leave")
        self.alive = False
        self.pending = {}
        yield self.env.timeout(0)   # so that leave() is a process like join()

    def handle_predecessor_leaving(self, msg):
        leaving = msg.payload["leaving"]
        new_pred = msg.payload["new_predecessor"]
        self.forget(leaving.addr)
        if new_pred is not None and new_pred.addr != self.address:
            if self.predecessor is None or in_interval(new_pred.id, self.predecessor.id, self.id):
                self.predecessor = new_pred

    def handle_successor_leaving(self, msg):
        leaving = msg.payload["leaving"]
        self.forget(leaving.addr)
        given = [s for s in msg.payload["successors"] if s.addr != leaving.addr]
        self.set_successors(given + self.successors)

    # ----- for measurements -----

    def routing_state_size(self):
        """Number of different nodes in fingers, successor list and predecessor.
        The finger table has m entries but only about log2(N) are different."""
        known = set()
        for f in self.fingers:
            if f is not None:
                known.add(f.addr)
        for s in self.successors:
            known.add(s.addr)
        if self.predecessor is not None:
            known.add(self.predecessor.addr)
        known.discard(self.address)
        return len(known)
