"""Phase 2 demo: Chord on its own, compared with the paper's predictions.

Part A  Static rings of growing size (perfect routing state):
        hops vs 1/2*log2(N), lookup latency, routing-state size.
Part B  A ring built by real joins, then 25% of nodes crash:
        join cost, convergence, lookups during and after repair.

This is a sanity check of the implementation, not the final comparison
(that is Phase 5, with Pastry alongside and more seeds).

Run:
    python -m experiments.phase2_chord_demo
    python -m experiments.phase2_chord_demo --seeds 1 2 3 4 5 --lookups 2000
"""

from __future__ import annotations

import argparse
import math
import statistics

import simpy

from chord.ring import Oracle, build_static, check_ring, create_nodes, finger_accuracy
from sim.metrics import summarize
from sim.network import Network
from sim.rng import make_rng


def lookups(env, nodes, count, seed, label):
    """Run `count` lookups from random live nodes; return their records."""
    rng = make_rng(seed, f"demo-lookups-{label}")
    live = [n for n in nodes if n.alive]
    procs = [env.process(rng.choice(live).lookup(f"key-{rng.getrandbits(64):x}"))
             for _ in range(count)]
    env.run(until=simpy.AllOf(env, procs))
    return [p.value for p in procs]


def part_a(sizes, seeds, n_lookups):
    print("A. Static rings: averages over seeds", seeds, f"({n_lookups} lookups per seed)")
    print(f"{'N':>6}{'hops':>8}{'±seed':>7}{'p95':>6}{'½log2N':>8}"
          f"{'latency ms':>12}{'state':>7}{'correct':>9}")
    for n in sizes:
        per_seed_hops, p95s, lats, states, correct = [], [], [], [], 0
        for seed in seeds:
            env = simpy.Environment()
            net = Network(env, seed=seed)
            nodes = create_nodes(net, n)
            build_static(nodes)
            recs = lookups(env, nodes, n_lookups, seed, "static")
            oracle = Oracle(nodes)
            correct += sum(oracle.check(r) for r in recs)
            per_seed_hops.append(statistics.fmean(r.hops for r in recs))
            p95s.append(summarize(r.hops for r in recs)["p95"])
            lats.append(statistics.fmean(r.latency for r in recs))
            states.append(statistics.fmean(nd.routing_state_size() for nd in nodes))
        spread = statistics.stdev(per_seed_hops) if len(seeds) > 1 else 0.0
        total = n_lookups * len(seeds)
        print(f"{n:>6}{statistics.fmean(per_seed_hops):8.2f}{spread:7.2f}"
              f"{statistics.fmean(p95s):6.1f}{0.5 * math.log2(n):8.2f}"
              f"{statistics.fmean(lats):12.1f}{statistics.fmean(states):7.1f}"
              f"{100 * correct / total:8.1f}%")


def part_b(n, seed):
    print(f"\nB. Dynamic ring: {n} nodes join one by one (seed {seed})")
    env = simpy.Environment()
    net = Network(env, seed=seed)
    nodes = create_nodes(net, n)
    join_costs = []

    def grow():
        yield from nodes[0].join(None)
        nodes[0].start_maintenance()
        for node in nodes[1:]:
            before = net.metrics.sent_by_tag["join"]
            yield from node.join(nodes[0].address)
            join_costs.append(net.metrics.sent_by_tag["join"] - before)
            node.start_maintenance()
            yield env.timeout(200)

    env.run(until=env.process(grow()))
    print(f"   all joined at t={env.now / 1000:.0f} s; messages per join: "
          f"mean {statistics.fmean(join_costs):.1f}, max {max(join_costs)}")
    for wait in (0, 5_000, 30_000):
        if wait:
            env.run(until=env.now + wait)
        print(f"   +{wait / 1000:>4.0f} s: ring pointer errors {len(check_ring(nodes)):>3}, "
              f"fingers correct {100 * finger_accuracy(nodes):5.1f}%")

    def lookup_report(label):
        recs = lookups(env, nodes, 500, seed, label)
        oracle = Oracle(nodes)
        ok = [r for r in recs if r.success]
        print(f"   {label:<28} success {100 * len(ok) / len(recs):5.1f}%  "
              f"correct {100 * sum(oracle.check(r) for r in recs) / len(recs):5.1f}%  "
              f"latency mean {statistics.fmean(r.latency for r in ok):6.0f} ms  "
              f"hops {statistics.fmean(r.hops for r in ok):.2f}")

    lookup_report("before crash")
    for node in make_rng(seed, "demo-crash").sample(nodes, n // 4):
        node.fail()
    print(f"   -- {n // 4} nodes (25%) crash at t={env.now / 1000:.0f} s --")
    lookup_report("immediately after crash")
    env.run(until=env.now + 60_000)
    print(f"   +60 s: ring pointer errors {len(check_ring(nodes))}, "
          f"fingers correct {100 * finger_accuracy(nodes):.1f}%")
    lookup_report("after 60 s of repair")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--sizes", type=int, nargs="+", default=[16, 64, 256, 1024, 4096])
    p.add_argument("--seeds", type=int, nargs="+", default=[1, 2, 3])
    p.add_argument("--lookups", type=int, default=1000)
    p.add_argument("--dynamic-n", type=int, default=100)
    a = p.parse_args()
    part_a(a.sizes, a.seeds, a.lookups)
    part_b(a.dynamic_n, a.seeds[0])


if __name__ == "__main__":
    main()
