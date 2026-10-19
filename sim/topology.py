"""Node positions and latency between nodes.

Every node gets an (x, y) position. Latency of a message = straight line
distance between the two nodes, and 1 unit = 1 ms. The Pastry paper also used
this kind of "Euclidean plane" model in its experiments.

Three topologies:
  UniformSquare - nodes spread evenly in a square
  Clustered     - nodes in a few groups ("continents"), close inside a group
                  and far between groups. Pastry should gain most here.
  Homogeneous   - all latencies are equal, so picking nearby nodes can't help.
                  We use it as a control case.
"""

import math

import numpy as np


def euclidean(p, q):
    """Distance between two points."""
    return math.hypot(p[0] - q[0], p[1] - q[1])


class Topology:
    """Base class for the topologies."""

    name = "base"

    def setup(self, rng):
        # Topologies that have random layout (Clustered) override this.
        # It is called once by the Network with a seeded rng.
        return self

    def sample_position(self, rng):
        raise NotImplementedError

    def latency(self, p, q):
        """One-way latency in ms between positions p and q."""
        return euclidean(p, q)

    def latencies_from(self, p, xs, ys):
        # same as latency() but for many points at once using numpy,
        # only used to build big test networks quickly
        return np.hypot(xs - p[0], ys - p[1])


class UniformSquare(Topology):
    """Nodes placed randomly in a side x side square.
    With side = 100 the average distance is about 52 ms."""

    name = "uniform"

    def __init__(self, side=100.0):
        if side <= 0:
            raise ValueError("side must be positive")
        self.side = side

    def sample_position(self, rng):
        return (rng.uniform(0, self.side), rng.uniform(0, self.side))

    def __repr__(self):
        return "UniformSquare(side=" + str(self.side) + ")"


class Clustered(Topology):
    """Nodes grouped around n_clusters centres.

    A node picks a random cluster and its position is the centre plus some
    gaussian noise (spread). The centres are chosen randomly in setup(), so
    different seeds give different maps. Inside a cluster nodes are ~9 ms
    apart, between clusters it is about 52 ms on average.
    """

    name = "clustered"

    def __init__(self, n_clusters=5, side=100.0, spread=5.0, centers=None):
        if n_clusters < 1:
            raise ValueError("need at least one cluster")
        if centers is not None and len(centers) != n_clusters:
            raise ValueError("len(centers) must equal n_clusters")
        self.n_clusters = n_clusters
        self.side = side
        self.spread = spread
        if centers is None:
            self.centers = None
        else:
            self.centers = list(centers)

    def setup(self, rng):
        if self.centers is not None:
            return self
        # return a new object so the original one can be reused with other seeds
        centers = []
        for i in range(self.n_clusters):
            centers.append((rng.uniform(0, self.side), rng.uniform(0, self.side)))
        return Clustered(self.n_clusters, self.side, self.spread, centers)

    def sample_position(self, rng):
        if self.centers is None:
            raise RuntimeError("call setup() first (the Network does this)")
        cx, cy = self.centers[rng.randrange(self.n_clusters)]
        return (rng.gauss(cx, self.spread), rng.gauss(cy, self.spread))

    def __repr__(self):
        return "Clustered(n_clusters=%d, side=%s, spread=%s)" % (
            self.n_clusters, self.side, self.spread)


class Homogeneous(Topology):
    """Every pair of nodes has the same latency (latency_ms).
    Positions are still made so plots look normal, but they are not used."""

    name = "homogeneous"

    def __init__(self, latency_ms=50.0, side=100.0):
        if latency_ms < 0:
            raise ValueError("latency must be non-negative")
        self.latency_ms = latency_ms
        self.side = side

    def sample_position(self, rng):
        return (rng.uniform(0, self.side), rng.uniform(0, self.side))

    def latency(self, p, q):
        return self.latency_ms

    def latencies_from(self, p, xs, ys):
        return np.full(len(xs), self.latency_ms)

    def __repr__(self):
        return "Homogeneous(latency_ms=" + str(self.latency_ms) + ")"
