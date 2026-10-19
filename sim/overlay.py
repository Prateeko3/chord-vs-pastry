"""Recursive routing machinery shared by Chord and Pastry.

Both overlays route a message hop by hop towards the node responsible for a
key. The *only* thing that differs between them is how each node picks the
next hop. So the forwarding mechanics live here, written once:

    origin --route--> hop1 --route--> hop2 ... --route--> owner
       ^                                                    |
       +------------------- route_done ---------------------+

- Each "route" message is an RPC whose reply is an immediate "ack". If no ack
  arrives within HOP_TIMEOUT, the forwarder calls forget(next_hop) and asks
  next_hop() again (which now picks a different node).
- The node where next_hop() returns None is the destination ("owner"). It
  runs deliver() (used by the key-value store in Phase 4) and sends
  "route_done" straight back to the origin.
- `hops` counts forwarding steps from the origin to the owner, for both
  overlays identically.

This shared code is a fairness guarantee: timeouts, retries, acks, hop
counting and latency accounting can't differ between Chord and Pastry.

A subclass must provide:
    key_id(key)      map an application key (str) into its ID space
    next_hop(p)      (NodeRef, extra_fields) to forward, or None = "it's me"
    forget(addr)     remove a node believed dead from all routing state
and may override:
    on_visit(p)      inspect/extend the message at each node (Pastry's join
                     uses this to collect routing-table rows along the path)
    deliver(p)       compute a result at the destination
"""

from __future__ import annotations

from typing import Any, Generator, NamedTuple

from sim.metrics import LookupRecord
from sim.node import BaseNode, NodeCrashed, RpcTimeout


class NodeRef(NamedTuple):
    """How one node refers to another: its overlay ID and network address.
    Immutable, so passing refs between nodes can't create shared mutable
    state by accident."""
    id: int
    addr: int


class OverlayNode(BaseNode):
    HOP_TIMEOUT = 500.0        # ms to wait for a per-hop ack
    LOOKUP_TIMEOUT = 10_000.0  # ms the origin waits for the final answer
    MAX_HOPS = 64              # safety net against loops in broken states
    MAX_HOP_ATTEMPTS = 8       # dead next hops one node will skip before giving up

    # Set by subclasses in __init__:
    id: int
    ref: NodeRef

    def __init__(self, network, address, position):
        super().__init__(network, address, position)
        # Routes this node started and is waiting on: lookup id -> event.
        self._waiting: dict[int, Any] = {}

    # --- to be provided by each overlay ----------------------------------
    def key_id(self, key: str) -> int:
        raise NotImplementedError

    def next_hop(self, p: dict) -> tuple[NodeRef, dict] | None:
        raise NotImplementedError

    def forget(self, addr: int) -> None:
        raise NotImplementedError

    def on_visit(self, p: dict) -> dict:
        return p

    def deliver(self, p: dict) -> Any:
        return None

    # --- the machinery -----------------------------------------------------
    def handle_route(self, msg):
        """A routing step arrives: acknowledge at once, keep routing in the
        background (the ack only means "I've got it")."""
        self.spawn(self._route(msg.payload))
        return "ack"

    def _route(self, p: dict) -> Generator:
        """This node's part of a route. `p` holds: key, origin, lid (route id),
        hops, path (addresses visited), tag, purpose, data, plus any
        overlay-specific fields added through next_hop()'s `extra`."""
        p = self.on_visit(p)
        for _attempt in range(self.MAX_HOP_ATTEMPTS):
            if not self.alive:
                return
            step = self.next_hop(p)
            if step is None:                       # I am the destination
                self._finish_route(p, success=True)
                return
            if p["hops"] >= self.MAX_HOPS:
                break
            nxt, extra = step
            forward = {**p, **extra, "hops": p["hops"] + 1, "path": p["path"] + (nxt.addr,)}
            try:
                yield from self.rpc(nxt.addr, "route", forward, tag=p["tag"],
                                    timeout=self.HOP_TIMEOUT)
                return                             # handed over successfully
            except RpcTimeout:
                self.forget(nxt.addr)              # looks dead: choose again
        self._finish_route(p, success=False)

    def _finish_route(self, p: dict, success: bool) -> None:
        result = {"lid": p["lid"], "success": success, "hops": p["hops"],
                  "path": p["path"], "owner": self.ref if success else None,
                  "result": self.deliver(p) if success else None}
        if p["origin"] == self.address:
            self._complete(result)                 # I asked myself: no message
        else:
            self.send(p["origin"], "route_done", result, tag=p["tag"])

    def handle_route_done(self, msg) -> None:
        self._complete(msg.payload)

    def _complete(self, result: dict) -> None:
        event = self._waiting.get(result["lid"])
        if event is not None and not event.triggered:
            event.succeed(result)

    def route(self, key: int, *, tag: str, purpose: str = "lookup", data: Any = None,
              via: int | None = None) -> Generator:
        """Route to the node responsible for ID `key`. Start here, or at node
        `via` (used when joining: we are not part of the overlay yet).
        Returns the result dict, or None if no answer arrived in time."""
        lid = self.network.next_message_id()
        done = self._waiting[lid] = self.env.event()
        p = {"key": key, "origin": self.address, "lid": lid, "hops": 0,
             "path": (self.address,), "tag": tag, "purpose": purpose, "data": data}
        if via is None:
            self.spawn(self._route(p))
        else:
            first = {**p, "hops": 1, "path": p["path"] + (via,)}
            try:
                yield from self.rpc(via, "route", first, tag=tag, timeout=self.HOP_TIMEOUT)
            except RpcTimeout:
                self._waiting.pop(lid, None)
                return None
        yield done | self.env.timeout(self.LOOKUP_TIMEOUT)
        self._waiting.pop(lid, None)
        if not self.alive:
            raise NodeCrashed(self.address)
        return done.value if done.triggered else None

    def lookup(self, key: str) -> Generator:
        """Shared interface: find the node responsible for `key`.
        Records and returns a LookupRecord."""
        start = self.env.now
        res = yield from self.route(self.key_id(key), tag="lookup")
        ok = res is not None and res["success"]
        record = LookupRecord(
            overlay=self.OVERLAY_NAME, origin=self.address, key=key,
            start_time=start, end_time=self.env.now,
            hops=res["hops"] if res else 0,
            owner=res["owner"].addr if ok else None, success=ok,
            path=res["path"] if res else ())
        self.metrics.record_lookup(record)
        return record
