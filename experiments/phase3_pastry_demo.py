"""Phase 3 demo: Pastry on its own, compared with the paper's predictions.

Part A  Static networks of growing size, with routing tables filled
        "optimal" (physically closest node per slot) or "random" (locality
        off): hops vs log16(N), latency, route stretch, state size.
Part B  A network built by real joins (each through its nearest node):
        join cost with/without the refinement step, table quality.
Part C  25% of nodes crash: lookups right after, and how fast each part
        of the state is cleaned up.

Route stretch = (latency along the route) / (direct latency origin->owner),
the paper's "relative distance". 1.0 would mean routing costs nothing extra.

Run:
    python -m experiments.phase3_pastry_demo
    python -m experiments.phase3_pastry_demo --seeds 1 2 3 4 5
"""

from __future__ import annotations

import argparse
import math
import statistics

import simpy

from pastry.ring import (Oracle, build_static, check_leafsets, create_nodes, nearest_node,
                         table_quality)
from sim.metrics import summarize
from sim.network import Network
from sim.rng import make_rng
from sim.topology import Clustered, UniformSquare


def lookups(env, nodes, count, seed, label):
    rng = make_rng(seed, f"demo-lookups-{label}")
    live = [n for n in nodes if n.alive]
    procs = [env.process(rng.choice(live).lookup(f"key-{rng.getrandbits(64):x}"))
             for _ in range(count)]
    env.run(until=simpy.AllOf(env, procs))
    return [p.value for p in procs]


def stretch(net, rec):
    """Forward route latency / direct latency (None for 0-hop lookups)."""
    direct = net.latency(rec.origin, rec.owner)
    if rec.hops == 0 or direct == 0:
        return None
    p = rec.path
    return sum(net.latency(a, b) for a, b in zip(p, p[1:])) / direct


def part_a(sizes, seeds, n_lookups):
    print(f"A. Static networks, averaged over seeds {seeds} ({n_lookups} lookups each)")
    for topo in (UniformSquare(), Clustered()):
        print(f"\n   topology: {topo.name}")
        print(f"   {'N':>6} {'tables':>8}{'hops':>7}{'±seed':>7}{'p95':>5}{'log16N':>8}"
              f"{'latency':>9}{'stretch':>9}{'state':>7}{'correct':>9}")
        for n in sizes:
            for mode in ("optimal", "random"):
                hops, p95, lat, st, state, correct = [], [], [], [], [], 0
                for seed in seeds:
                    env = simpy.Environment()
                    net = Network(env, seed=seed, topology=topo)
                    nodes = create_nodes(net, n)
                    build_static(nodes, mode, seed=seed)
                    recs = lookups(env, nodes, n_lookups, seed, "a")
                    oracle = Oracle(nodes)
                    correct += sum(oracle.check(r) for r in recs)
                    hops.append(statistics.fmean(r.hops for r in recs))
                    p95.append(summarize(r.hops for r in recs)["p95"])
                    lat.append(statistics.fmean(r.latency for r in recs))
                    s = [x for x in (stretch(net, r) for r in recs) if x is not None]
                    st.append(statistics.fmean(s) if s else 1.0)
                    state.append(statistics.fmean(nd.routing_state_size() for nd in nodes))
                spread = statistics.stdev(hops) if len(seeds) > 1 else 0.0
                print(f"   {n:>6} {mode:>8}{statistics.fmean(hops):7.2f}{spread:7.2f}"
                      f"{statistics.fmean(p95):5.1f}{math.log(n, 16):8.2f}"
                      f"{statistics.fmean(lat):9.1f}{statistics.fmean(st):9.2f}"
                      f"{statistics.fmean(state):7.1f}"
                      f"{100 * correct / (n_lookups * len(seeds)):8.1f}%")


def grow(n, seed, refine):
    env = simpy.Environment()
    net = Network(env, seed=seed, topology=Clustered())
    nodes = create_nodes(net, n, join_refine=refine)
    costs = []

    def go():
        yield from nodes[0].join(None)
        nodes[0].start_maintenance()
        for i, node in enumerate(nodes[1:], start=1):
            before = net.metrics.sent_by_tag["join"]
            yield from node.join(nearest_node(node, nodes[:i]).address)
            costs.append(net.metrics.sent_by_tag["join"] - before)
            node.start_maintenance()
            yield env.timeout(100)

    env.run(until=env.process(go()))
    env.run(until=env.now + 10_000)
    return env, net, nodes, costs


def part_b(n, seed):
    print(f"\nB. {n} nodes join one by one through their nearest node "
          f"(clustered topology, seed {seed})")
    # Reference: a perfect static build on exactly the same positions and IDs.
    env = simpy.Environment()
    net = Network(env, seed=seed, topology=Clustered())
    ref_nodes = create_nodes(net, n)
    build_static(ref_nodes, "optimal", seed=seed)
    ref = statistics.fmean(r.latency for r in lookups(env, ref_nodes, 1000, seed, "b"))
    for refine in (True, False):
        env, net, nodes, costs = grow(n, seed, refine)
        q = table_quality(nodes)
        recs = lookups(env, nodes, 1000, seed, "b")
        oracle = Oracle(nodes)
        print(f"   join_refine={str(refine):<5}  messages/join mean {statistics.fmean(costs):5.0f}"
              f" (last 50: {statistics.fmean(costs[-50:]):4.0f})  "
              f"table complete {100 * q['completeness']:5.1f}%  "
              f"entry latency vs best {q['latency_ratio']:.3f}x  "
              f"leaf errors {len(check_leafsets(nodes))}  "
              f"lookups correct {100 * sum(oracle.check(r) for r in recs) / len(recs):.0f}%  "
              f"latency {statistics.fmean(r.latency for r in recs):.1f} ms")
    print(f"   static optimal tables, same nodes: latency {ref:.1f} ms")


def part_c(n, seed):
    print(f"\nC. {n}-node static network with maintenance; 25% crash (seed {seed})")
    env = simpy.Environment()
    net = Network(env, seed=seed)
    nodes = create_nodes(net, n)
    build_static(nodes, "optimal", seed=seed)
    for node in nodes:
        node.start_maintenance()
    env.run(until=3000)

    def report(label):
        recs = lookups(env, nodes, 500, seed, label)
        oracle = Oracle(nodes)
        ok = [r for r in recs if r.success]
        print(f"   {label:<26} success {100 * len(ok) / len(recs):5.1f}%  "
              f"correct {100 * sum(oracle.check(r) for r in recs) / len(recs):5.1f}%  "
              f"latency {statistics.fmean(r.latency for r in ok):6.0f} ms")

    report("before crash")
    for node in make_rng(seed, "demo-crash").sample(nodes, n // 4):
        node.fail()
    crash_t = env.now
    report("immediately after crash")
    live = {nd.address for nd in nodes if nd.alive}
    print(f"   {'time after crash':>18} {'leaf-set errors':>16} {'dead in M':>10} {'dead in tables':>15}")
    for t in (10_000, 30_000, 60_000, 120_000, 240_000):
        env.run(until=crash_t + t)
        dead_m = sum(r.addr not in live for nd in nodes if nd.alive for r in nd.neighborhood)
        dead_t = sum(e.addr not in live for nd in nodes if nd.alive for e in nd.table_entries())
        print(f"   {t // 1000:>16} s {len(check_leafsets(nodes)):>16} {dead_m:>10} {dead_t:>15}")
    report("after 240 s")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--sizes", type=int, nargs="+", default=[16, 256, 1024, 4096])
    p.add_argument("--seeds", type=int, nargs="+", default=[1, 2, 3])
    p.add_argument("--lookups", type=int, default=1000)
    p.add_argument("--dynamic-n", type=int, default=300)
    a = p.parse_args()
    part_a(a.sizes, a.seeds, a.lookups)
    part_b(a.dynamic_n, a.seeds[0])
    part_c(a.dynamic_n, a.seeds[0])


if __name__ == "__main__":
    main()
