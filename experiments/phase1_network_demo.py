"""Phase 1 demo: what do the three network topologies "feel like"?

For each topology we create N nodes, then pick random pairs and measure the
round-trip time (RTT) of a real simulated RPC ("ping"). This exercises the
whole Phase 1 stack: topology -> network -> node RPC -> metrics.

What to look for:
  uniform      RTTs spread smoothly; median about 2 x 52 ms.
  clustered    two very different kinds of pair. Same-cluster pings are
               tiny (p5 is small) and cross-cluster pings are large. That
               gap is what Pastry's proximity-aware routing should exploit.
  homogeneous  every RTT is identical, so there is nothing to exploit.
Then the same pings with 10% message loss, showing how RPC timeouts
surface lost requests and lost replies.

Run:
    python -m experiments.phase1_network_demo
    python -m experiments.phase1_network_demo --nodes 500 --pings 5000 --seed 7
"""

from __future__ import annotations

import argparse

import simpy

from sim.metrics import percentile, summarize
from sim.network import Network
from sim.node import BaseNode, RpcTimeout
from sim.rng import make_rng
from sim.topology import Clustered, Homogeneous, Topology, UniformSquare


class PingNode(BaseNode):
    def handle_ping(self, msg):
        return "pong"


def measure(topology: Topology, n_nodes: int, n_pings: int, seed: int,
            loss_rate: float = 0.0) -> dict:
    env = simpy.Environment()
    net = Network(env, seed=seed, topology=topology, loss_rate=loss_rate)
    nodes = [net.create_node(PingNode) for _ in range(n_nodes)]
    pick = make_rng(seed, "demo-workload")
    rtts: list[float] = []
    timeouts = 0

    def one_ping(a: PingNode, b: PingNode):
        nonlocal timeouts
        start = env.now
        try:
            yield from a.rpc(b.address, "ping", timeout=500)
            rtts.append(env.now - start)
        except RpcTimeout:
            timeouts += 1

    for _ in range(n_pings):
        a, b = pick.sample(nodes, 2)   # two different nodes
        env.process(one_ping(a, b))
    env.run()
    rtt = summarize(rtts)
    rtt["p5"] = percentile(rtts, 5)   # how fast the *closest* pairs are
    return {"rtt": rtt, "timeouts": timeouts, "metrics": net.metrics}


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--nodes", type=int, default=200)
    p.add_argument("--pings", type=int, default=2000)
    p.add_argument("--seed", type=int, default=42)
    a = p.parse_args()

    topologies = [UniformSquare(), Clustered(), Homogeneous()]
    print(f"Ping RTTs between random node pairs (N={a.nodes}, {a.pings} pings, "
          f"seed={a.seed}); all times in simulated ms\n")
    print(f"{'topology':<12}{'mean':>8}{'p5':>8}{'p50':>8}{'p95':>8}{'max':>8}")
    for topo in topologies:
        r = measure(topo, a.nodes, a.pings, a.seed)["rtt"]
        print(f"{topo.name:<12}{r['mean']:8.1f}{percentile_row(r)}")

    print("\nSame pings on the uniform topology with 10% message loss:")
    r = measure(UniformSquare(), a.nodes, a.pings, a.seed, loss_rate=0.10)
    m = r["metrics"]
    print(f"  messages sent {m.total_sent}, lost {m.lost}, "
          f"RPC timeouts {r['timeouts']} of {a.pings} pings "
          f"({100 * r['timeouts'] / a.pings:.1f}%; theory 1 - 0.9^2 = 19%)")


def percentile_row(r: dict) -> str:
    return "".join(f"{r[k]:8.1f}" for k in ("p5", "p50", "p95", "max"))


if __name__ == "__main__":
    main()
