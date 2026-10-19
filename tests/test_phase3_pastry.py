"""Phase 3 tests: Pastry.

Static networks (pastry.ring.build_static) test the routing algorithm;
dynamic tests check join, leaf-set maintenance, leave and crash repair.
"""

import math
import statistics

import pytest
import simpy

from pastry.node import ID_BITS, PastryNode
from pastry.ring import (Oracle, build_static, check_leafsets, create_nodes, nearest_node,
                         table_quality)
from sim.network import Network
from sim.rng import make_rng
from sim.topology import Clustered, UniformSquare


# --- helpers ------------------------------------------------------------------

def static_net(n, seed=1, proximity="optimal", topology=None, **node_kw):
    env = simpy.Environment()
    net = Network(env, seed=seed, topology=topology)
    nodes = create_nodes(net, n, **node_kw)
    build_static(nodes, proximity, seed=seed)
    return env, net, nodes


def run_lookups(env, nodes, count, seed=0):
    """`count` lookups for random keys from random live nodes; returns records.
    Runs only until those lookups finish (maintenance may run forever)."""
    rng = make_rng(seed, "test-lookups")
    live = [n for n in nodes if n.alive]
    procs = [env.process(rng.choice(live).lookup(f"key-{rng.getrandbits(64):x}"))
             for _ in range(count)]
    env.run(until=simpy.AllOf(env, procs))
    return [p.value for p in procs]


def route_to(env, origin, key_id):
    proc = env.process(origin.route(key_id, tag="lookup"))
    env.run(until=proc)
    return proc.value


def grow(n, seed=1, gap=100.0, topology=None, **node_kw):
    """Join nodes one at a time, each through the physically nearest node
    already in the network, with maintenance running everywhere."""
    env = simpy.Environment()
    net = Network(env, seed=seed, topology=topology)
    nodes = create_nodes(net, n, **node_kw)

    def go():
        yield from nodes[0].join(None)
        nodes[0].start_maintenance()
        for i, node in enumerate(nodes[1:], start=1):
            ok = yield from node.join(nearest_node(node, nodes[:i]).address)
            assert ok
            node.start_maintenance()
            yield env.timeout(gap)

    env.run(until=env.process(go()))
    return env, net, nodes


# --- ID arithmetic ------------------------------------------------------------

def test_digits_shared_prefix_and_distance():
    env, net, (node,) = static_net(1)
    a = int("12345678" + "0" * 24, 16)
    c = int("12340000" + "f" * 24, 16)
    assert [node.digit(a, i) for i in range(5)] == [1, 2, 3, 4, 5]
    assert node.shl(a, c) == 4
    assert node.shl(a, a) == 32
    assert node.shl(0, 1 << 127) == 0
    top = (1 << ID_BITS) - 1
    assert node.dist(top, 1) == 2          # circular: wraps through zero
    assert node.dist(5, 9) == 4


# --- static networks: correctness and cost ------------------------------------

@pytest.mark.parametrize("proximity", ["optimal", "random"])
@pytest.mark.parametrize("n", [10, 100, 1000])
def test_lookups_always_reach_numerically_closest_node(n, proximity):
    env, net, nodes = static_net(n, proximity=proximity)
    oracle = Oracle(nodes)
    records = run_lookups(env, nodes, 1000)
    assert all(r.success for r in records)
    assert all(oracle.check(r) for r in records)


def test_boundary_keys_route_correctly():
    # Keys exactly on node IDs, one either side, half-way between
    # neighbours, and the two ends of the ID space (wrap-around).
    env, net, nodes = static_net(200)
    oracle = Oracle(nodes)
    ids = oracle.ids
    keys = [0, 1, (1 << ID_BITS) - 1, (1 << ID_BITS) // 2]
    for i in range(0, 200, 10):
        nxt = ids[(i + 1) % 200]
        keys += [ids[i], ids[i] - 1, ids[i] + 1, (ids[i] + nxt) // 2]
    rng = make_rng(1, "boundary")
    for key in keys:
        key %= 1 << ID_BITS
        res = route_to(env, rng.choice(nodes), key)
        assert res["owner"].addr == oracle.owner(key).address, hex(key)


@pytest.mark.parametrize("n", [10, 100, 1000])
def test_average_hops_is_about_log16_n(n):
    env, net, nodes = static_net(n)
    mean_hops = statistics.fmean(r.hops for r in run_lookups(env, nodes, 2000))
    assert abs(mean_hops - math.log(n, 16)) < 0.4, mean_hops


def test_hops_grow_by_about_one_per_16x_nodes():
    means = {}
    for n in (256, 4096):
        env, net, nodes = static_net(n, seed=5)
        means[n] = statistics.fmean(r.hops for r in run_lookups(env, nodes, 3000))
    assert 0.7 < means[4096] - means[256] < 1.2, means


def test_lookup_latency_equals_sum_of_link_latencies_on_the_path():
    env, net, nodes = static_net(300)
    for rec in run_lookups(env, nodes, 300):
        p = rec.path
        forward = sum(net.latency(a, b) for a, b in zip(p, p[1:]))
        assert rec.latency == pytest.approx(forward + net.latency(p[-1], rec.origin))


def test_routing_state_size_matches_the_paper():
    # Paper: about (2^b - 1) * ceil(log16 N) table entries, plus |L| + |M|.
    env, net, nodes = static_net(1000)
    sizes = [n.routing_state_size() for n in nodes]
    upper = 15 * math.ceil(math.log(1000, 16)) + 16 + 32
    assert statistics.fmean(sizes) < upper
    table = statistics.fmean(len(n.table_entries()) for n in nodes)
    assert 15 * math.floor(math.log(1000, 16)) <= table <= 15 * math.ceil(math.log(1000, 16))


def test_static_leaf_sets_and_tables_are_perfect():
    env, net, nodes = static_net(500)
    assert check_leafsets(nodes) == []
    q = table_quality(nodes)
    assert q["completeness"] == 1.0 and q["latency_ratio"] == pytest.approx(1.0)


# --- the "rare case" and locality ---------------------------------------------

def test_rare_case_picks_a_valid_node_when_table_slot_is_empty():
    env, net, nodes = static_net(1000)
    node = nodes[0]
    rng = make_rng(3, "rare")
    checked = 0
    while checked < 50:
        key = rng.getrandbits(ID_BITS)
        if node._in_leaf_range(key):
            continue
        row = node.shl(key, node.id)
        node.table[row][node.digit(key, row)] = None       # force the rare case
        step = node.next_hop({"key": key})
        assert step is not None
        t = step[0]
        assert node.shl(t.id, key) >= row
        assert node.dist(t.id, key) < node.dist(node.id, key)
        checked += 1


def test_lookups_correct_even_with_most_table_entries_missing():
    # Correctness rests on the leaf set; the table only adds speed.
    env, net, nodes = static_net(500)
    rng = make_rng(4, "holes")
    for node in nodes:
        for row in node.table:
            for col in range(len(row)):
                if rng.random() < 0.7:
                    row[col] = None
    oracle = Oracle(nodes)
    assert all(oracle.check(r) for r in run_lookups(env, nodes, 500))


@pytest.mark.parametrize("topology", [UniformSquare(), Clustered()])
def test_proximity_aware_tables_cut_latency_not_hops(topology):
    # Same seed -> same node positions and IDs: a paired comparison.
    results = {}
    for mode in ("optimal", "random"):
        env, net, nodes = static_net(1000, seed=7, proximity=mode, topology=topology)
        recs = run_lookups(env, nodes, 1500)
        results[mode] = (statistics.fmean(r.hops for r in recs),
                         statistics.fmean(r.latency for r in recs))
    (hops_opt, lat_opt), (hops_rand, lat_rand) = results["optimal"], results["random"]
    assert abs(hops_opt - hops_rand) < 0.1
    assert lat_opt < 0.8 * lat_rand, results


# --- dynamic membership --------------------------------------------------------

def test_two_nodes_find_each_other():
    env, net, nodes = grow(2)
    a, b = nodes
    assert a.leaves() == [b.ref] and b.leaves() == [a.ref]


def test_sequential_joins_build_correct_and_local_state():
    env, net, nodes = grow(120, topology=Clustered())
    env.run(until=env.now + 10_000)
    assert check_leafsets(nodes) == []
    q = table_quality(nodes)
    assert q["completeness"] > 0.95
    assert q["latency_ratio"] < 1.2          # close to the physically best choice
    oracle = Oracle(nodes)
    assert all(oracle.check(r) for r in run_lookups(env, nodes, 300))


def test_concurrent_joins_converge():
    env, net, nodes = static_net(10)
    for node in nodes:
        node.start_maintenance()
    newcomers = create_nodes(net, 40)
    for node in newcomers:
        env.process(node.join(nearest_node(node, nodes).address))
        node.start_maintenance()
    env.run(until=60_000)
    everyone = nodes + newcomers
    assert check_leafsets(everyone) == []
    oracle = Oracle(everyone)
    assert all(oracle.check(r) for r in run_lookups(env, everyone, 300))


def test_join_message_cost():
    # Route (~log16 N hops x 2 + 1) + refine (2 per node in R u M) +
    # announce (1 per node in L u R u M): a few times the state size.
    env, net, nodes = static_net(1000)
    costs, sizes = [], []
    for node in create_nodes(net, 10):
        before = net.metrics.sent_by_tag["join"]
        assert env.run(until=env.process(node.join(nearest_node(node, nodes).address)))
        costs.append(net.metrics.sent_by_tag["join"] - before)
        sizes.append(node.routing_state_size())
    assert statistics.fmean(sizes) < statistics.fmean(costs) < 4 * statistics.fmean(sizes)


def test_join_without_refine_is_cheaper():
    env, net, nodes = static_net(500)
    costs = {}
    for refine in (True, False):
        node = create_nodes(net, 1, join_refine=refine)[0]
        before = net.metrics.sent_by_tag["join"]
        env.run(until=env.process(node.join(nearest_node(node, nodes).address)))
        costs[refine] = net.metrics.sent_by_tag["join"] - before
    assert costs[False] < costs[True]


# --- leaving and crashing -----------------------------------------------------

def test_graceful_leave_fixes_leaf_sets_without_maintenance():
    env, net, nodes = static_net(60)
    leaver = Oracle(nodes).ring[20]
    env.run(until=env.process(leaver.leave()))
    env.run(until=env.now + 500)
    assert check_leafsets(nodes) == []
    oracle = Oracle(nodes)
    assert all(oracle.check(r) for r in run_lookups(env, nodes, 300))


def test_lookups_stay_correct_right_after_crashes_without_maintenance():
    env, net, nodes = static_net(300)
    for node in make_rng(1, "crash").sample(nodes, 60):
        node.fail()
    oracle = Oracle(nodes)
    records = run_lookups(env, nodes, 500)
    assert all(r.success for r in records)
    assert all(oracle.check(r) for r in records)


def test_leaf_sets_repair_after_mass_crash():
    env, net, nodes = static_net(200)
    for node in nodes:
        node.start_maintenance()
    env.run(until=3000)
    for node in make_rng(2, "crash").sample(nodes, 50):
        node.fail()
    env.run(until=env.now + 60_000)
    assert check_leafsets(nodes) == []        # the correctness-critical state
    oracle = Oracle(nodes)
    assert all(oracle.check(r) for r in run_lookups(env, nodes, 300))


def test_neighborhood_sets_eventually_drop_dead_nodes():
    # The neighborhood set is probed one member per round, so it is cleaned
    # much more slowly than the leaf set (measured: ~2-4 minutes).
    env, net, nodes = static_net(100)
    for node in nodes:
        node.start_maintenance()
    env.run(until=3000)
    for node in make_rng(2, "crash").sample(nodes, 25):
        node.fail()
    env.run(until=env.now + 240_000)
    live = {n.address for n in nodes if n.alive}
    assert all(r.addr in live for n in nodes if n.alive for r in n.neighborhood)


def test_crashed_node_sends_nothing():
    env, net, nodes = static_net(30)
    for node in nodes:
        node.start_maintenance()
    senders = []
    real_send = net.send
    net.send = lambda msg: (senders.append((env.now, msg.src)), real_send(msg))
    env.run(until=3000)
    victim = nodes[5]
    victim.fail()
    t = env.now
    env.run(until=15_000)
    assert any(s == victim.address for when, s in senders if when < t)
    assert not any(s == victim.address for when, s in senders if when > t)


def test_same_seed_same_results():
    def run(seed):
        env, net, nodes = static_net(300, seed=seed)
        return [(r.hops, r.owner, r.latency) for r in run_lookups(env, nodes, 200)]
    assert run(3) == run(3)
    assert run(3) != run(4)


def test_bad_parameters_rejected():
    env = simpy.Environment()
    net = Network(env, seed=1)
    with pytest.raises(ValueError):
        net.create_node(PastryNode, b=3)
    with pytest.raises(ValueError):
        net.create_node(PastryNode, leaf_size=7)
