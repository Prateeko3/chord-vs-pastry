"""Phase 2 tests: Chord.

Static rings (state built perfectly by chord.ring.build_static) test the
lookup algorithm itself: correctness, hop counts, latency accounting.
Dynamic tests start nodes from nothing and check that join + stabilize +
fix_fingers build the right ring, and that leaves and crashes are repaired.
"""

import math
import statistics

import pytest
import simpy

from chord.node import ChordNode
from chord.ring import Oracle, build_static, check_ring, create_nodes, finger_accuracy
from sim.network import Network
from sim.rng import make_rng


# --- helpers ------------------------------------------------------------------

def static_ring(n, seed=1, **node_kw):
    env = simpy.Environment()
    net = Network(env, seed=seed)
    nodes = create_nodes(net, n, **node_kw)
    build_static(nodes)
    return env, net, nodes


def run_lookups(env, nodes, count, seed=0):
    """Issue `count` lookups for random keys from random live origins."""
    rng = make_rng(seed, "test-lookups")
    live = [n for n in nodes if n.alive]
    records = []

    def one(origin, key):
        rec = yield from origin.lookup(key)
        records.append(rec)

    for _ in range(count):
        env.process(one(rng.choice(live), f"key-{rng.getrandbits(64):x}"))
    env.run()
    return records


def grow_ring(n, seed=1, gap=200.0, **node_kw):
    """Start one node, then join the rest one after another through it,
    with maintenance running everywhere. Returns when every join is done."""
    env = simpy.Environment()
    net = Network(env, seed=seed)
    nodes = create_nodes(net, n, **node_kw)

    def grow():
        yield from nodes[0].join(None)
        nodes[0].start_maintenance()
        for node in nodes[1:]:
            ok = yield from node.join(nodes[0].address)
            assert ok
            node.start_maintenance()
            yield env.timeout(gap)

    env.run(until=env.process(grow()))
    return env, net, nodes


# --- static ring: lookup correctness and cost ---------------------------------

@pytest.mark.parametrize("n", [10, 100, 1000])
def test_static_lookups_always_find_the_responsible_node(n):
    env, net, nodes = static_ring(n)
    oracle = Oracle(nodes)
    records = run_lookups(env, nodes, 1000)
    assert all(r.success for r in records)
    assert all(oracle.check(r) for r in records)


@pytest.mark.parametrize("n", [10, 100, 1000])
def test_average_hops_is_about_half_log2_n(n):
    env, net, nodes = static_ring(n)
    records = run_lookups(env, nodes, 2000)
    mean_hops = statistics.fmean(r.hops for r in records)
    # Hops are counted all the way to the owner. Measured: 1/2*log2(N) + ~0.3
    # (see the learning guide, Phase 2 section 3.3).
    assert abs(mean_hops - 0.5 * math.log2(n)) < 0.75, mean_hops


def test_hops_grow_by_half_a_hop_per_doubling_of_n():
    # The O(log N) claim, measured directly: 256 -> 2048 nodes is 3 doublings.
    means = {}
    for n in (256, 2048):
        env, net, nodes = static_ring(n, seed=5)
        means[n] = statistics.fmean(r.hops for r in run_lookups(env, nodes, 3000))
    slope = (means[2048] - means[256]) / 3
    assert 0.4 < slope < 0.6, means


def test_every_identifier_on_a_small_ring_resolves_correctly():
    # m = 8 -> only 256 IDs, so we can try every single one, including the
    # wrap-around point between 255 and 0.
    env, net, nodes = static_ring(40, m=8)
    oracle = Oracle(nodes)
    results = {}

    def ask(origin, ident):
        res = yield from origin._find_successor(ident, tag="lookup")
        results[ident] = res["owner"].addr

    for ident in range(256):
        env.process(ask(nodes[ident % len(nodes)], ident))
    env.run()
    for ident in range(256):
        assert results[ident] == oracle.successor(ident).address, ident


def test_lookup_latency_equals_sum_of_link_latencies_on_the_path():
    # Recursive routing: origin -> ... -> owner, then owner -> origin directly.
    env, net, nodes = static_ring(200)
    for rec in run_lookups(env, nodes, 300):
        path = rec.path
        forward = sum(net.latency(a, b) for a, b in zip(path, path[1:]))
        back = net.latency(path[-1], rec.origin)
        assert len(path) == rec.hops + 1 and path[-1] == rec.owner
        assert rec.latency == pytest.approx(forward + back)


def test_routing_state_is_logarithmic():
    env, net, nodes = static_ring(1000)
    sizes = [n.routing_state_size() for n in nodes]
    # About log2(N) distinct fingers, plus the successor list (r = 8,
    # overlapping the smallest fingers), plus the predecessor.
    assert statistics.fmean(sizes) < math.log2(1000) + 8 + 1
    assert max(sizes) <= 32 + 8 + 1   # can never exceed m + r + 1


def test_static_builder_and_oracle_agree():
    env, net, nodes = static_ring(300)
    assert check_ring(nodes) == []
    assert finger_accuracy(nodes) == 1.0


def test_single_node_owns_everything_with_zero_hops():
    env = simpy.Environment()
    net = Network(env, seed=1)
    (node,) = create_nodes(net, 1)
    env.run(until=env.process(node.join(None)))
    records = run_lookups(env, [node], 20)
    assert all(r.success and r.owner == node.address and r.hops == 0 for r in records)


def test_ids_are_unique_even_when_hashes_collide():
    env = simpy.Environment()
    net = Network(env, seed=1)
    nodes = create_nodes(net, 12, m=4)      # 12 nodes on a 16-position ring
    assert len({n.id for n in nodes}) == 12


def test_same_seed_same_lookups():
    def hops(seed):
        env, net, nodes = static_ring(300, seed=seed)
        return [(r.hops, r.owner, r.latency) for r in run_lookups(env, nodes, 200)]
    assert hops(9) == hops(9)
    assert hops(9) != hops(10)


# --- dynamic membership: join / stabilize -------------------------------------

def test_two_nodes_form_a_ring():
    env, net, nodes = grow_ring(2)
    env.run(until=env.now + 5000)
    a, b = nodes
    assert a.successor == b.ref and b.successor == a.ref
    assert a.predecessor == b.ref and b.predecessor == a.ref


def test_sequential_joins_converge_to_a_correct_ring():
    env, net, nodes = grow_ring(60)
    env.run(until=env.now + 60_000)          # let stabilize / fix_fingers run
    assert check_ring(nodes) == []
    assert finger_accuracy(nodes) == 1.0
    oracle = Oracle(nodes)
    assert all(oracle.check(r) for r in run_lookups_bounded(env, nodes, 300))


def test_concurrent_joins_converge():
    # 30 nodes all join through the same bootstrap at the same moment: the
    # case stabilization is designed for (Chord ToN, Theorem 4).
    env = simpy.Environment()
    net = Network(env, seed=4)
    nodes = create_nodes(net, 31)
    env.run(until=env.process(nodes[0].join(None)))
    nodes[0].start_maintenance()
    for node in nodes[1:]:
        env.process(node.join(nodes[0].address))
        node.start_maintenance()
    env.run(until=env.now + 120_000)
    assert check_ring(nodes) == []
    assert finger_accuracy(nodes) == 1.0


def test_join_message_cost_is_polylogarithmic():
    # Join 10 extra nodes (one at a time) into a stable 1000-node ring and
    # count messages tagged "join". Expected: ~log2(N) finger lookups, each
    # ~1/2*log2(N) hops x 2 messages (route + ack) + 1 answer, so about
    # log2(N)^2 + 2*log2(N) ~ 120. The paper's bound is O(log^2 N).
    env, net, nodes = static_ring(1000)
    costs = []
    for newcomer in create_nodes(net, 10):
        before = net.metrics.sent_by_tag["join"]
        assert env.run(until=env.process(newcomer.join(nodes[0].address)))
        costs.append(net.metrics.sent_by_tag["join"] - before)
    log_n = math.log2(1000)
    assert log_n < statistics.fmean(costs) < 2 * log_n ** 2, costs


def test_notify_only_accepts_a_closer_predecessor():
    # Paper: notify(n') sets predecessor = n' only if n' is in (predecessor, n).
    # A stale notify from a node further back must NOT replace a good pointer.
    # (Convergence tests can't catch this bug: the ring heals eventually.)
    from chord.node import NodeRef
    env, net, nodes = static_ring(30)
    ring = Oracle(nodes).ring
    target, good_pred, farther = ring[12], ring[11], ring[10]
    farther.send(target.address, "notify", farther.ref)
    env.run()
    assert target.predecessor == good_pred.ref
    # A (new) node between the current predecessor and the target is accepted.
    closer = NodeRef((good_pred.id + target.id) // 2, 999)
    assert good_pred.id < closer.id < target.id
    good_pred.send(target.address, "notify", closer)
    env.run()
    assert target.predecessor == closer


def test_stabilize_adopts_a_node_that_joined_in_between():
    env, net, nodes = static_ring(30)
    ring = Oracle(nodes).ring
    a, c = ring[5], ring[6]
    b = create_nodes(net, 1)[0]                       # a real, live node...
    b.set_id((a.id + c.id) // 2)                      # ...placed between a and c
    assert a.id < b.id < c.id
    c.predecessor = b.ref                             # as if b had notified c
    env.run(until=env.process(a.stabilize()))
    assert a.successor == b.ref


# --- leaving and crashing -----------------------------------------------------

def test_graceful_leave_repairs_neighbours_without_stabilization():
    env, net, nodes = static_ring(50)        # no maintenance running at all
    oracle = Oracle(nodes)
    leaver = oracle.ring[10]
    pred, succ = oracle.ring[9], oracle.ring[11]
    env.run(until=env.process(leaver.leave()))
    env.run(until=env.now + 500)             # let the two notices arrive
    assert pred.successor == succ.ref
    assert succ.predecessor == pred.ref
    assert pred.successors[:3] == [oracle.ring[11].ref, oracle.ring[12].ref, oracle.ring[13].ref]
    oracle_after = Oracle(nodes)
    assert all(oracle_after.check(r) for r in run_lookups(env, nodes, 300))


def test_lookups_stay_correct_right_after_crashes_without_repair():
    # 20% of nodes crash; no stabilization runs. Lookups must route around
    # the dead nodes using timeouts and the successor list.
    env, net, nodes = static_ring(200)
    for node in make_rng(1, "crash").sample(nodes, 40):
        node.fail()
    oracle = Oracle(nodes)
    records = run_lookups(env, nodes, 500)
    assert all(r.success for r in records)
    assert all(oracle.check(r) for r in records)


def test_ring_repairs_itself_after_mass_crash():
    env, net, nodes = static_ring(100)
    for node in nodes:
        node.start_maintenance()
    env.run(until=5000)
    for node in make_rng(2, "crash").sample(nodes, 25):
        node.fail()
    env.run(until=env.now + 60_000)
    assert check_ring(nodes) == []
    assert finger_accuracy(nodes) == 1.0


def test_crashed_node_sends_nothing_and_is_forgotten():
    env, net, nodes = static_ring(20)
    for node in nodes:
        node.start_maintenance()
    senders = []                              # (time, src) of every message
    real_send = net.send
    net.send = lambda msg: (senders.append((env.now, msg.src)), real_send(msg))
    env.run(until=3000)
    victim = nodes[3]
    victim.fail()
    crash_time = env.now
    env.run(until=20_000)
    assert any(src == victim.address for t, src in senders if t < crash_time)
    assert not any(src == victim.address for t, src in senders if t > crash_time)
    # ...and every live node has removed it from its successor list.
    assert all(victim.address not in {s.addr for s in n.successors}
               for n in nodes if n.alive)


def run_lookups_bounded(env, nodes, count, seed=0):
    """Like run_lookups, but for networks with maintenance running forever:
    run only until all lookups have finished."""
    rng = make_rng(seed, "test-lookups")
    live = [n for n in nodes if n.alive]
    procs = [env.process(rng.choice(live).lookup(f"key-{i}")) for i in range(count)]
    env.run(until=simpy.AllOf(env, procs))
    return [p.value for p in procs]
