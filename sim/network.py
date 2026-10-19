"""The simulated network: it carries messages between nodes after a delay.

Nothing here uses real sockets. A "message" is a Python object, and "sending"
it means asking the simpy clock to hand it to the destination node
`latency` milliseconds from now.

    delay = topology.latency(src_position, dst_position)    # distance in ms
          + uniform(0, jitter)                               # optional noise

With probability `loss_rate` a message is silently thrown away, like a lost
UDP packet. A message that arrives at a node that has crashed (or left) is
also dropped. Either way the sender gets no reply and has to notice by a
timeout, exactly as on a real network.

Simplifications (worth knowing):
- No bandwidth limit or queueing: any number of messages can be in flight,
  and nodes process messages instantly (zero CPU time).
- Latency is symmetric (A->B costs the same as B->A).
- Messages from one sender can overtake each other if jitter is on.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable, TypeVar

import simpy

from sim.metrics import Metrics
from sim.rng import make_rng
from sim.topology import Position, Topology, UniformSquare

if TYPE_CHECKING:
    from sim.node import BaseNode

N = TypeVar("N", bound="BaseNode")


@dataclass(slots=True)
class Message:
    """One message on the wire.

    kind          what the message asks for, e.g. "ping" or "find_successor".
                  The receiving node handles it with method `handle_<kind>`.
    payload       the arguments (a dict for requests; any value for replies).
    tag           why it was sent ("lookup", "join", "maintenance", ...), so
                  the metrics can answer questions like "messages per join".
    expects_reply True for an RPC request (the sender is waiting).
    is_reply      True for the answer to an RPC; `reply_to` is the msg_id of
                  the request it answers.
    """

    src: int
    dst: int
    kind: str
    payload: Any
    msg_id: int
    sent_at: float
    tag: str = "other"
    expects_reply: bool = False
    is_reply: bool = False
    reply_to: int | None = None


class Network:
    """Owns the simpy environment, all nodes, and the delivery of messages."""

    def __init__(self, env: simpy.Environment, *, seed: int,
                 topology: Topology | None = None, jitter: float = 0.0,
                 loss_rate: float = 0.0, metrics: Metrics | None = None):
        if jitter < 0:
            raise ValueError("jitter must be >= 0")
        if not 0.0 <= loss_rate < 1.0:
            raise ValueError("loss_rate must be in [0, 1)")
        self.env = env
        self.seed = seed
        self.jitter = jitter
        self.loss_rate = loss_rate
        self.metrics = metrics if metrics is not None else Metrics()
        # Separate random streams (see sim/rng.py for why):
        self.topology = (topology or UniformSquare()).bind(make_rng(seed, "topology-layout"))
        self._position_rng = make_rng(seed, "topology-positions")
        self._link_rng = make_rng(seed, "network-links")   # jitter and loss
        self._nodes: dict[int, "BaseNode"] = {}
        self._addresses = itertools.count()   # 0, 1, 2, ... like IP addresses
        self._msg_ids = itertools.count()

    # --- nodes ------------------------------------------------------------
    def create_node(self, cls: Callable[..., N], position: Position | None = None,
                    **kwargs: Any) -> N:
        """Make a node of class `cls`, give it a fresh address and a position
        (sampled from the topology unless given), and register it."""
        address = next(self._addresses)
        if position is None:
            position = self.topology.sample_position(self._position_rng)
        node = cls(self, address, position, **kwargs)
        self._nodes[address] = node
        return node

    def node(self, address: int) -> "BaseNode":
        return self._nodes[address]

    def all_nodes(self) -> list["BaseNode"]:
        return list(self._nodes.values())

    def live_nodes(self) -> list["BaseNode"]:
        """Ground truth for *experiment* code (e.g. checking a lookup's answer).
        Overlay nodes must never call this: a real node has no global view."""
        return [n for n in self._nodes.values() if n.alive]

    def is_alive(self, address: int) -> bool:
        node = self._nodes.get(address)
        return node is not None and node.alive

    def latency(self, a: int, b: int) -> float:
        """Base one-way latency between two addresses (no jitter).
        Pastry uses this as its "proximity metric" (see Phase 3)."""
        if a == b:
            return 0.0
        return self.topology.latency(self._nodes[a].position, self._nodes[b].position)

    # --- messages ---------------------------------------------------------
    def next_message_id(self) -> int:
        return next(self._msg_ids)

    def send(self, msg: Message) -> None:
        """Put a message on the wire. It arrives later (or never, if lost)."""
        self.metrics.on_send(msg)
        if self.loss_rate > 0 and self._link_rng.random() < self.loss_rate:
            self.metrics.on_lost(msg)
            return
        delay = self.latency(msg.src, msg.dst)
        if self.jitter > 0:
            delay += self._link_rng.uniform(0.0, self.jitter)
        # Ask simpy to wake us up after `delay` ms, then hand over the message.
        # (A timeout with a callback is much cheaper than a whole process.)
        timer = self.env.timeout(delay)
        timer.callbacks.append(lambda _event, m=msg: self._arrive(m))

    def _arrive(self, msg: Message) -> None:
        node = self._nodes.get(msg.dst)
        if node is None or not node.alive:
            self.metrics.on_dropped_dead(msg)
            return
        self.metrics.on_deliver(msg)
        node.receive(msg)
