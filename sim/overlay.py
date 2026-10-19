"""Routing code that Chord and Pastry share.

Both overlays forward a message hop by hop until it reaches the node that is
responsible for the key. Only the choice of the next hop is different, so
the rest is written once here and both use it:

    origin -> hop1 -> hop2 -> ... -> owner
       ^                              |
       +--------- route_done ---------+

Every forward is an rpc and the next node answers "ack" right away. If there
is no ack within HOP_TIMEOUT we think that node is dead, call forget() on it
and try another next hop. The owner sends "route_done" directly back to the
node that started the lookup. hops = number of forwards until the owner.

Chord and Pastry have to write:
    key_id(key)    turn a key string into an ID
    next_hop(p)    return (next node, extra fields) or None if we are the owner
    forget(addr)   remove a dead node from our tables
and they can also change:
    on_visit(p)    look at the message at each node (Pastry join uses it)
    deliver(p)     the result computed at the owner
"""

from collections import namedtuple

from sim.metrics import LookupRecord
from sim.node import BaseNode, NodeCrashed, RpcTimeout

# How one node stores another node: its ID and its network address.
NodeRef = namedtuple("NodeRef", ["id", "addr"])


class OverlayNode(BaseNode):
    HOP_TIMEOUT = 500.0        # ms to wait for the ack of one hop
    LOOKUP_TIMEOUT = 10000.0   # ms the origin waits for the answer
    MAX_HOPS = 64              # stop if a route goes on too long (loop)
    MAX_HOP_ATTEMPTS = 8       # how many dead next hops we try to skip

    def __init__(self, network, address, position):
        BaseNode.__init__(self, network, address, position)
        # lookups started by this node: lookup id -> simpy event
        self.waiting = {}

    # these are written in ChordNode / PastryNode
    def key_id(self, key):
        raise NotImplementedError

    def next_hop(self, p):
        raise NotImplementedError

    def forget(self, addr):
        raise NotImplementedError

    def on_visit(self, p):
        return p

    def deliver(self, p):
        return None

    def handle_route(self, msg):
        # reply "ack" at once, the routing continues in another process
        self.spawn(self.route_step(msg.payload))
        return "ack"

    def route_step(self, p):
        """What one node does with a routing message.
        p has: key, origin, lid (lookup id), hops, path, tag, purpose, data."""
        p = self.on_visit(p)
        for attempt in range(self.MAX_HOP_ATTEMPTS):
            if not self.alive:
                return
            step = self.next_hop(p)
            if step is None:
                # we are the owner of the key
                self.finish_route(p, True)
                return
            if p["hops"] >= self.MAX_HOPS:
                break
            next_node = step[0]
            extra = step[1]
            forward = dict(p)
            forward.update(extra)
            forward["hops"] = p["hops"] + 1
            forward["path"] = p["path"] + (next_node.addr,)
            try:
                yield from self.rpc(next_node.addr, "route", forward, tag=p["tag"],
                                    timeout=self.HOP_TIMEOUT)
                return
            except RpcTimeout:
                # next node looks dead, remove it and choose again
                self.forget(next_node.addr)
        self.finish_route(p, False)

    def finish_route(self, p, success):
        result = {"lid": p["lid"], "success": success, "hops": p["hops"], "path": p["path"]}
        if success:
            result["owner"] = self.ref
            result["result"] = self.deliver(p)
        else:
            result["owner"] = None
            result["result"] = None
        if p["origin"] == self.address:
            self.complete(result)     # we started it ourselves, no message needed
        else:
            self.send(p["origin"], "route_done", result, tag=p["tag"])

    def handle_route_done(self, msg):
        self.complete(msg.payload)

    def complete(self, result):
        event = self.waiting.get(result["lid"])
        if event is not None and not event.triggered:
            event.succeed(result)

    def route(self, key, tag, purpose="lookup", data=None, via=None):
        """Route to the node responsible for ID key. If via is given the
        route starts at that node (used for joining, since we are not in the
        overlay yet). Returns the result dict, or None on timeout."""
        lid = self.network.new_message_id()
        done = self.env.event()
        self.waiting[lid] = done
        p = {"key": key, "origin": self.address, "lid": lid, "hops": 0,
             "path": (self.address,), "tag": tag, "purpose": purpose, "data": data}
        if via is None:
            self.spawn(self.route_step(p))
        else:
            first = dict(p)
            first["hops"] = 1
            first["path"] = p["path"] + (via,)
            try:
                yield from self.rpc(via, "route", first, tag=tag, timeout=self.HOP_TIMEOUT)
            except RpcTimeout:
                del self.waiting[lid]
                return None
        timer = self.env.timeout(self.LOOKUP_TIMEOUT)
        yield self.env.any_of([done, timer])
        del self.waiting[lid]
        if not self.alive:
            raise NodeCrashed(self.address)
        if done.triggered:
            return done.value
        return None

    def lookup(self, key):
        """Find the node responsible for key. Saves a LookupRecord in the
        metrics and returns it."""
        start = self.env.now
        res = yield from self.route(self.key_id(key), tag="lookup")
        ok = res is not None and res["success"]
        hops = 0
        path = ()
        owner = None
        if res is not None:
            hops = res["hops"]
            path = res["path"]
        if ok:
            owner = res["owner"].addr
        record = LookupRecord(self.OVERLAY_NAME, self.address, key, start, self.env.now,
                              hops, owner, ok, path=path)
        self.metrics.record_lookup(record)
        return record
