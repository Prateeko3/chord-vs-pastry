"""Phase 2 tests for Chord.

First part uses static rings (state filled by build_static) to test the
lookup itself: correct owner, number of hops, latency. Second part starts
from nothing and checks join + stabilize + fix_fingers, and that leaves and
crashes get repaired.
"""

import math

import pytest
import simpy

from chord.node import NodeRef
from chord.ring import Oracle, build_static, check_ring, create_nodes, finger_accuracy
from sim.metrics import mean
from sim.network import Network
from sim.rng import make_rng


# ----- helper functions -----

def static_ring(n, seed=1, **node_args):
    env = simpy.Environment()
    net = Network(env, seed=seed)
    nodes = create_nodes(net, n, **node_args)
    build_static(nodes)
    return env, net, nodes


def save_lookup(origin, key, records):
    rec = yield from origin.lookup(key)
    records.append(rec)


def run_lookups(env, nodes, count, seed=0):
    """Do count lookups for random keys from random live nodes."""
    rng = make_rng(seed, "test-lookups")
    live = [n for n in nodes if n.alive]
    records = []
    for i in range(count):
        origin = rng.choice(live)
        key = f"key-{rng.getrandbits(64):x}"
        env.process(save_lookup(origin, key, records))
    env.run()
    return records


def run_lookups_bounded(env, nodes, count, seed=0):
    """Same as run_lookups but stops when the lookups are done (needed when
    maintenance is running, because then the simulation never ends)."""
    rng = make_rng(seed, "test-lookups")
    live = [n for n in nodes if n.alive]
    procs = []
    for i in range(count):
        procs.append(env.process(rng.choice(live).lookup(f"key-{i}")))
    env.run(until=simpy.AllOf(env, procs))
    return [p.value for p in procs]


def join_one_by_one(env, nodes, gap):
    yield from nodes[0].join(None)
    nodes[0].start_maintenance()
    for node in nodes[1:]:
        ok = yield from node.join(nodes[0].address)
        assert ok
        node.start_maintenance()
        yield env.timeout(gap)


def grow_ring(n, seed=1, gap=200.0, **node_args):
    """Start one node and join the others one by one through it, with
    maintenance running. Returns when all joins are done."""
    env = simpy.Environment()
    net = Network(env, seed=seed)
    nodes = create_nodes(net, n, **node_args)
    env.run(until=env.process(join_one_by_one(env, nodes, gap)))
    return env, net, nodes


def all_correct(oracle, records):
    for r in records:
        if not oracle.check(r):
            return False
    return True


# ----- static ring -----

@pytest.mark.parametrize("n", [10, 100, 1000])
def test_lookups_find_the_right_node(n):
    env, net, nodes = static_ring(n)
    oracle = Oracle(nodes)
    records = run_lookups(env, nodes, 1000)
    for r in records:
        assert r.success
    assert all_correct(oracle, records)


@pytest.mark.parametrize("n", [10, 100, 1000])
def test_average_hops_about_half_log2_n(n):
    env, net, nodes = static_ring(n)
    records = run_lookups(env, nodes, 2000)
    mean_hops = mean([r.hops for r in records])
    # we count hops until the owner, we measured about 1/2*log2(N) + 0.3
    assert abs(mean_hops - 0.5 * math.log2(n)) < 0.75, mean_hops


def test_hops_grow_half_a_hop_when_n_doubles():
    # 256 -> 2048 nodes is 3 doublings
    means = {}
    for n in [256, 2048]:
        env, net, nodes = static_ring(n, seed=5)
        means[n] = mean([r.hops for r in run_lookups(env, nodes, 3000)])
    slope = (means[2048] - means[256]) / 3
    assert 0.4 < slope < 0.6, means


def test_every_id_on_a_small_ring():
    # with m = 8 there are only 256 IDs so we can try all of them
    # (this also tests going past 255 back to 0)
    env, net, nodes = static_ring(40, m=8)
    oracle = Oracle(nodes)
    results = {}

    def ask(origin, ident):
        res = yield from origin.find_successor(ident, "lookup")
        results[ident] = res["owner"].addr

    for ident in range(256):
        env.process(ask(nodes[ident % len(nodes)], ident))
    env.run()
    for ident in range(256):
        assert results[ident] == oracle.successor(ident).address, ident


def test_latency_is_sum_of_link_latencies():
    # origin -> ... -> owner, then the owner answers the origin directly
    env, net, nodes = static_ring(200)
    for rec in run_lookups(env, nodes, 300):
        path = rec.path
        forward = 0
        for i in range(len(path) - 1):
            forward += net.latency(path[i], path[i + 1])
        back = net.latency(path[-1], rec.origin)
        assert len(path) == rec.hops + 1 and path[-1] == rec.owner
        assert rec.latency == pytest.approx(forward + back)


def test_routing_state_is_logarithmic():
    env, net, nodes = static_ring(1000)
    sizes = [n.routing_state_size() for n in nodes]
    # about log2(N) fingers + successor list (8) + predecessor
    assert mean(sizes) < math.log2(1000) + 8 + 1
    assert max(sizes) <= 32 + 8 + 1   # can't be more than m + r + 1


def test_static_builder_is_correct():
    env, net, nodes = static_ring(300)
    assert check_ring(nodes) == []
    assert finger_accuracy(nodes) == 1.0


def test_single_node_owns_everything():
    env = simpy.Environment()
    net = Network(env, seed=1)
    node = create_nodes(net, 1)[0]
    env.run(until=env.process(node.join(None)))
    for r in run_lookups(env, [node], 20):
        assert r.success and r.owner == node.address and r.hops == 0


def test_ids_are_unique():
    env = simpy.Environment()
    net = Network(env, seed=1)
    nodes = create_nodes(net, 12, m=4)      # 12 nodes on a ring with 16 places
    ids = set(n.id for n in nodes)
    assert len(ids) == 12


def lookup_results(seed):
    env, net, nodes = static_ring(300, seed=seed)
    return [(r.hops, r.owner, r.latency) for r in run_lookups(env, nodes, 200)]


def test_same_seed_same_lookups():
    assert lookup_results(9) == lookup_results(9)
    assert lookup_results(9) != lookup_results(10)


# ----- joining and stabilization -----

def test_two_nodes_make_a_ring():
    env, net, nodes = grow_ring(2)
    env.run(until=env.now + 5000)
    a = nodes[0]
    b = nodes[1]
    assert a.successor == b.ref and b.successor == a.ref
    assert a.predecessor == b.ref and b.predecessor == a.ref


def test_joins_one_by_one_give_correct_ring():
    env, net, nodes = grow_ring(60)
    env.run(until=env.now + 60000)    # give stabilize and fix_fingers time
    assert check_ring(nodes) == []
    assert finger_accuracy(nodes) == 1.0
    oracle = Oracle(nodes)
    assert all_correct(oracle, run_lookups_bounded(env, nodes, 300))


def test_joins_at_the_same_time():
    # 30 nodes join through the same node at the same moment.
    # Stabilization should still fix everything (Theorem 4 in the ToN paper).
    env = simpy.Environment()
    net = Network(env, seed=4)
    nodes = create_nodes(net, 31)
    env.run(until=env.process(nodes[0].join(None)))
    nodes[0].start_maintenance()
    for node in nodes[1:]:
        env.process(node.join(nodes[0].address))
        node.start_maintenance()
    env.run(until=env.now + 120000)
    assert check_ring(nodes) == []
    assert finger_accuracy(nodes) == 1.0


def test_join_message_cost():
    # Join 10 nodes into a 1000 node ring and count "join" messages.
    # About log2(N) finger lookups, each ~1/2*log2(N) hops * 2 messages + 1,
    # so around log2(N)^2 + 2*log2(N) = 120. The paper says O(log^2 N).
    env, net, nodes = static_ring(1000)
    costs = []
    for newcomer in create_nodes(net, 10):
        before = net.metrics.count_tag("join")
        assert env.run(until=env.process(newcomer.join(nodes[0].address)))
        costs.append(net.metrics.count_tag("join") - before)
    log_n = math.log2(1000)
    assert log_n < mean(costs) < 2 * log_n ** 2, costs


def test_notify_only_takes_a_closer_predecessor():
    # From the paper: predecessor = n' only if n' is in (predecessor, n).
    # An old notify from a node further back must not replace a good one.
    # (The convergence tests didn't catch this because the ring heals later.)
    env, net, nodes = static_ring(30)
    ring = Oracle(nodes).ring
    target = ring[12]
    good_pred = ring[11]
    farther = ring[10]
    farther.send(target.address, "notify", farther.ref)
    env.run()
    assert target.predecessor == good_pred.ref
    # a node between the predecessor and the target should be accepted
    closer = NodeRef((good_pred.id + target.id) // 2, 999)
    assert good_pred.id < closer.id < target.id
    good_pred.send(target.address, "notify", closer)
    env.run()
    assert target.predecessor == closer


def test_stabilize_finds_node_that_joined_in_between():
    env, net, nodes = static_ring(30)
    ring = Oracle(nodes).ring
    a = ring[5]
    c = ring[6]
    b = create_nodes(net, 1)[0]
    b.set_id((a.id + c.id) // 2)      # put b between a and c
    assert a.id < b.id < c.id
    c.predecessor = b.ref             # like b already notified c
    env.run(until=env.process(a.stabilize()))
    assert a.successor == b.ref


# ----- leaving and crashing -----

def test_leave_fixes_neighbours_without_stabilize():
    env, net, nodes = static_ring(50)   # no maintenance running
    oracle = Oracle(nodes)
    leaver = oracle.ring[10]
    pred = oracle.ring[9]
    succ = oracle.ring[11]
    env.run(until=env.process(leaver.leave()))
    env.run(until=env.now + 500)        # wait for the two messages
    assert pred.successor == succ.ref
    assert succ.predecessor == pred.ref
    assert pred.successors[:3] == [oracle.ring[11].ref, oracle.ring[12].ref, oracle.ring[13].ref]
    assert all_correct(Oracle(nodes), run_lookups(env, nodes, 300))


def test_lookups_correct_right_after_crashes():
    # 20% of nodes crash and nothing repairs the ring. Lookups have to go
    # around the dead nodes using timeouts and the successor list.
    env, net, nodes = static_ring(200)
    for node in make_rng(1, "crash").sample(nodes, 40):
        node.fail()
    oracle = Oracle(nodes)
    records = run_lookups(env, nodes, 500)
    for r in records:
        assert r.success
    assert all_correct(oracle, records)


def test_ring_repairs_after_many_crashes():
    env, net, nodes = static_ring(100)
    for node in nodes:
        node.start_maintenance()
    env.run(until=5000)
    for node in make_rng(2, "crash").sample(nodes, 25):
        node.fail()
    env.run(until=env.now + 60000)
    assert check_ring(nodes) == []
    assert finger_accuracy(nodes) == 1.0


def test_crashed_node_sends_nothing():
    env, net, nodes = static_ring(20)
    for node in nodes:
        node.start_maintenance()
    senders = []          # (time, sender) of every message
    real_send = net.send

    def send_and_record(msg):
        senders.append((env.now, msg.src))
        real_send(msg)

    net.send = send_and_record
    env.run(until=3000)
    victim = nodes[3]
    victim.fail()
    crash_time = env.now
    env.run(until=20000)
    sent_before = [s for t, s in senders if t < crash_time and s == victim.address]
    sent_after = [s for t, s in senders if t > crash_time and s == victim.address]
    assert len(sent_before) > 0
    assert len(sent_after) == 0
    # and no live node still has it in its successor list
    for n in nodes:
        if n.alive:
            for s in n.successors:
                assert s.addr != victim.address
