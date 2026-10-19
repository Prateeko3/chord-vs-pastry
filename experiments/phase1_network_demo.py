"""Phase 1 demo: ping random pairs of nodes on the three topologies.

For each topology we make N nodes, ping random pairs and print the round trip
times (RTT). On the uniform map the median should be around 2 x 52 ms. On the
clustered map p5 is very small (same cluster) and p95 is big (different
clusters). On the homogeneous map every RTT is the same.
At the end we run the pings again with 10% message loss to see the timeouts.

Run:
    python -m experiments.phase1_network_demo
    python -m experiments.phase1_network_demo --nodes 500 --pings 5000 --seed 7
"""

import argparse

import simpy

from sim.metrics import percentile, summarize
from sim.network import Network
from sim.node import BaseNode, RpcTimeout
from sim.rng import make_rng
from sim.topology import Clustered, Homogeneous, UniformSquare


class PingNode(BaseNode):
    def handle_ping(self, msg):
        return "pong"


def one_ping(env, a, b, rtts, counts):
    start = env.now
    try:
        yield from a.rpc(b.address, "ping", timeout=500)
        rtts.append(env.now - start)
    except RpcTimeout:
        counts["timeouts"] += 1


def measure(topology, n_nodes, n_pings, seed, loss_rate=0.0):
    env = simpy.Environment()
    net = Network(env, seed=seed, topology=topology, loss_rate=loss_rate)
    nodes = []
    for i in range(n_nodes):
        nodes.append(net.create_node(PingNode))
    pick = make_rng(seed, "demo-workload")
    rtts = []
    counts = {"timeouts": 0}
    for i in range(n_pings):
        a, b = pick.sample(nodes, 2)   # two different nodes
        env.process(one_ping(env, a, b, rtts, counts))
    env.run()
    rtt = summarize(rtts)
    rtt["p5"] = percentile(rtts, 5)
    return {"rtt": rtt, "timeouts": counts["timeouts"], "metrics": net.metrics}


def main():
    parser = argparse.ArgumentParser(description="Phase 1 demo: ping times on the three topologies")
    parser.add_argument("--nodes", type=int, default=200)
    parser.add_argument("--pings", type=int, default=2000)
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()

    topologies = [UniformSquare(), Clustered(), Homogeneous()]
    print("Ping RTTs between random node pairs (N=%d, %d pings, seed=%d); "
          "all times in simulated ms\n" % (args.nodes, args.pings, args.seed))
    print("%-12s%8s%8s%8s%8s%8s" % ("topology", "mean", "p5", "p50", "p95", "max"))
    for topo in topologies:
        r = measure(topo, args.nodes, args.pings, args.seed)["rtt"]
        print("%-12s%8.1f%8.1f%8.1f%8.1f%8.1f" % (topo.name, r["mean"], r["p5"], r["p50"],
                                                   r["p95"], r["max"]))

    print("\nSame pings on the uniform topology with 10% message loss:")
    r = measure(UniformSquare(), args.nodes, args.pings, args.seed, loss_rate=0.10)
    m = r["metrics"]
    print("  messages sent %d, lost %d, RPC timeouts %d of %d pings "
          "(%.1f%%; theory 1 - 0.9^2 = 19%%)" % (m.total_sent(), m.lost, r["timeouts"], args.pings,
                                                100 * r["timeouts"] / args.pings))


if __name__ == "__main__":
    main()
