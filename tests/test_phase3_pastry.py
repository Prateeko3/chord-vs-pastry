"""Phase 3 tests for Pastry.

Static networks (build_static) test the routing. The other tests check
joining, leaf set maintenance, leaving and repair after crashes.
"""

import math

import pytest
import simpy

from pastry.node import ID_BITS, PastryNode
from pastry.ring import (Oracle, build_static, check_leafsets, create_nodes, nearest_node,
                         table_quality)
from sim.metrics import mean
from sim.network import Network
from sim.rng import make_rng
from sim.topology import Clustered, UniformSquare


# ----- helper functions -----

def static_net(n, seed=1, proximity="optimal", topology=None, **node_args):
    env = simpy.Environment()
    net = Network(env, seed=seed, topology=topology)
    nodes = create_nodes(net, n, **node_args)
    build_static(nodes, proximity, seed=seed)
    return env, net, nodes


def run_lookups(env, nodes, count, seed=0):
    """Do count lookups from random live nodes and wait until they finish."""
    rng = make_rng(seed, "test-lookups")
    live = [n for n in nodes if n.alive]
    procs = []
    for i in range(count):
        origin = rng.choice(live)
        key = f"key-{rng.getrandbits(64):x}"
        procs.append(env.process(origin.lookup(key)))
    env.run(until=simpy.AllOf(env, procs))
    return [p.value for p in procs]


def route_to(env, origin, key_id):
    proc = env.process(origin.route(key_id, "lookup"))
    env.run(until=proc)
    return proc.value


def join_all(env, nodes, gap):
    yield from nodes[0].join(None)
    nodes[0].start_maintenance()
    for i in range(1, len(nodes)):
        node = nodes[i]
        ok = yield from node.join(nearest_node(node, nodes[:i]).address)
        assert ok
        node.start_maintenance()
        yield env.timeout(gap)


def grow(n, seed=1, gap=100.0, topology=None, **node_args):
    """Nodes join one by one, each through the nearest node already in,
    and maintenance runs on all of them."""
    env = simpy.Environment()
    net = Network(env, seed=seed, topology=topology)
    nodes = create_nodes(net, n, **node_args)
    env.run(until=env.process(join_all(env, nodes, gap)))
    return env, net, nodes


def all_correct(oracle, records):
    for r in records:
        if not oracle.check(r):
            return False
    return True


# ----- ID functions -----

def test_digits_prefix_and_distance():
    env, net, nodes = static_net(1)
    node = nodes[0]
    a = int("12345678" + "0" * 24, 16)
    c = int("12340000" + "f" * 24, 16)
    assert [node.digit(a, i) for i in range(5)] == [1, 2, 3, 4, 5]
    assert node.shl(a, c) == 4
    assert node.shl(a, a) == 32
    assert node.shl(0, 2 ** 127) == 0
    top = 2 ** ID_BITS - 1
    assert node.dist(top, 1) == 2          # goes around through 0
    assert node.dist(5, 9) == 4


# ----- static networks -----

@pytest.mark.parametrize("proximity", ["optimal", "random"])
@pytest.mark.parametrize("n", [10, 100, 1000])
def test_lookups_reach_numerically_closest_node(n, proximity):
    env, net, nodes = static_net(n, proximity=proximity)
    oracle = Oracle(nodes)
    records = run_lookups(env, nodes, 1000)
    for r in records:
        assert r.success
    assert all_correct(oracle, records)


def test_special_keys():
    # keys equal to node IDs, one above/below, halfway between two nodes,
    # and the two ends of the ID space
    env, net, nodes = static_net(200)
    oracle = Oracle(nodes)
    ids = oracle.ids
    keys = [0, 1, 2 ** ID_BITS - 1, 2 ** ID_BITS // 2]
    for i in range(0, 200, 10):
        nxt = ids[(i + 1) % 200]
        keys.append(ids[i])
        keys.append(ids[i] - 1)
        keys.append(ids[i] + 1)
        keys.append((ids[i] + nxt) // 2)
    rng = make_rng(1, "boundary")
    for key in keys:
        key = key % 2 ** ID_BITS
        res = route_to(env, rng.choice(nodes), key)
        assert res["owner"].addr == oracle.owner(key).address, hex(key)


@pytest.mark.parametrize("n", [10, 100, 1000])
def test_average_hops_about_log16_n(n):
    env, net, nodes = static_net(n)
    mean_hops = mean([r.hops for r in run_lookups(env, nodes, 2000)])
    assert abs(mean_hops - math.log(n, 16)) < 0.4, mean_hops


def test_hops_grow_about_one_for_16_times_more_nodes():
    means = {}
    for n in [256, 4096]:
        env, net, nodes = static_net(n, seed=5)
        means[n] = mean([r.hops for r in run_lookups(env, nodes, 3000)])
    assert 0.7 < means[4096] - means[256] < 1.2, means


def test_latency_is_sum_of_link_latencies():
    env, net, nodes = static_net(300)
    for rec in run_lookups(env, nodes, 300):
        p = rec.path
        forward = 0
        for i in range(len(p) - 1):
            forward += net.latency(p[i], p[i + 1])
        assert rec.latency == pytest.approx(forward + net.latency(p[-1], rec.origin))


def test_routing_state_size_like_in_paper():
    # paper: about (2^b - 1) * log16(N) table entries, plus |L| + |M|
    env, net, nodes = static_net(1000)
    sizes = [n.routing_state_size() for n in nodes]
    upper = 15 * math.ceil(math.log(1000, 16)) + 16 + 32
    assert mean(sizes) < upper
    table = mean([len(n.table_entries()) for n in nodes])
    assert 15 * math.floor(math.log(1000, 16)) <= table <= 15 * math.ceil(math.log(1000, 16))


def test_static_state_is_perfect():
    env, net, nodes = static_net(500)
    assert check_leafsets(nodes) == []
    q = table_quality(nodes)
    assert q["completeness"] == 1.0
    assert q["latency_ratio"] == pytest.approx(1.0)


# ----- rare case and locality -----

def test_rare_case_gives_a_valid_node():
    env, net, nodes = static_net(1000)
    node = nodes[0]
    rng = make_rng(3, "rare")
    checked = 0
    while checked < 50:
        key = rng.getrandbits(ID_BITS)
        if node.in_leaf_range(key):
            continue
        row = node.shl(key, node.id)
        node.table[row][node.digit(key, row)] = None   # force the rare case
        step = node.next_hop({"key": key})
        assert step is not None
        t = step[0]
        assert node.shl(t.id, key) >= row
        assert node.dist(t.id, key) < node.dist(node.id, key)
        checked += 1


def test_lookups_correct_with_most_table_entries_removed():
    # correctness depends on the leaf set, the table only makes it faster
    env, net, nodes = static_net(500)
    rng = make_rng(4, "holes")
    for node in nodes:
        for row in node.table:
            for col in range(len(row)):
                if rng.random() < 0.7:
                    row[col] = None
    oracle = Oracle(nodes)
    assert all_correct(oracle, run_lookups(env, nodes, 500))


@pytest.mark.parametrize("topology", [UniformSquare(), Clustered()])
def test_locality_lowers_latency_but_not_hops(topology):
    # same seed = same positions and IDs, only the table filling is different
    results = {}
    for mode in ["optimal", "random"]:
        env, net, nodes = static_net(1000, seed=7, proximity=mode, topology=topology)
        recs = run_lookups(env, nodes, 1500)
        results[mode] = (mean([r.hops for r in recs]), mean([r.latency for r in recs]))
    hops_opt, lat_opt = results["optimal"]
    hops_rand, lat_rand = results["random"]
    assert abs(hops_opt - hops_rand) < 0.1
    assert lat_opt < 0.8 * lat_rand, results


# ----- joining -----

def test_two_nodes_know_each_other():
    env, net, nodes = grow(2)
    a = nodes[0]
    b = nodes[1]
    assert a.leaves() == [b.ref] and b.leaves() == [a.ref]


def test_joins_give_correct_and_local_state():
    env, net, nodes = grow(120, topology=Clustered())
    env.run(until=env.now + 10000)
    assert check_leafsets(nodes) == []
    q = table_quality(nodes)
    assert q["completeness"] > 0.95
    assert q["latency_ratio"] < 1.2      # close to the best possible choice
    oracle = Oracle(nodes)
    assert all_correct(oracle, run_lookups(env, nodes, 300))


def test_joins_at_the_same_time():
    env, net, nodes = static_net(10)
    for node in nodes:
        node.start_maintenance()
    newcomers = create_nodes(net, 40)
    for node in newcomers:
        env.process(node.join(nearest_node(node, nodes).address))
        node.start_maintenance()
    env.run(until=60000)
    everyone = nodes + newcomers
    assert check_leafsets(everyone) == []
    oracle = Oracle(everyone)
    assert all_correct(oracle, run_lookups(env, everyone, 300))


def test_join_message_cost():
    # route (about log16 N hops * 2 + 1) + refine (2 per node in table and
    # neighborhood) + announce (1 per known node), so a few times the state size
    env, net, nodes = static_net(1000)
    costs = []
    sizes = []
    for node in create_nodes(net, 10):
        before = net.metrics.count_tag("join")
        assert env.run(until=env.process(node.join(nearest_node(node, nodes).address)))
        costs.append(net.metrics.count_tag("join") - before)
        sizes.append(node.routing_state_size())
    assert mean(sizes) < mean(costs) < 4 * mean(sizes)


def test_join_without_refine_uses_less_messages():
    env, net, nodes = static_net(500)
    costs = {}
    for refine in [True, False]:
        node = create_nodes(net, 1, join_refine=refine)[0]
        before = net.metrics.count_tag("join")
        env.run(until=env.process(node.join(nearest_node(node, nodes).address)))
        costs[refine] = net.metrics.count_tag("join") - before
    assert costs[False] < costs[True]


# ----- leaving and crashing -----

def test_leave_fixes_leaf_sets_without_maintenance():
    env, net, nodes = static_net(60)
    leaver = Oracle(nodes).ring[20]
    env.run(until=env.process(leaver.leave()))
    env.run(until=env.now + 500)
    assert check_leafsets(nodes) == []
    oracle = Oracle(nodes)
    assert all_correct(oracle, run_lookups(env, nodes, 300))


def test_lookups_correct_right_after_crashes():
    env, net, nodes = static_net(300)
    for node in make_rng(1, "crash").sample(nodes, 60):
        node.fail()
    oracle = Oracle(nodes)
    records = run_lookups(env, nodes, 500)
    for r in records:
        assert r.success
    assert all_correct(oracle, records)


def test_leaf_sets_repair_after_many_crashes():
    env, net, nodes = static_net(200)
    for node in nodes:
        node.start_maintenance()
    env.run(until=3000)
    for node in make_rng(2, "crash").sample(nodes, 50):
        node.fail()
    env.run(until=env.now + 60000)
    assert check_leafsets(nodes) == []    # this is what correctness needs
    oracle = Oracle(nodes)
    assert all_correct(oracle, run_lookups(env, nodes, 300))


def test_neighborhood_sets_drop_dead_nodes_later():
    # the neighborhood set is checked one node per round, so it takes much
    # longer than the leaf set (we measured 2-4 minutes)
    env, net, nodes = static_net(100)
    for node in nodes:
        node.start_maintenance()
    env.run(until=3000)
    for node in make_rng(2, "crash").sample(nodes, 25):
        node.fail()
    env.run(until=env.now + 240000)
    live = set(n.address for n in nodes if n.alive)
    for n in nodes:
        if n.alive:
            for r in n.neighborhood:
                assert r.addr in live


def test_crashed_node_sends_nothing():
    env, net, nodes = static_net(30)
    for node in nodes:
        node.start_maintenance()
    senders = []
    real_send = net.send

    def send_and_record(msg):
        senders.append((env.now, msg.src))
        real_send(msg)

    net.send = send_and_record
    env.run(until=3000)
    victim = nodes[5]
    victim.fail()
    t = env.now
    env.run(until=15000)
    sent_before = [s for when, s in senders if when < t and s == victim.address]
    sent_after = [s for when, s in senders if when > t and s == victim.address]
    assert len(sent_before) > 0
    assert len(sent_after) == 0


def lookup_results(seed):
    env, net, nodes = static_net(300, seed=seed)
    return [(r.hops, r.owner, r.latency) for r in run_lookups(env, nodes, 200)]


def test_same_seed_same_results():
    assert lookup_results(3) == lookup_results(3)
    assert lookup_results(3) != lookup_results(4)


def test_bad_parameters():
    env = simpy.Environment()
    net = Network(env, seed=1)
    with pytest.raises(ValueError):
        net.create_node(PastryNode, b=3)
    with pytest.raises(ValueError):
        net.create_node(PastryNode, leaf_size=7)
