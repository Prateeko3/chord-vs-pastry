"""Base class for all simulated nodes (Chord and Pastry both extend it).

What it gives:
  send(dst, kind, payload)   send a message, no reply expected
  rpc(dst, kind, payload)    send a request and wait for the reply
                             (raises RpcTimeout if no reply comes)
  receive(msg)               called by the network, calls handle_<kind>
  run_periodically(...)      run a task again and again (like stabilize)
  fail()                     crash the node

and the methods both overlays have to write: join, leave, lookup and
routing_state_size.

About simpy and yield: simpy runs generator functions as processes. When a
process does "yield event" it stops until that event happens in simulated
time, and the rest of the simulation keeps running. So

    reply = yield from self.rpc(other, "ping")

sends a ping, waits for the reply (or the timeout) and then continues.
We need "yield from" because rpc() is a generator itself.

A handler method gets the message and either returns the answer directly, or
(if it has a yield inside) waits for something first and then returns it.
For a request the returned value is sent back as the reply.
"""

import types

from sim.network import Message
from sim.rng import make_rng


class RpcTimeout(Exception):
    """No reply came in time (other node is dead or a message got lost)."""


class NodeCrashed(Exception):
    """Used to stop the processes of a node that has crashed."""


class UnknownMessageKind(Exception):
    """A node got a message it has no handler for (this is a bug)."""


class BaseNode:
    DEFAULT_RPC_TIMEOUT = 1000.0   # ms
    OVERLAY_NAME = "base"

    def __init__(self, network, address, position):
        self.network = network
        self.env = network.env
        self.address = address
        self.position = position
        self.alive = True
        # own random generator for this node (used for the timers)
        self.rng = make_rng(network.seed, "node-" + str(address))
        # rpc requests we are waiting for: message id -> simpy event
        self.pending = {}

    def __repr__(self):
        if self.alive:
            state = "alive"
        else:
            state = "dead"
        return "<%s addr=%d %s>" % (type(self).__name__, self.address, state)

    @property
    def metrics(self):
        return self.network.metrics

    # ----- sending -----

    def make_message(self, dst, kind, payload, tag, expects_reply):
        return Message(self.address, dst, kind, payload, self.network.new_message_id(),
                       self.env.now, tag=tag, expects_reply=expects_reply)

    def send(self, dst, kind, payload=None, tag="other"):
        """Send a message without waiting. Dead nodes can't send."""
        if not self.alive:
            return
        self.network.send(self.make_message(dst, kind, payload, tag, False))

    def rpc(self, dst, kind, payload=None, tag="other", timeout=None):
        """Send a request and wait for the reply:

            value = yield from self.rpc(dst, "kind", {...})

        Raises RpcTimeout if there is no reply in time, and NodeCrashed if this
        node died while it was waiting.
        """
        if not self.alive:
            raise NodeCrashed(self.address)
        msg = self.make_message(dst, kind, payload, tag, True)
        reply_event = self.env.event()
        self.pending[msg.msg_id] = reply_event
        self.network.send(msg)

        if timeout is None:
            timeout = self.DEFAULT_RPC_TIMEOUT
        timer = self.env.timeout(timeout)
        # wait until either the reply comes or the timer runs out
        yield self.env.any_of([reply_event, timer])
        if msg.msg_id in self.pending:
            del self.pending[msg.msg_id]

        if not self.alive:
            raise NodeCrashed(self.address)
        if reply_event.triggered:
            return reply_event.value
        raise RpcTimeout("%s from %d to %d: no reply in %s ms" % (kind, self.address, dst, timeout))

    # ----- receiving -----

    def receive(self, msg):
        """Called by the network when a message reaches this node."""
        if msg.is_reply:
            event = self.pending.get(msg.reply_to)
            # if we already timed out, the late reply is ignored
            if event is not None and not event.triggered:
                event.succeed(msg.payload)
            return

        # call the method called handle_<kind>, e.g. handle_ping
        handler = getattr(self, "handle_" + msg.kind, None)
        if handler is None:
            raise UnknownMessageKind(type(self).__name__ + " has no handle_" + msg.kind)
        result = handler(msg)
        if isinstance(result, types.GeneratorType):
            # the handler has to wait for something, so run it as a process
            # and send the reply when it is done
            self.spawn(self.finish_request(msg, result))
        elif msg.expects_reply:
            self.reply(msg, result)

    def finish_request(self, msg, handler_process):
        value = yield from handler_process
        if msg.expects_reply:
            self.reply(msg, value)

    def reply(self, request, value):
        if not self.alive:
            return
        reply = Message(self.address, request.src, request.kind + ".reply", value,
                        self.network.new_message_id(), self.env.now, tag=request.tag,
                        is_reply=True, reply_to=request.msg_id)
        self.network.send(reply)

    # ----- background processes -----

    def spawn(self, generator):
        """Start a process for this node. If the node crashes while the
        process is waiting, it just stops (no error)."""
        return self.env.process(self.run_safely(generator))

    def run_safely(self, generator):
        try:
            result = yield from generator
            return result
        except NodeCrashed:
            return None

    def run_periodically(self, period, task, jitter=0.5):
        """Call task() about every `period` ms while the node is alive.
        The wait is random between period*(1-jitter) and period*(1+jitter) so
        that nodes don't all run at the same moment. task can be a normal
        function or a generator function."""
        if jitter < 0 or jitter >= 1:
            raise ValueError("jitter must be in [0, 1)")
        return self.spawn(self.repeat(period, task, jitter))

    def repeat(self, period, task, jitter):
        while self.alive:
            yield self.env.timeout(period * self.rng.uniform(1 - jitter, 1 + jitter))
            if not self.alive:
                return
            out = task()
            if isinstance(out, types.GeneratorType):
                yield from out

    def fail(self):
        """Crash: the node stops at once and doesn't tell anyone.
        Other nodes find out only by timeouts."""
        self.alive = False
        self.pending = {}

    # ----- methods that Chord and Pastry must write -----

    def join(self, bootstrap):
        raise NotImplementedError

    def leave(self):
        raise NotImplementedError

    def lookup(self, key):
        raise NotImplementedError

    def routing_state_size(self):
        raise NotImplementedError
