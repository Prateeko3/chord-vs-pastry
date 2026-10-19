"""BaseNode: what every simulated node can do, whatever overlay it runs.

Chord and Pastry nodes both inherit from this class. It provides:

  send(dst, kind, payload)        fire-and-forget message
  rpc(dst, kind, payload)         send a request and wait for the reply
                                  (raises RpcTimeout if none comes in time)
  receive(msg)                    called by the Network; dispatches the
                                  message to a method named handle_<kind>
  run_periodically(period, task)  repeat a task forever (e.g. Chord's
                                  stabilize) while the node is alive
  fail()                          crash: stop instantly, tell nobody

and declares the interface both overlays must implement, so experiment code
can treat them identically:

  join(bootstrap_address)   leave()   lookup(key)   routing_state_size()

HOW "WAITING" WORKS IN SIMPY (read this once, it explains every `yield`)
-----------------------------------------------------------------------
simpy runs *generator functions* as processes. When a process does
`yield some_event`, it is paused until that event happens in simulated time;
the rest of the simulation keeps running meanwhile. So

    reply = yield from self.rpc(other, "ping")

means "send a ping, pause this process until the reply arrives (or the
timeout fires), then continue with `reply` holding the answer".
`yield from` is needed because rpc() is itself a generator.

Message handlers
----------------
A handler receives the Message and either
  - returns a value immediately (plain method), or
  - is a generator (contains `yield`) when it needs to wait, e.g. to ask
    another node first. Its `return` value becomes the reply.
For an RPC request, the return value is sent back automatically.
"""

from __future__ import annotations

import inspect
from typing import TYPE_CHECKING, Any, Callable, Generator

import simpy

from sim.network import Message
from sim.rng import make_rng
from sim.topology import Position

if TYPE_CHECKING:
    from sim.metrics import Metrics
    from sim.network import Network


class RpcTimeout(Exception):
    """No reply arrived in time (the other node is dead, or a message was lost)."""


class NodeCrashed(Exception):
    """Raised inside a crashed node's own processes so they stop running."""


class UnknownMessageKind(Exception):
    """A node received a message it has no handle_<kind> method for (a bug)."""


class BaseNode:
    # Default time to wait for an RPC reply (ms). Overlays may override.
    DEFAULT_RPC_TIMEOUT = 1000.0
    # Short name used in LookupRecords ("chord", "pastry").
    OVERLAY_NAME = "base"

    def __init__(self, network: "Network", address: int, position: Position):
        self.network = network
        self.env: simpy.Environment = network.env
        self.address = address
        self.position = position
        self.alive = True
        # Each node has its own random stream (e.g. for randomised timers),
        # derived from the experiment seed and the node's address.
        self.rng = make_rng(network.seed, f"node-{address}")
        # RPCs we have sent and are still waiting on: msg_id -> simpy event.
        self._pending: dict[int, simpy.Event] = {}

    def __repr__(self) -> str:
        state = "alive" if self.alive else "dead"
        return f"<{type(self).__name__} addr={self.address} {state}>"

    @property
    def metrics(self) -> "Metrics":
        return self.network.metrics

    # --- sending -----------------------------------------------------------
    def _new_message(self, dst: int, kind: str, payload: Any, tag: str,
                     expects_reply: bool) -> Message:
        return Message(src=self.address, dst=dst, kind=kind, payload=payload,
                       msg_id=self.network.next_message_id(), sent_at=self.env.now,
                       tag=tag, expects_reply=expects_reply)

    def send(self, dst: int, kind: str, payload: Any = None, *, tag: str = "other") -> None:
        """Fire-and-forget. A crashed node cannot send anything."""
        if not self.alive:
            return
        self.network.send(self._new_message(dst, kind, payload, tag, expects_reply=False))

    def rpc(self, dst: int, kind: str, payload: Any = None, *, tag: str = "other",
            timeout: float | None = None) -> Generator[simpy.Event, Any, Any]:
        """Send a request and wait for the reply. Use as:

            value = yield from self.rpc(dst, "kind", {...})

        Raises RpcTimeout if no reply arrives within `timeout` ms, and
        NodeCrashed if *this* node crashed while it was waiting.
        """
        if not self.alive:
            raise NodeCrashed(self.address)
        msg = self._new_message(dst, kind, payload, tag, expects_reply=True)
        reply_event = self.env.event()
        self._pending[msg.msg_id] = reply_event
        self.network.send(msg)

        wait = self.DEFAULT_RPC_TIMEOUT if timeout is None else timeout
        timer = self.env.timeout(wait)
        yield reply_event | timer          # resume on whichever happens first
        self._pending.pop(msg.msg_id, None)

        if not self.alive:
            raise NodeCrashed(self.address)
        if reply_event.triggered:
            return reply_event.value
        raise RpcTimeout(f"{kind} from {self.address} to {dst} got no reply "
                         f"within {wait} ms")

    # --- receiving ---------------------------------------------------------
    def receive(self, msg: Message) -> None:
        """Called by the Network when a message arrives at this (live) node."""
        if msg.is_reply:
            event = self._pending.get(msg.reply_to)
            # If we already gave up (timed out), the late reply is ignored.
            if event is not None and not event.triggered:
                event.succeed(msg.payload)
            return

        handler = getattr(self, f"handle_{msg.kind}", None)
        if handler is None:
            raise UnknownMessageKind(f"{type(self).__name__} has no handle_{msg.kind}")
        result = handler(msg)
        if inspect.isgenerator(result):
            # The handler needs to wait for something: run it as a process,
            # and reply when it finishes.
            self.spawn(self._finish_request(msg, result))
        elif msg.expects_reply:
            self._reply(msg, result)

    def _finish_request(self, msg: Message, handler_process: Generator) -> Generator:
        value = yield from handler_process
        if msg.expects_reply:
            self._reply(msg, value)

    def _reply(self, request: Message, value: Any) -> None:
        if not self.alive:
            return
        reply = Message(src=self.address, dst=request.src, kind=f"{request.kind}.reply",
                        payload=value, msg_id=self.network.next_message_id(),
                        sent_at=self.env.now, tag=request.tag,
                        is_reply=True, reply_to=request.msg_id)
        self.network.send(reply)

    # --- background work ---------------------------------------------------
    def spawn(self, generator: Generator) -> simpy.Process:
        """Start a process for this node. If the node crashes while the
        process is paused, the process ends quietly instead of erroring."""
        def guarded():
            try:
                return (yield from generator)
            except NodeCrashed:
                return None
        return self.env.process(guarded())

    def run_periodically(self, period: float, task: Callable[[], Any], *,
                         jitter: float = 0.5) -> simpy.Process:
        """Run `task()` roughly every `period` ms until the node dies.

        Each wait is period * uniform(1 - jitter, 1 + jitter). Randomising the
        timers stops all nodes firing in lock-step, which the Chord paper also
        recommends. `task` may be a plain function or a generator function.
        """
        if not 0 <= jitter < 1:
            raise ValueError("jitter must be in [0, 1)")

        def loop():
            while self.alive:
                yield self.env.timeout(period * self.rng.uniform(1 - jitter, 1 + jitter))
                if not self.alive:
                    return
                out = task()
                if inspect.isgenerator(out):
                    yield from out
        return self.spawn(loop())

    # --- membership --------------------------------------------------------
    def fail(self) -> None:
        """Crash-stop failure: the node stops instantly and tells no one.
        Messages to it are dropped from now on; others find out by timeouts."""
        self.alive = False
        self._pending.clear()

    # --- the interface both overlays implement -----------------------------
    def join(self, bootstrap: int | None) -> Generator:
        """Join the overlay via an existing node (None = first node)."""
        raise NotImplementedError

    def leave(self) -> Generator:
        """Leave gracefully: hand over state, tell neighbours, then stop."""
        raise NotImplementedError

    def lookup(self, key: str) -> Generator:
        """Find the node responsible for `key`; record a LookupRecord."""
        raise NotImplementedError

    def routing_state_size(self) -> int:
        """Number of distinct entries in this node's routing state."""
        raise NotImplementedError
