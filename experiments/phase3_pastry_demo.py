"""Phase 3 demo: Pastry alone, compared with what the paper says.

Part A: static networks of different sizes with "optimal" tables (closest
        node in every entry) or "random" tables (no locality): hops vs
        log16(N), latency, route stretch, state size
Part B: network built with real joins (each node joins through its nearest
        node): join cost with and without the refine step, table quality
Part C: 25% of nodes crash: lookups right after, and how fast the leaf sets,
        neighborhood sets and tables get cleaned

Route stretch = latency along the route / direct latency from origin to owner.

Run:
    python -m experiments.phase3_pastry_demo
    python -m experiments.phase3_pastry_demo --seeds 1 2 3 4 5
"""

import argparse
import math
import statistics

import simpy

from pastry.ring import (Oracle, build_static, check_leafsets, create_nodes, nearest_node,
                         table_quality)
from sim.metrics import mean, summarize
from sim.network import Network
from sim.rng import make_rng
from sim.topology import Clustered, UniformSquare


def lookups(env, nodes, count, seed, label):
    rng = make_rng(seed, "demo-lookups-" + label)
    live = [n for n in nodes if n.alive]
    procs = []
    for i in range(count):
        origin = rng.choice(live)
        key = f"key-{rng.getrandbits(64):x}"
        procs.append(env.process(origin.lookup(key)))
    env.run(until=simpy.AllOf(env, procs))
    return [p.value for p in procs]


def stretch(net, rec):
    """Route latency / direct latency (None if the lookup had 0 hops)."""
    direct = net.latency(rec.origin, rec.owner)
    if rec.hops == 0 or direct == 0:
        return None
    total = 0
    for i in range(len(rec.path) - 1):
        total += net.latency(rec.path[i], rec.path[i + 1])
    return total / direct


def count_correct(oracle, recs):
    correct = 0
    for r in recs:
        if oracle.check(r):
            correct += 1
    return correct


def part_a(sizes, seeds, n_lookups):
    print(f"A. Static networks, averaged over seeds {seeds} ({n_lookups} lookups each)")
    for topo in [UniformSquare(), Clustered()]:
        print(f"\n   topology: {topo.name}")
        print(f"   {'N':>6} {'tables':>8}{'hops':>7}{'±seed':>7}{'p95':>5}{'log16N':>8}"
              f"{'latency':>9}{'stretch':>9}{'state':>7}{'correct':>9}")
        for n in sizes:
            for mode in ["optimal", "random"]:
                hops = []
                p95 = []
                lat = []
                st = []
                state = []
                correct = 0
                for seed in seeds:
                    env = simpy.Environment()
                    net = Network(env, seed=seed, topology=topo)
                    nodes = create_nodes(net, n)
                    build_static(nodes, mode, seed=seed)
                    recs = lookups(env, nodes, n_lookups, seed, "a")
                    oracle = Oracle(nodes)
                    correct += count_correct(oracle, recs)
                    hops.append(mean([r.hops for r in recs]))
                    p95.append(summarize([r.hops for r in recs])["p95"])
                    lat.append(mean([r.latency for r in recs]))
                    s = []
                    for r in recs:
                        x = stretch(net, r)
                        if x is not None:
                            s.append(x)
                    if len(s) > 0:
                        st.append(mean(s))
                    else:
                        st.append(1.0)
                    state.append(mean([nd.routing_state_size() for nd in nodes]))
                if len(seeds) > 1:
                    spread = statistics.stdev(hops)
                else:
                    spread = 0.0
                print(f"   {n:>6} {mode:>8}{mean(hops):7.2f}{spread:7.2f}"
                      f"{mean(p95):5.1f}{math.log(n, 16):8.2f}"
                      f"{mean(lat):9.1f}{mean(st):9.2f}"
                      f"{mean(state):7.1f}"
                      f"{100 * correct / (n_lookups * len(seeds)):8.1f}%")


def join_all(env, net, nodes, costs):
    """Every node joins through the nearest node that is already in."""
    yield from nodes[0].join(None)
    nodes[0].start_maintenance()
    for i in range(1, len(nodes)):
        node = nodes[i]
        before = net.metrics.count_tag("join")
        yield from node.join(nearest_node(node, nodes[:i]).address)
        costs.append(net.metrics.count_tag("join") - before)
        node.start_maintenance()
        yield env.timeout(100)


def grow(n, seed, refine):
    env = simpy.Environment()
    net = Network(env, seed=seed, topology=Clustered())
    nodes = create_nodes(net, n, join_refine=refine)
    costs = []
    env.run(until=env.process(join_all(env, net, nodes, costs)))
    env.run(until=env.now + 10000)
    return env, net, nodes, costs


def part_b(n, seed):
    print(f"\nB. {n} nodes join one by one through their nearest node "
          f"(clustered topology, seed {seed})")
    # for comparison: perfect static tables with the same nodes
    env = simpy.Environment()
    net = Network(env, seed=seed, topology=Clustered())
    ref_nodes = create_nodes(net, n)
    build_static(ref_nodes, "optimal", seed=seed)
    ref = mean([r.latency for r in lookups(env, ref_nodes, 1000, seed, "b")])
    for refine in [True, False]:
        env, net, nodes, costs = grow(n, seed, refine)
        q = table_quality(nodes)
        recs = lookups(env, nodes, 1000, seed, "b")
        oracle = Oracle(nodes)
        print(f"   join_refine={str(refine):<5}  messages/join mean {mean(costs):5.0f}"
              f" (last 50: {mean(costs[-50:]):4.0f})  "
              f"table complete {100 * q['completeness']:5.1f}%  "
              f"entry latency vs best {q['latency_ratio']:.3f}x  "
              f"leaf errors {len(check_leafsets(nodes))}  "
              f"lookups correct {100 * count_correct(oracle, recs) / len(recs):.0f}%  "
              f"latency {mean([r.latency for r in recs]):.1f} ms")
    print(f"   static optimal tables, same nodes: latency {ref:.1f} ms")


def crash_report(env, nodes, seed, label):
    recs = lookups(env, nodes, 500, seed, label)
    oracle = Oracle(nodes)
    ok = [r for r in recs if r.success]
    print(f"   {label:<26} success {100 * len(ok) / len(recs):5.1f}%  "
          f"correct {100 * count_correct(oracle, recs) / len(recs):5.1f}%  "
          f"latency {mean([r.latency for r in ok]):6.0f} ms")


def part_c(n, seed):
    print(f"\nC. {n}-node static network with maintenance; 25% crash (seed {seed})")
    env = simpy.Environment()
    net = Network(env, seed=seed)
    nodes = create_nodes(net, n)
    build_static(nodes, "optimal", seed=seed)
    for node in nodes:
        node.start_maintenance()
    env.run(until=3000)

    crash_report(env, nodes, seed, "before crash")
    for node in make_rng(seed, "demo-crash").sample(nodes, n // 4):
        node.fail()
    crash_t = env.now
    crash_report(env, nodes, seed, "immediately after crash")
    live = set(nd.address for nd in nodes if nd.alive)
    print(f"   {'time after crash':>18} {'leaf-set errors':>16} {'dead in M':>10} {'dead in tables':>15}")
    for t in [10000, 30000, 60000, 120000, 240000]:
        env.run(until=crash_t + t)
        dead_m = 0
        dead_t = 0
        for nd in nodes:
            if not nd.alive:
                continue
            for r in nd.neighborhood:
                if r.addr not in live:
                    dead_m += 1
            for e in nd.table_entries():
                if e.addr not in live:
                    dead_t += 1
        print(f"   {t // 1000:>16} s {len(check_leafsets(nodes)):>16} {dead_m:>10} {dead_t:>15}")
    crash_report(env, nodes, seed, "after 240 s")


def main():
    parser = argparse.ArgumentParser(description="Phase 3 demo: Pastry")
    parser.add_argument("--sizes", type=int, nargs="+", default=[16, 256, 1024, 4096])
    parser.add_argument("--seeds", type=int, nargs="+", default=[1, 2, 3])
    parser.add_argument("--lookups", type=int, default=1000)
    parser.add_argument("--dynamic-n", type=int, default=300)
    args = parser.parse_args()
    part_a(args.sizes, args.seeds, args.lookups)
    part_b(args.dynamic_n, args.seeds[0])
    part_c(args.dynamic_n, args.seeds[0])


if __name__ == "__main__":
    main()
