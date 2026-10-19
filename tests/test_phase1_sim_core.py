"""Phase 1 tests: the simulation core delivers messages at the right time,
loses/drops them when it should, RPCs and timeouts behave, randomness is
reproducible, and the metrics collector counts correctly."""

import math

import numpy as np
import pytest
import simpy

from sim.metrics import LookupRecord, Metrics, percentile, summarize
from sim.network import Network
from sim.node import BaseNode, RpcTimeout, UnknownMessageKind
from sim.rng import make_rng
from sim.topology import Clustered, Homogeneous, UniformSquare, euclidean


# --- Small node classes used only by these tests ---------------------------

class EchoNode(BaseNode):
    """Remembers when each one-way message arrived; answers pings."""

    def __init__(self, network, address, position):
        super().__init__(network, address, position)
        self.arrivals = []   # (time, payload)

    def handle_note(self, msg):               # one-way message
        self.arrivals.append((self.env.now, msg.payload))

    def handle_ping(self, msg):               # RPC: plain method -> instant reply
        return ("pong", self.address)

    def handle_slow_ping(self, msg):          # RPC: waits before replying
        yield self.env.timeout(msg.payload["delay"])
        return "slow-pong"

    def handle_relay(self, msg):              # RPC that makes a nested RPC
        answer = yield from self.rpc(msg.payload["to"], "ping")
        return ("relayed", answer)


def make_net(seed=1, **kw):
    env = simpy.Environment()
    return env, Network(env, seed=seed, **kw)


def run_process(env, gen):
    """Run one generator as a process to completion and return its value."""
    proc = env.process(gen)
    env.run(until=proc)
    return proc.value


# --- Message delivery timing ----------------------------------------------

def test_one_way_message_arrives_after_euclidean_distance():
    env, net = make_net()
    a = net.create_node(EchoNode, position=(0, 0))
    b = net.create_node(EchoNode, position=(3, 4))     # distance 5
    a.send(b.address, "note", "hello")
    env.run()
    assert b.arrivals == [(5.0, "hello")]


def test_rpc_round_trip_is_twice_the_distance():
    env, net = make_net()
    a = net.create_node(EchoNode, position=(0, 0))
    b = net.create_node(EchoNode, position=(6, 8))     # distance 10

    def caller():
        reply = yield from a.rpc(b.address, "ping")
        return reply, env.now

    reply, t = run_process(env, caller())
    assert reply == ("pong", b.address)
    assert t == 20.0


def test_nested_rpc_timing_adds_up():
    # A asks B to ping C. Time = A->B + B->C + C->B + B->A = 5 + 6 + 6 + 5.
    env, net = make_net()
    a = net.create_node(EchoNode, position=(0, 0))
    b = net.create_node(EchoNode, position=(3, 4))
    c = net.create_node(EchoNode, position=(3, 10))

    def caller():
        reply = yield from a.rpc(b.address, "relay", {"to": c.address})
        return reply, env.now

    reply, t = run_process(env, caller())
    assert reply == ("relayed", ("pong", c.address))
    assert t == 22.0


def test_generator_handler_delay_is_included():
    env, net = make_net()
    a = net.create_node(EchoNode, position=(0, 0))
    b = net.create_node(EchoNode, position=(0, 10))

    def caller():
        reply = yield from a.rpc(b.address, "slow_ping", {"delay": 7})
        return reply, env.now

    assert run_process(env, caller()) == ("slow-pong", 27.0)


def test_message_to_self_has_zero_delay():
    env, net = make_net()
    a = net.create_node(EchoNode, position=(50, 50))
    a.send(a.address, "note", 1)
    env.run()
    assert a.arrivals == [(0.0, 1)]


def test_messages_in_flight_are_delivered_in_time_order():
    env, net = make_net()
    hub = net.create_node(EchoNode, position=(0, 0))
    far = net.create_node(EchoNode, position=(0, 30))
    near = net.create_node(EchoNode, position=(0, 10))
    far.send(hub.address, "note", "far")    # sent first, arrives at 30
    near.send(hub.address, "note", "near")  # sent second, arrives at 10
    env.run()
    assert hub.arrivals == [(10.0, "near"), (30.0, "far")]


# --- Jitter and loss -------------------------------------------------------

def test_jitter_adds_between_zero_and_jitter_ms():
    env, net = make_net(jitter=4.0)
    a = net.create_node(EchoNode, position=(0, 0))
    b = net.create_node(EchoNode, position=(0, 10))
    for i in range(200):
        a.send(b.address, "note", i)
    env.run()
    delays = [t for t, _ in b.arrivals]
    assert len(delays) == 200
    assert all(10.0 <= d <= 14.0 for d in delays)
    assert len(set(delays)) > 150            # really random, not constant
    assert 11.5 < sum(delays) / 200 < 12.5   # mean of U(0,4) is 2


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
    # Expected 70% delivered; std of the fraction is ~0.0065, allow ~4.5 std.
    assert abs(delivered / n - 0.7) < 0.03


@pytest.mark.parametrize("bad", [{"loss_rate": 1.0}, {"loss_rate": -0.1}, {"jitter": -1}])
def test_network_rejects_bad_parameters(bad):
    with pytest.raises(ValueError):
        make_net(**bad)


# --- Failures and timeouts -------------------------------------------------

def test_message_to_crashed_node_is_dropped():
    env, net = make_net()
    a = net.create_node(EchoNode, position=(0, 0))
    b = net.create_node(EchoNode, position=(0, 10))
    b.fail()
    a.send(b.address, "note", "anyone?")
    env.run()
    assert b.arrivals == []
    assert net.metrics.dropped_dead == 1


def test_node_crashing_while_message_in_flight_drops_it():
    env, net = make_net()
    a = net.create_node(EchoNode, position=(0, 0))
    b = net.create_node(EchoNode, position=(0, 10))
    a.send(b.address, "note", "x")           # would arrive at t=10

    def crash_b_at_5():
        yield env.timeout(5)
        b.fail()

    env.process(crash_b_at_5())
    env.run()
    assert b.arrivals == []


def test_rpc_to_dead_node_times_out_exactly_at_timeout():
    env, net = make_net()
    a = net.create_node(EchoNode, position=(0, 0))
    b = net.create_node(EchoNode, position=(0, 10))
    b.fail()

    def caller():
        try:
            yield from a.rpc(b.address, "ping", timeout=100)
        except RpcTimeout:
            return "timeout", env.now
        return "reply", env.now

    assert run_process(env, caller()) == ("timeout", 100.0)


def test_late_reply_after_timeout_is_ignored():
    env, net = make_net()
    a = net.create_node(EchoNode, position=(0, 0))
    b = net.create_node(EchoNode, position=(0, 10))

    def caller():
        try:
            yield from a.rpc(b.address, "slow_ping", {"delay": 50}, timeout=30)
        except RpcTimeout:
            return "timeout"

    assert run_process(env, caller()) == "timeout"
    env.run()   # let the late reply (arriving at t=70) be delivered: no crash
    assert net.metrics.sent_by_kind["slow_ping.reply"] == 1


def test_crashed_node_cannot_send_and_its_rpc_process_stops_quietly():
    env, net = make_net()
    a = net.create_node(EchoNode, position=(0, 0))
    b = net.create_node(EchoNode, position=(0, 10))
    finished = []

    def caller():
        yield from a.rpc(b.address, "slow_ping", {"delay": 50}, timeout=200)
        finished.append(True)    # must never run: A crashes while waiting

    a.spawn(caller())

    def crash_a():
        yield env.timeout(5)
        a.fail()
        a.send(b.address, "note", "ghost")   # a dead node cannot send

    env.process(crash_a())
    env.run()
    assert finished == []
    assert b.arrivals == []
    assert net.metrics.dropped_dead == 1     # B's reply reached dead A


def test_unknown_message_kind_is_a_loud_error():
    env, net = make_net()
    a = net.create_node(EchoNode, position=(0, 0))
    b = net.create_node(EchoNode, position=(0, 1))
    a.send(b.address, "no_such_kind")
    with pytest.raises(UnknownMessageKind):
        env.run()


# --- Periodic tasks --------------------------------------------------------

def test_run_periodically_repeats_until_node_dies():
    env, net = make_net()
    a = net.create_node(EchoNode, position=(0, 0))
    ticks = []
    a.run_periodically(10, lambda: ticks.append(env.now), jitter=0)
    env.run(until=55)
    assert ticks == [10, 20, 30, 40, 50]
    a.fail()
    env.run(until=200)
    assert len(ticks) == 5


def test_run_periodically_with_jitter_stays_in_range():
    env, net = make_net()
    a = net.create_node(EchoNode, position=(0, 0))
    ticks = []
    a.run_periodically(10, lambda: ticks.append(env.now), jitter=0.5)
    env.run(until=1000)
    gaps = np.diff([0.0] + ticks)
    assert all(5.0 <= g <= 15.0 for g in gaps)
    assert 80 < len(ticks) < 120             # about 1000 / 10


# --- Base interface ---------------------------------------------------------

@pytest.mark.parametrize("call", [
    lambda n: n.join(None), lambda n: n.leave(),
    lambda n: n.lookup("k"), lambda n: n.routing_state_size(),
])
def test_base_interface_must_be_overridden(call):
    env, net = make_net()
    n = net.create_node(BaseNode)
    with pytest.raises(NotImplementedError):
        call(n)


# --- Topologies and reproducibility ----------------------------------------

def test_uniform_positions_stay_in_square():
    env, net = make_net(topology=UniformSquare(side=50))
    for _ in range(500):
        x, y = net.create_node(EchoNode).position
        assert 0 <= x <= 50 and 0 <= y <= 50


def test_clustered_nodes_sit_near_a_centre():
    env, net = make_net(topology=Clustered(n_clusters=4, side=100, spread=2))
    centers = net.topology.centers
    assert len(centers) == 4
    for _ in range(400):
        p = net.create_node(EchoNode).position
        assert min(euclidean(p, c) for c in centers) < 5 * 2   # within 5 sigma


def test_clustered_topology_must_be_bound():
    with pytest.raises(RuntimeError):
        Clustered().sample_position(make_rng(1, "x"))


def test_homogeneous_latency_is_constant():
    env, net = make_net(topology=Homogeneous(latency_ms=42))
    nodes = [net.create_node(EchoNode) for _ in range(20)]
    for a in nodes:
        for b in nodes:
            assert net.latency(a.address, b.address) == (0.0 if a is b else 42)


def test_same_seed_gives_identical_simulation():
    def run(seed):
        env, net = make_net(seed=seed, topology=Clustered(), jitter=5, loss_rate=0.1)
        nodes = [net.create_node(EchoNode) for _ in range(30)]
        for i, a in enumerate(nodes):
            a.send(nodes[(i * 7) % 30].address, "note", i)
        env.run()
        return ([n.position for n in nodes], [n.arrivals for n in nodes],
                net.metrics.lost)

    assert run(42) == run(42)
    assert run(42) != run(43)


def test_cluster_centres_depend_on_seed_but_spec_is_reusable():
    spec = Clustered(n_clusters=3)
    c1 = make_net(seed=1, topology=spec)[1].topology.centers
    c1_again = make_net(seed=1, topology=spec)[1].topology.centers
    c2 = make_net(seed=2, topology=spec)[1].topology.centers
    assert c1 == c1_again and c1 != c2
    assert spec.centers is None   # the shared spec object was not modified


# --- Metrics -----------------------------------------------------------------

def test_metrics_count_messages_by_kind_and_tag():
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
    assert m.sent_by_tag == {"lookup": 2, "maintenance": 1}   # reply inherits tag
    assert m.total_sent == 3 and m.delivered == 3


@pytest.mark.parametrize("q", [0, 5, 50, 90, 95, 99, 100])
def test_percentile_matches_numpy(q):
    xs = [float(v) for v in make_rng(7, "pct").choices(range(1000), k=137)]
    assert percentile(xs, q) == pytest.approx(np.percentile(xs, q))


def test_summarize_handles_empty_and_simple_input():
    assert summarize([])["count"] == 0
    assert math.isnan(summarize([])["mean"])
    s = summarize([1, 2, 3, 4])
    assert (s["count"], s["mean"], s["min"], s["max"], s["p50"]) == (4, 2.5, 1, 4, 2.5)
    assert s["std"] == pytest.approx(np.std([1, 2, 3, 4]))


def test_lookup_summary_uses_only_successful_lookups_for_hops_and_latency():
    m = Metrics()
    m.record_lookup(LookupRecord("chord", 0, "a", 0.0, 10.0, 2, 5, True, True))
    m.record_lookup(LookupRecord("chord", 0, "b", 0.0, 30.0, 4, 6, True, False))
    m.record_lookup(LookupRecord("chord", 0, "c", 0.0, 999.0, 9, None, False))
    s = m.lookup_summary()
    assert s["lookups"] == 3
    assert s["success_rate"] == pytest.approx(2 / 3)
    assert s["correct_rate"] == 0.5
    assert s["hops_mean"] == 3 and s["latency_mean"] == 20.0


def test_metrics_reset_clears_everything():
    m = Metrics()
    m.record_lookup(LookupRecord("x", 0, "k", 0, 1, 1, 1, True))
    m.record_distribution("keys_per_node", [1, 2])
    m.lost = 3
    m.reset()
    assert m.lookups == [] and m.distributions == {} and m.lost == 0
