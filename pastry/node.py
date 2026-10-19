"""Pastry node, implemented from the Pastry paper (Rowstron and Druschel,
Middleware 2001).

Main idea:
- IDs are 128 bit numbers, seen as 32 digits in base 2^b (b = 4, so hex)
- a key is stored at the live node whose ID is numerically closest to it
- each hop goes to a node that shares at least one more starting digit with
  the key, so a route takes about log16(N) hops
- for each routing table entry we keep the node with the lowest latency, so
  most hops are short

State of a node (section 2.1 of the paper):
  routing table - 32 rows x 16 columns. Row l has nodes that share the first
                  l digits with us and have digit l = column number.
  leaf set      - the 8 closest smaller IDs (leaf_left) and 8 closest larger
                  IDs (leaf_right). Used for the last hop.
  neighborhood  - the 32 nodes with lowest latency that we know about.

Routing (Table 1 in the paper) is in next_hop():
  1. if the key is in the range of the leaf set -> go to the closest leaf
  2. else use the routing table entry for the next digit
  3. else (the "rare case") go to any known node that has at least the same
     prefix length with the key and is numerically closer to it

Sending/acks/timeouts are in sim/overlay.py (same code as Chord).
The things we changed or simplified compared to the paper are listed in the
learning guide (Phase 3, section 3.8).
"""

from sim.ids import hash_to_id
from sim.node import RpcTimeout
from sim.overlay import NodeRef, OverlayNode

ID_BITS = 128


def unique_nodes(refs):
    """Remove duplicate nodes (same address), keep the first one, keep order."""
    result = []
    seen = set()
    for r in refs:
        if r.addr not in seen:
            seen.add(r.addr)
            result.append(r)
    return result


class PastryNode(OverlayNode):
    OVERLAY_NAME = "pastry"
    MAX_REPAIR_TRIES = 4       # how many nodes we ask to fix one table entry
    FAILED_MEMORY = 30000.0    # ms we remember that a node was dead

    def __init__(self, network, address, position, b=4, leaf_size=16,
                 neighborhood_size=32, node_id=None, maintenance_period=1000.0,
                 join_refine=True):
        OverlayNode.__init__(self, network, address, position)
        if ID_BITS % b != 0:
            raise ValueError("b must divide 128")
        if leaf_size < 2 or leaf_size % 2 != 0:
            raise ValueError("leaf_size must be an even number >= 2")
        self.b = b
        self.base = 2 ** b               # 16 possible digits when b = 4
        self.digits = ID_BITS // b       # 32 digits when b = 4
        self.space = 2 ** ID_BITS        # size of the ID space (it is a circle)
        self.half_leaf = leaf_size // 2
        self.m_size = neighborhood_size
        self.period = maintenance_period
        self.join_refine = join_refine
        if node_id is None:
            node_id = hash_to_id("pastry-node:" + str(network.seed) + ":" + str(address), ID_BITS)
        self.id = node_id
        self.ref = NodeRef(node_id, address)
        self.table = []
        for i in range(self.digits):
            self.table.append([None] * self.base)
        self.leaf_left = []       # smaller IDs, closest first
        self.leaf_right = []      # bigger IDs, closest first
        self.neighborhood = []    # lowest latency first
        self.joined = False
        self.maintenance_started = False
        self.leaf_probe_index = 0
        self.neigh_probe_index = 0
        self.repairs_running = set()
        # nodes we found dead: address -> time. Other nodes can still tell us
        # about them for a while, so we remember them and don't add them back.
        self.failed_nodes = {}
        self.pinging = set()      # nodes we are pinging before adding to leaf set

    def __repr__(self):
        if self.alive:
            state = "alive"
        else:
            state = "dead"
        return "<PastryNode addr=%d id=%032x %s>" % (self.address, self.id, state)

    # ----- ID functions -----

    def key_id(self, key):
        return hash_to_id(key, ID_BITS)

    def digit(self, ident, row):
        """Digit number row of an ID (row 0 is the first digit)."""
        return (ident >> (self.b * (self.digits - 1 - row))) & (self.base - 1)

    def shl(self, a, c):
        """Length of the shared prefix of a and c in digits (shl in the paper)."""
        x = a ^ c
        if x == 0:
            return self.digits
        return (ID_BITS - x.bit_length()) // self.b

    def dist(self, a, c):
        """Distance between two IDs on the circle."""
        d = abs(a - c)
        return min(d, self.space - d)

    def cw_dist(self, a, c):
        """Clockwise distance from a to c."""
        return (c - a) % self.space

    def closeness(self, ref, key):
        # used to find the numerically closest node. If two nodes are at the
        # same distance the smaller ID wins (same rule everywhere)
        return (self.dist(ref.id, key), ref.id)

    def proximity(self, ref):
        """Latency to another node. (Simplification: we take it from the
        simulator instead of measuring it with pings.)"""
        return self.network.latency(self.address, ref.addr)

    # ----- reading our state -----

    def leaves(self):
        """All leaf set nodes. In a small network a node can be on both
        sides, so we remove duplicates."""
        return unique_nodes(self.leaf_left + self.leaf_right)

    def table_entries(self):
        entries = []
        for row in self.table:
            for e in row:
                if e is not None:
                    entries.append(e)
        return entries

    def known_nodes(self):
        """Leaf set + routing table + neighborhood (what the rare case searches)."""
        return unique_nodes(self.leaves() + self.table_entries() + self.neighborhood)

    def leaf_set_covers_all(self):
        """True if we know all the nodes (no leaves, or the two sides overlap)."""
        if len(self.leaf_left) == 0 or len(self.leaf_right) == 0:
            return True
        left_addrs = set(r.addr for r in self.leaf_left)
        for r in self.leaf_right:
            if r.addr in left_addrs:
                return True
        return False

    def in_leaf_range(self, key):
        """Is key between our farthest left leaf and farthest right leaf?"""
        if self.leaf_set_covers_all():
            return True
        if self.cw_dist(self.id, key) <= self.cw_dist(self.id, self.leaf_right[-1].id):
            return True
        return self.cw_dist(key, self.id) <= self.cw_dist(self.leaf_left[-1].id, self.id)

    # ----- adding nodes to our state -----

    def consider(self, refs, leaf=True):
        """Try to add some nodes to our state. Each part keeps a node only if
        it is better than what is already there:
            leaf set     - numerically closer (skipped when leaf=False)
            table entry  - lower latency
            neighborhood - lower latency
        leaf=False is used for nodes we only heard about from others, those go
        into the leaf set only through check_and_add_leaves()."""
        all_refs = []
        for r in refs:
            if r is not None:
                all_refs.append(r)
        fresh = []
        for r in unique_nodes(all_refs):
            if r.addr != self.address and not self.failed_recently(r.addr):
                fresh.append(r)
        if len(fresh) == 0:
            return
        # leaf set: keep the half_leaf closest on each side
        if leaf:
            pool = unique_nodes(self.leaf_left + self.leaf_right + fresh)
            self.leaf_right = sorted(pool, key=lambda r: self.cw_dist(self.id, r.id))[:self.half_leaf]
            self.leaf_left = sorted(pool, key=lambda r: self.cw_dist(r.id, self.id))[:self.half_leaf]
        # routing table: row = shared prefix length, column = next digit
        for r in fresh:
            row = self.shl(self.id, r.id)
            if row >= self.digits:
                continue
            col = self.digit(r.id, row)
            cur = self.table[row][col]
            if cur is None:
                self.table[row][col] = r
            elif cur.addr != r.addr and self.proximity(r) < self.proximity(cur):
                self.table[row][col] = r
        # neighborhood: the m_size nodes with lowest latency
        pool = unique_nodes(self.neighborhood + fresh)
        self.neighborhood = sorted(pool, key=self.proximity)[:self.m_size]

    def check_and_add_leaves(self, refs):
        """For nodes we heard about from another node's leaf set.

        This info can be old (the other node may not know yet that some of
        them died). So if a node would go into our leaf set we ping it first
        and only add it if it answers. Without this, dead nodes kept coming
        back into the leaf sets after crashes.
        """
        fresh = []
        for r in refs:
            if r is not None and r.addr != self.address and not self.failed_recently(r.addr):
                fresh.append(r)
        current = set(r.addr for r in self.leaves())
        pool = unique_nodes(self.leaves() + fresh)
        right = sorted(pool, key=lambda r: self.cw_dist(self.id, r.id))[:self.half_leaf]
        left = sorted(pool, key=lambda r: self.cw_dist(r.id, self.id))[:self.half_leaf]
        for r in unique_nodes(left + right):
            if r.addr not in current and r.addr not in self.pinging:
                self.pinging.add(r.addr)
                self.spawn(self.ping_then_add(r))

    def ping_then_add(self, ref):
        try:
            yield from self.rpc(ref.addr, "ping", tag="maintenance", timeout=self.HOP_TIMEOUT)
            self.consider([ref])
        except RpcTimeout:
            self.failed_nodes[ref.addr] = self.env.now
        finally:
            self.pinging.discard(ref.addr)

    # ----- routing (Table 1 of the paper) -----

    def next_hop(self, p):
        key = p["key"]
        if key == self.id:
            return None
        # 1. key is in leaf set range: go to the numerically closest one
        if self.in_leaf_range(key):
            best = self.ref
            for r in self.leaves():
                if self.closeness(r, key) < self.closeness(best, key):
                    best = r
            if best.addr == self.address:
                return None
            return best, {}
        # 2. routing table: node with one more matching digit
        row = self.shl(key, self.id)
        entry = self.table[row][self.digit(key, row)]
        if entry is not None:
            return entry, {}
        # 3. rare case: a node with at least the same prefix length that is
        #    numerically closer to the key. We take the closest one.
        my_dist = self.dist(self.id, key)
        best = None
        for r in self.known_nodes():
            if self.shl(r.id, key) >= row and self.dist(r.id, key) < my_dist:
                if best is None or self.closeness(r, key) < self.closeness(best, key):
                    best = r
        if best is None:
            return None
        return best, {}

    # ----- joining (section 2.4 of the paper) -----

    def join(self, bootstrap):
        """Join using a bootstrap node that should be close to us.

        1. send a join message with key = our ID starting at the bootstrap
           node A. Each node on the way adds the table rows we can use and its
           neighborhood set (on_visit). The last node Z (numerically closest
           to us) adds its leaf set (deliver).
        2. build our state from all these nodes
        3. if join_refine: ask every node in our table and neighborhood for
           its state and keep nodes that are closer (locality step from the paper)
        4. tell everyone in our state that we joined
        """
        tag = "join"
        if bootstrap is None:
            self.joined = True
            return True
        data = {"joiner": self.ref, "cands": ()}
        res = yield from self.route(self.id, tag, purpose="join", data=data, via=bootstrap)
        if res is None or not res["success"]:
            return False
        self.consider(res["result"]["cands"])
        if self.join_refine:
            targets = []
            for r in unique_nodes(self.table_entries() + self.neighborhood):
                targets.append(r.addr)
            answers = yield from self.ask_many(targets, "get_state", None, tag)
            for state in answers:
                if state is not None:
                    self.consider([state[0]])            # the node that answered
                    self.consider(state, leaf=False)     # nodes it told us about
                    self.check_and_add_leaves(state)
        announcement = {"node": self.ref, "leaves": tuple(self.leaves())}
        for r in self.known_nodes():
            self.send(r.addr, "announce", announcement, tag=tag)
        self.joined = True
        return True

    def on_visit(self, p):
        """Every node on the join route gives the new node some of its state."""
        if p["purpose"] != "join":
            return p
        joiner = p["data"]["joiner"]
        usable_rows = min(self.shl(self.id, joiner.id), self.digits - 1)
        gift = [self.ref] + self.neighborhood
        for row in range(usable_rows + 1):
            for e in self.table[row]:
                if e is not None:
                    gift.append(e)
        new_data = dict(p["data"])
        new_data["cands"] = p["data"]["cands"] + tuple(gift)
        new_p = dict(p)
        new_p["data"] = new_data
        return new_p

    def deliver(self, p):
        if p["purpose"] == "join":
            # we are Z (closest to the new node), so add our leaf set too
            return {"cands": p["data"]["cands"] + tuple([self.ref] + self.leaves())}
        return None

    def handle_announce(self, msg):
        self.consider([msg.payload["node"]])          # the sender is alive
        self.check_and_add_leaves(msg.payload["leaves"])

    def handle_get_state(self, msg):
        return tuple([self.ref] + self.known_nodes())

    def handle_get_leafset(self, msg):
        return tuple([self.ref] + self.leaves())

    def handle_get_neighborhood(self, msg):
        return tuple([self.ref] + self.neighborhood)

    def handle_get_table_entry(self, msg):
        row, col = msg.payload
        return self.table[row][col]

    def handle_ping(self, msg):
        return "pong"

    def ask_many(self, addrs, kind, payload, tag):
        """Send the same rpc to many nodes at the same time and wait for all.
        Returns the answers (None for nodes that didn't answer)."""
        procs = []
        for a in addrs:
            procs.append(self.spawn(self.ask_one(a, kind, payload, tag)))
        if len(procs) > 0:
            yield self.env.all_of(procs)
        answers = []
        for p in procs:
            answers.append(p.value)
        return answers

    def ask_one(self, addr, kind, payload, tag):
        try:
            answer = yield from self.rpc(addr, kind, payload, tag=tag, timeout=self.HOP_TIMEOUT)
            return answer
        except RpcTimeout:
            self.forget(addr)
            return None

    # ----- failures (section 2.4 of the paper) -----

    def failed_recently(self, addr):
        when = self.failed_nodes.get(addr)
        if when is None:
            return False
        if self.env.now - when > self.FAILED_MEMORY:
            del self.failed_nodes[addr]
            return False
        return True

    def forget(self, addr, repair=True):
        """Remove a dead node from our state and start the repair from the
        paper for the places where it was."""
        self.failed_nodes[addr] = self.env.now
        in_left = False
        in_right = False
        for r in self.leaf_left:
            if r.addr == addr:
                in_left = True
        for r in self.leaf_right:
            if r.addr == addr:
                in_right = True
        self.leaf_left = [r for r in self.leaf_left if r.addr != addr]
        self.leaf_right = [r for r in self.leaf_right if r.addr != addr]
        in_neigh = False
        for r in self.neighborhood:
            if r.addr == addr:
                in_neigh = True
        self.neighborhood = [r for r in self.neighborhood if r.addr != addr]
        slots = []
        for row_i in range(len(self.table)):
            row = self.table[row_i]
            for col in range(len(row)):
                if row[col] is not None and row[col].addr == addr:
                    row[col] = None
                    slots.append((row_i, col))
        if not repair or not self.alive:
            return
        if in_left:
            self.start_repair(("leaf", "left"), self.repair_leaf("left", addr))
        if in_right:
            self.start_repair(("leaf", "right"), self.repair_leaf("right", addr))
        for slot in slots:
            self.start_repair(("table", slot), self.repair_table_slot(slot[0], slot[1], addr))
        if in_neigh:
            self.start_repair(("neigh",), self.repair_neighborhood(addr))

    def start_repair(self, job, gen):
        # only one repair of the same thing at a time
        if job in self.repairs_running:
            return
        self.repairs_running.add(job)
        self.spawn(self.run_repair(job, gen))

    def run_repair(self, job, gen):
        try:
            yield from gen
        finally:
            self.repairs_running.discard(job)

    def repair_leaf(self, side, dead):
        """From the paper: ask the live leaf with the largest index on that
        side for its leaf set."""
        if side == "left":
            members = self.leaf_left
        else:
            members = self.leaf_right
        if len(members) > 0:
            target = members[-1]
        elif len(self.leaves()) > 0:
            target = self.leaves()[-1]
        else:
            return
        try:
            leafset = yield from self.rpc(target.addr, "get_leafset", tag="maintenance",
                                          timeout=self.HOP_TIMEOUT)
        except RpcTimeout:
            self.forget(target.addr)   # also dead, this starts another repair
            return
        self.consider([leafset[0]])    # the node that answered is alive
        self.check_and_add_leaves([r for r in leafset if r.addr != dead])

    def repair_table_slot(self, row, col, dead):
        """From the paper: ask other nodes in the same row for their entry
        [row][col]. If that doesn't work, ask nodes in the next row."""
        asked = 0
        for r in range(row, self.digits):
            row_list = self.table[r]
            for c in range(len(row_list)):
                entry = row_list[c]
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
                if found is not None and found.addr != dead and found.addr != self.address:
                    self.consider([found], leaf=False)

    def repair_neighborhood(self, dead):
        """From the paper: ask another neighborhood member for its
        neighborhood set (we ask the closest one)."""
        if len(self.neighborhood) == 0:
            return
        target = self.neighborhood[0]
        try:
            theirs = yield from self.rpc(target.addr, "get_neighborhood", tag="maintenance",
                                         timeout=self.HOP_TIMEOUT)
        except RpcTimeout:
            self.forget(target.addr)
            return
        self.consider([theirs[0]])
        self.consider([r for r in theirs if r.addr != dead], leaf=False)

    # ----- periodic maintenance -----

    def start_maintenance(self):
        if self.maintenance_started:
            return
        self.maintenance_started = True
        self.run_periodically(self.period, self.maintain)

    def maintain(self):
        """Keep-alive round. The paper says leaf set neighbours send
        keep-alives but doesn't give the exact protocol, so we do this:
          1. get the leaf set of our left and right neighbour
          2. ping one other leaf set node (going round the list)
          3. ping one neighborhood node (going round the list)
        """
        tag = "maintenance"
        neighbours = unique_nodes(self.leaf_left[:1] + self.leaf_right[:1])
        for r in neighbours:
            try:
                leafset = yield from self.rpc(r.addr, "get_leafset", tag=tag,
                                              timeout=self.HOP_TIMEOUT)
                self.check_and_add_leaves(leafset)
            except RpcTimeout:
                self.forget(r.addr)
        # take both lists now (forget() below can change self.neighborhood)
        leaf_members = self.leaves()
        neigh_members = self.neighborhood
        if len(leaf_members) > 0:
            i = self.leaf_probe_index % len(leaf_members)
            self.leaf_probe_index = i + 1
            target = leaf_members[i]
            try:
                yield from self.rpc(target.addr, "ping", tag=tag, timeout=self.HOP_TIMEOUT)
            except RpcTimeout:
                self.forget(target.addr)
        if len(neigh_members) > 0:
            i = self.neigh_probe_index % len(neigh_members)
            self.neigh_probe_index = i + 1
            target = neigh_members[i]
            try:
                yield from self.rpc(target.addr, "ping", tag=tag, timeout=self.HOP_TIMEOUT)
            except RpcTimeout:
                self.forget(target.addr)

    # ----- leaving (the paper treats leaving like a failure, we added this) -----

    def leave(self):
        """Tell our leaf set that we leave and give them our leaf set so they
        can replace us. Table entries pointing to us get fixed later."""
        if not self.alive:
            return
        notice = {"node": self.ref, "leaves": tuple(self.leaves())}
        for r in self.leaves():
            self.send(r.addr, "leaving", notice, tag="leave")
        self.alive = False
        self.pending = {}
        yield self.env.timeout(0)

    def handle_leaving(self, msg):
        gone = msg.payload["node"]
        self.forget(gone.addr, repair=False)
        self.check_and_add_leaves([r for r in msg.payload["leaves"] if r.addr != gone.addr])

    def routing_state_size(self):
        """Number of different nodes in the table, leaf set and neighborhood."""
        return len(self.known_nodes())
