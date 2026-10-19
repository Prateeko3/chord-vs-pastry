"""Simulated network that delivers messages between nodes.

There are no real sockets. Sending a message means telling simpy to give it
to the receiver after some delay:

    delay = latency between the two positions + random(0, jitter)

With probability loss_rate the message is just dropped. A message that
arrives at a dead node is also dropped, so the sender only finds out by a
timeout, like in a real network.

Things we don't model: bandwidth, queues, CPU time. Latency is the same in
both directions.
"""

from sim.metrics import Metrics
from sim.rng import make_rng
from sim.topology import UniformSquare


class Message:
    """A message between two nodes.

    kind    - what the message is, e.g. "ping" or "route". The receiver
              handles it in its method handle_<kind>.
    payload - the data (usually a dict for requests, anything for replies)
    tag     - why it was sent ("lookup", "join", "maintenance" ...)
    expects_reply / is_reply / reply_to - used for request/reply (rpc)
    """

    def __init__(self, src, dst, kind, payload, msg_id, sent_at, tag="other",
                 expects_reply=False, is_reply=False, reply_to=None):
        self.src = src
        self.dst = dst
        self.kind = kind
        self.payload = payload
        self.msg_id = msg_id
        self.sent_at = sent_at
        self.tag = tag
        self.expects_reply = expects_reply
        self.is_reply = is_reply
        self.reply_to = reply_to


class Network:
    """Holds the simpy environment and all nodes, and delivers messages."""

    def __init__(self, env, seed, topology=None, jitter=0.0, loss_rate=0.0, metrics=None):
        if jitter < 0:
            raise ValueError("jitter must be >= 0")
        if loss_rate < 0 or loss_rate >= 1:
            raise ValueError("loss_rate must be in [0, 1)")
        self.env = env
        self.seed = seed
        self.jitter = jitter
        self.loss_rate = loss_rate
        if metrics is None:
            metrics = Metrics()
        self.metrics = metrics
        if topology is None:
            topology = UniformSquare()
        # separate random generators for the layout, positions and links
        self.topology = topology.setup(make_rng(seed, "topology-layout"))
        self.position_rng = make_rng(seed, "topology-positions")
        self.link_rng = make_rng(seed, "network-links")
        self.nodes = {}            # address -> node
        self.next_address = 0      # addresses are 0, 1, 2, ...
        self.next_msg_id = 0

    def create_node(self, node_class, position=None, **kwargs):
        """Create a node, give it the next address and a position."""
        address = self.next_address
        self.next_address += 1
        if position is None:
            position = self.topology.sample_position(self.position_rng)
        node = node_class(self, address, position, **kwargs)
        self.nodes[address] = node
        return node

    def node(self, address):
        return self.nodes[address]

    def all_nodes(self):
        return list(self.nodes.values())

    def live_nodes(self):
        # Only for test/experiment code. Nodes themselves should not use this
        # because a real node can't see the whole network.
        return [n for n in self.nodes.values() if n.alive]

    def is_alive(self, address):
        node = self.nodes.get(address)
        return node is not None and node.alive

    def latency(self, a, b):
        """One-way latency between two addresses (without jitter).
        Pastry also uses this as its proximity measure."""
        if a == b:
            return 0.0
        return self.topology.latency(self.nodes[a].position, self.nodes[b].position)

    def new_message_id(self):
        msg_id = self.next_msg_id
        self.next_msg_id += 1
        return msg_id

    def send(self, msg):
        """Send a message. It arrives after the delay, or never if lost."""
        self.metrics.on_send(msg)
        if self.loss_rate > 0 and self.link_rng.random() < self.loss_rate:
            self.metrics.on_lost(msg)
            return
        delay = self.latency(msg.src, msg.dst)
        if self.jitter > 0:
            delay += self.link_rng.uniform(0.0, self.jitter)
        # simpy calls self.arrive when the timeout finishes,
        # the message is passed as the timeout's value
        timer = self.env.timeout(delay, value=msg)
        timer.callbacks.append(self.arrive)

    def arrive(self, event):
        msg = event.value
        node = self.nodes.get(msg.dst)
        if node is None or not node.alive:
            self.metrics.on_dropped_dead(msg)
            return
        self.metrics.on_deliver(msg)
        node.receive(msg)
