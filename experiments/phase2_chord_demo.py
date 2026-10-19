"""Phase 2 demo: Chord alone, compared with what the paper says.

Part A: static rings of different sizes -> hops vs 1/2*log2(N), latency,
        routing state size
Part B: ring built with real joins, then 25% of the nodes crash -> join cost,
        how fast the ring gets correct, lookups during and after the repair

Run:
    python -m experiments.phase2_chord_demo
    python -m experiments.phase2_chord_demo --seeds 1 2 3 4 5 --lookups 2000
"""

import argparse
import math
import statistics

import simpy

from chord.ring import Oracle, build_static, check_ring, create_nodes, finger_accuracy
from sim.metrics import mean, summarize
from sim.network import Network
from sim.rng import make_rng


def lookups(env, nodes, count, seed, label):
    """Do count lookups from random live nodes and return the records."""
    rng = make_rng(seed, "demo-lookups-" + label)
    live = [n for n in nodes if n.alive]
    procs = []
    for i in range(count):
        origin = rng.choice(live)
        key = f"key-{rng.getrandbits(64):x}"
        procs.append(env.process(origin.lookup(key)))
    env.run(until=simpy.AllOf(env, procs))
    return [p.value for p in procs]


def part_a(sizes, seeds, n_lookups):
    print("A. Static rings: averages over seeds", seeds, f"({n_lookups} lookups per seed)")
    print(f"{'N':>6}{'hops':>8}{'±seed':>7}{'p95':>6}{'½log2N':>8}"
          f"{'latency ms':>12}{'state':>7}{'correct':>9}")
    for n in sizes:
        hops_per_seed = []
        p95s = []
        lats = []
        states = []
        correct = 0
        for seed in seeds:
            env = simpy.Environment()
            net = Network(env, seed=seed)
            nodes = create_nodes(net, n)
            build_static(nodes)
            recs = lookups(env, nodes, n_lookups, seed, "static")
            oracle = Oracle(nodes)
            for r in recs:
                if oracle.check(r):
                    correct += 1
            hops_per_seed.append(mean([r.hops for r in recs]))
            p95s.append(summarize([r.hops for r in recs])["p95"])
            lats.append(mean([r.latency for r in recs]))
            states.append(mean([nd.routing_state_size() for nd in nodes]))
        if len(seeds) > 1:
            spread = statistics.stdev(hops_per_seed)
        else:
            spread = 0.0
        total = n_lookups * len(seeds)
        print(f"{n:>6}{mean(hops_per_seed):8.2f}{spread:7.2f}"
              f"{mean(p95s):6.1f}{0.5 * math.log2(n):8.2f}"
              f"{mean(lats):12.1f}{mean(states):7.1f}"
              f"{100 * correct / total:8.1f}%")


def grow(env, net, nodes, join_costs):
    """Join the nodes one by one through the first node."""
    yield from nodes[0].join(None)
    nodes[0].start_maintenance()
    for node in nodes[1:]:
        before = net.metrics.count_tag("join")
        yield from node.join(nodes[0].address)
        join_costs.append(net.metrics.count_tag("join") - before)
        node.start_maintenance()
        yield env.timeout(200)


def lookup_report(env, nodes, seed, label):
    recs = lookups(env, nodes, 500, seed, label)
    oracle = Oracle(nodes)
    ok = [r for r in recs if r.success]
    correct = 0
    for r in recs:
        if oracle.check(r):
            correct += 1
    print(f"   {label:<28} success {100 * len(ok) / len(recs):5.1f}%  "
          f"correct {100 * correct / len(recs):5.1f}%  "
          f"latency mean {mean([r.latency for r in ok]):6.0f} ms  "
          f"hops {mean([r.hops for r in ok]):.2f}")


def part_b(n, seed):
    print(f"\nB. Dynamic ring: {n} nodes join one by one (seed {seed})")
    env = simpy.Environment()
    net = Network(env, seed=seed)
    nodes = create_nodes(net, n)
    join_costs = []
    env.run(until=env.process(grow(env, net, nodes, join_costs)))
    print(f"   all joined at t={env.now / 1000:.0f} s; messages per join: "
          f"mean {mean(join_costs):.1f}, max {max(join_costs)}")
    for wait in [0, 5000, 30000]:
        if wait > 0:
            env.run(until=env.now + wait)
        print(f"   +{wait / 1000:>4.0f} s: ring pointer errors {len(check_ring(nodes)):>3}, "
              f"fingers correct {100 * finger_accuracy(nodes):5.1f}%")

    lookup_report(env, nodes, seed, "before crash")
    for node in make_rng(seed, "demo-crash").sample(nodes, n // 4):
        node.fail()
    print(f"   -- {n // 4} nodes (25%) crash at t={env.now / 1000:.0f} s --")
    lookup_report(env, nodes, seed, "immediately after crash")
    env.run(until=env.now + 60000)
    print(f"   +60 s: ring pointer errors {len(check_ring(nodes))}, "
          f"fingers correct {100 * finger_accuracy(nodes):.1f}%")
    lookup_report(env, nodes, seed, "after 60 s of repair")


def main():
    parser = argparse.ArgumentParser(description="Phase 2 demo: Chord")
    parser.add_argument("--sizes", type=int, nargs="+", default=[16, 64, 256, 1024, 4096])
    parser.add_argument("--seeds", type=int, nargs="+", default=[1, 2, 3])
    parser.add_argument("--lookups", type=int, default=1000)
    parser.add_argument("--dynamic-n", type=int, default=100)
    args = parser.parse_args()
    part_a(args.sizes, args.seeds, args.lookups)
    part_b(args.dynamic_n, args.seeds[0])


if __name__ == "__main__":
    main()
