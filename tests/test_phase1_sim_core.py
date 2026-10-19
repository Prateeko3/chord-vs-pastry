"""Phase 1 tests for the simulator: message delays, loss, rpc and timeouts,
crashes, periodic tasks, topologies, seeds and the metrics collector."""

import math

import numpy as np
import pytest
import simpy

from sim.metrics import LookupRecord, Metrics, percentile, summarize
from sim.network import Network
from sim.node import BaseNode, RpcTimeout, UnknownMessageKind
from sim.rng import make_rng
from sim.topology import Clustered, Homogeneous, UniformSquare, euclidean


class EchoNode(BaseNode):
    """Small node class only for these tests."""

    def __init__(self, network, address, position):
        BaseNode.__init__(self, network, address, position)
        self.arrivals = []   # (time, payload) of every "note" message

    def handle_note(self, msg):
        self.arrivals.append((self.env.now, msg.payload))

    def handle_ping(self, msg):
        return ("pong", self.address)

    def handle_slow_ping(self, msg):
        yield self.env.timeout(msg.payload["delay"])
        return "slow-pong"

    def handle_relay(self, msg):
        # asks another node and returns its answer
        answer = yield from self.rpc(msg.payload["to"], "ping")
        return ("relayed", answer)


def make_net(seed=1, **kwargs):
    env = simpy.Environment()
    return env, Network(env, seed=seed, **kwargs)


def run_process(env, gen):
    """Run one process until it ends and return its value."""
    proc = env.process(gen)
    env.run(until=proc)
    return proc.value


# ----- timing -----

def test_message_arrives_after_the_distance():
    env, net = make_net()
    a = net.create_node(EchoNode, position=(0, 0))
    b = net.create_node(EchoNode, position=(3, 4))     # distance 5
    a.send(b.address, "note", "hello")
    env.run()
    assert b.arrivals == [(5.0, "hello")]


def ping_and_time(env, a, b, kind="ping", payload=None):
    reply = yield from a.rpc(b.address, kind, payload)
    return reply, env.now


def test_rpc_takes_twice_the_distance():
    env, net = make_net()
    a = net.create_node(EchoNode, position=(0, 0))
    b = net.create_node(EchoNode, position=(6, 8))     # distance 10
    reply, t = run_process(env, ping_and_time(env, a, b))
    assert reply == ("pong", b.address)
    assert t == 20.0


def test_nested_rpc_time_adds_up():
    # a asks b to ping c: 5 + 6 + 6 + 5 = 22
    env, net = make_net()
    a = net.create_node(EchoNode, position=(0, 0))
    b = net.create_node(EchoNode, position=(3, 4))
    c = net.create_node(EchoNode, position=(3, 10))
    reply, t = run_process(env, ping_and_time(env, a, b, "relay", {"to": c.address}))
    assert reply == ("relayed", ("pong", c.address))
    assert t == 22.0


def test_handler_waiting_time_is_counted():
    env, net = make_net()
    a = net.create_node(EchoNode, position=(0, 0))
    b = net.create_node(EchoNode, position=(0, 10))
    assert run_process(env, ping_and_time(env, a, b, "slow_ping", {"delay": 7})) == ("slow-pong", 27.0)


def test_message_to_self_has_no_delay():
    env, net = make_net()
    a = net.create_node(EchoNode, position=(50, 50))
    a.send(a.address, "note", 1)
    env.run()
    assert a.arrivals == [(0.0, 1)]


def test_messages_arrive_in_time_order():
    env, net = make_net()
    hub = net.create_node(EchoNode, position=(0, 0))
    far = net.create_node(EchoNode, position=(0, 30))
    near = net.create_node(EchoNode, position=(0, 10))
    far.send(hub.address, "note", "far")    # sent first but arrives at 30
    near.send(hub.address, "note", "near")  # arrives at 10
    env.run()
    assert hub.arrivals == [(10.0, "near"), (30.0, "far")]


# ----- jitter and loss -----

def test_jitter_is_between_0_and_jitter():
    env, net = make_net(jitter=4.0)
    a = net.create_node(EchoNode, position=(0, 0))
    b = net.create_node(EchoNode, position=(0, 10))
    for i in range(200):
        a.send(b.address, "note", i)
    env.run()
    delays = [t for t, payload in b.arrivals]
    assert len(delays) == 200
    for d in delays:
        assert 10.0 <= d <= 14.0
    assert len(set(delays)) > 150            # really random
    assert 11.5 < sum(delays) / 200 < 12.5   # average jitter should be 2


def test_loss_rate_drops_about_that_fraction():
    env, net = make_net(loss_rate=0.3)
    a = net.create_node(EchoNode, position=(0, 0))
    b = net.create_node(EchoNode, position=(1, 0))
    n = 5000
    for i in range(n):
        a.send(b.address, "note", i)
    env.run()
    delivered = len(b.arrivals)
    assert net.metrics.lost + delivered == n
    # 70% should arrive, std of this is about 0.0065 so 0.03 is a safe margin
    assert abs(delivered / n - 0.7) < 0.03


@pytest.mark.parametrize("bad", [{"loss_rate": 1.0}, {"loss_rate": -0.1}, {"jitter": -1}])
def test_bad_network_parameters(bad):
    with pytest.raises(ValueError):
        make_net(**bad)


# ----- failures and timeouts -----

def test_message_to_dead_node_is_dropped():
    env, net = make_net()
    a = net.create_node(EchoNode, position=(0, 0))
    b = net.create_node(EchoNode, position=(0, 10))
    b.fail()
    a.send(b.address, "note", "anyone?")
    env.run()
    assert b.arrivals == []
    assert net.metrics.dropped_dead == 1


def crash_later(env, node, when):
    yield env.timeout(when)
    node.fail()


def test_node_dying_while_message_on_the_way():
    env, net = make_net()
    a = net.create_node(EchoNode, position=(0, 0))
    b = net.create_node(EchoNode, position=(0, 10))
    a.send(b.address, "note", "x")           # would arrive at t = 10
    env.process(crash_later(env, b, 5))
    env.run()
    assert b.arrivals == []


def ping_with_timeout(env, a, b, kind, payload, timeout):
    try:
        yield from a.rpc(b.address, kind, payload, timeout=timeout)
    except RpcTimeout:
        return "timeout", env.now
    return "reply", env.now


def test_rpc_to_dead_node_times_out():
    env, net = make_net()
    a = net.create_node(EchoNode, position=(0, 0))
    b = net.create_node(EchoNode, position=(0, 10))
    b.fail()
    assert run_process(env, ping_with_timeout(env, a, b, "ping", None, 100)) == ("timeout", 100.0)


def test_late_reply_is_ignored():
    env, net = make_net()
    a = net.create_node(EchoNode, position=(0, 0))
    b = net.create_node(EchoNode, position=(0, 10))
    result = run_process(env, ping_with_timeout(env, a, b, "slow_ping", {"delay": 50}, 30))
    assert result[0] == "timeout"
    env.run()   # the reply comes at t = 70, nothing should break
    assert net.metrics.sent_by_kind["slow_ping.reply"] == 1


def test_crashed_node_stops_and_cannot_send():
    env, net = make_net()
    a = net.create_node(EchoNode, position=(0, 0))
    b = net.create_node(EchoNode, position=(0, 10))
    finished = []

    def caller():
        yield from a.rpc(b.address, "slow_ping", {"delay": 50}, timeout=200)
        finished.append(True)   # should never happen, a crashes first

    def crash_a():
        yield env.timeout(5)
        a.fail()
        a.send(b.address, "note", "ghost")

    a.spawn(caller())
    env.process(crash_a())
    env.run()
    assert finished == []
    assert b.arrivals == []
    assert net.metrics.dropped_dead == 1     # b's reply reached dead a


def test_unknown_message_kind_raises_error():
    env, net = make_net()
    a = net.create_node(EchoNode, position=(0, 0))
    b = net.create_node(EchoNode, position=(0, 1))
    a.send(b.address, "no_such_kind")
    with pytest.raises(UnknownMessageKind):
        env.run()


# ----- periodic tasks -----

def test_run_periodically_stops_when_node_dies():
    env, net = make_net()
    a = net.create_node(EchoNode, position=(0, 0))
    ticks = []
    a.run_periodically(10, lambda: ticks.append(env.now), jitter=0)
    env.run(until=55)
    assert ticks == [10, 20, 30, 40, 50]
    a.fail()
    env.run(until=200)
    assert len(ticks) == 5


def test_run_periodically_with_jitter():
    env, net = make_net()
    a = net.create_node(EchoNode, position=(0, 0))
    ticks = []
    a.run_periodically(10, lambda: ticks.append(env.now), jitter=0.5)
    env.run(until=1000)
    gaps = np.diff([0.0] + ticks)
    for g in gaps:
        assert 5.0 <= g <= 15.0
    assert 80 < len(ticks) < 120


# ----- base class -----

@pytest.mark.parametrize("method", ["join", "leave", "lookup", "routing_state_size"])
def test_base_methods_must_be_written_by_subclass(method):
    env, net = make_net()
    n = net.create_node(BaseNode)
    with pytest.raises(NotImplementedError):
        if method == "join":
            n.join(None)
        elif method == "leave":
            n.leave()
        elif method == "lookup":
            n.lookup("k")
        else:
            n.routing_state_size()


# ----- topologies and seeds -----

def test_uniform_positions_inside_square():
    env, net = make_net(topology=UniformSquare(side=50))
    for i in range(500):
        x, y = net.create_node(EchoNode).position
        assert 0 <= x <= 50 and 0 <= y <= 50


def test_clustered_nodes_are_near_a_centre():
    env, net = make_net(topology=Clustered(n_clusters=4, side=100, spread=2))
    centers = net.topology.centers
    assert len(centers) == 4
    for i in range(400):
        p = net.create_node(EchoNode).position
        closest = min(euclidean(p, c) for c in centers)
        assert closest < 5 * 2     # within 5 standard deviations


def test_clustered_needs_setup_first():
    with pytest.raises(RuntimeError):
        Clustered().sample_position(make_rng(1, "x"))


def test_homogeneous_latency_is_constant():
    env, net = make_net(topology=Homogeneous(latency_ms=42))
    nodes = [net.create_node(EchoNode) for i in range(20)]
    for a in nodes:
        for b in nodes:
            if a is b:
                assert net.latency(a.address, b.address) == 0.0
            else:
                assert net.latency(a.address, b.address) == 42


def run_small_sim(seed):
    env, net = make_net(seed=seed, topology=Clustered(), jitter=5, loss_rate=0.1)
    nodes = [net.create_node(EchoNode) for i in range(30)]
    for i in range(30):
        nodes[i].send(nodes[(i * 7) % 30].address, "note", i)
    env.run()
    return [n.position for n in nodes], [n.arrivals for n in nodes], net.metrics.lost


def test_same_seed_gives_same_simulation():
    assert run_small_sim(42) == run_small_sim(42)
    assert run_small_sim(42) != run_small_sim(43)


def test_cluster_centres_depend_on_seed():
    spec = Clustered(n_clusters=3)
    c1 = make_net(seed=1, topology=spec)[1].topology.centers
    c1_again = make_net(seed=1, topology=spec)[1].topology.centers
    c2 = make_net(seed=2, topology=spec)[1].topology.centers
    assert c1 == c1_again and c1 != c2
    assert spec.centers is None   # the original object was not changed


# ----- metrics -----

def test_metrics_count_by_kind_and_tag():
    env, net = make_net()
    a = net.create_node(EchoNode, position=(0, 0))
    b = net.create_node(EchoNode, position=(0, 1))

    def caller():
        yield from a.rpc(b.address, "ping", tag="lookup")
        a.send(b.address, "note", tag="maintenance")

    run_process(env, caller())
    env.run()
    m = net.metrics
    assert m.sent_by_kind == {"ping": 1, "ping.reply": 1, "note": 1}
    assert m.sent_by_tag == {"lookup": 2, "maintenance": 1}   # reply has the same tag
    assert m.total_sent() == 3 and m.delivered == 3


@pytest.mark.parametrize("q", [0, 5, 50, 90, 95, 99, 100])
def test_percentile_same_as_numpy(q):
    xs = [float(v) for v in make_rng(7, "pct").choices(range(1000), k=137)]
    assert percentile(xs, q) == pytest.approx(np.percentile(xs, q))


def test_summarize():
    assert summarize([])["count"] == 0
    assert math.isnan(summarize([])["mean"])
    s = summarize([1, 2, 3, 4])
    assert s["count"] == 4 and s["mean"] == 2.5
    assert s["min"] == 1 and s["max"] == 4 and s["p50"] == 2.5
    assert s["std"] == pytest.approx(np.std([1, 2, 3, 4]))


def test_lookup_summary_only_uses_successful_lookups():
    m = Metrics()
    m.record_lookup(LookupRecord("chord", 0, "a", 0.0, 10.0, 2, 5, True, True))
    m.record_lookup(LookupRecord("chord", 0, "b", 0.0, 30.0, 4, 6, True, False))
    m.record_lookup(LookupRecord("chord", 0, "c", 0.0, 999.0, 9, None, False))
    s = m.lookup_summary()
    assert s["lookups"] == 3
    assert s["success_rate"] == pytest.approx(2 / 3)
    assert s["correct_rate"] == 0.5
    assert s["hops_mean"] == 3 and s["latency_mean"] == 20.0


def test_metrics_reset():
    m = Metrics()
    m.record_lookup(LookupRecord("x", 0, "k", 0, 1, 1, 1, True))
    m.record_distribution("keys_per_node", [1, 2])
    m.lost = 3
    m.reset()
    assert m.lookups == [] and m.distributions == {} and m.lost == 0
