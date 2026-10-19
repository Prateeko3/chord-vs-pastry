"""Where nodes sit in "physical" space, and how long a message takes between them.

Every simulated node gets a 2-D coordinate (x, y). The one-way latency of a
message between two nodes is their straight-line (Euclidean) distance, and
1 unit of distance = 1 millisecond. Real Internet latency is not exactly
geometric, but this "Euclidean plane" model is the same one Rowstron &
Druschel used to evaluate Pastry, and it gives us what we need: some pairs of
nodes are close (cheap to talk to) and some are far (expensive).

Three layouts ("topologies") are provided:

  UniformSquare  nodes scattered evenly over a square. Latencies vary
                 smoothly from ~0 to the diagonal of the square.
  Clustered      nodes grouped into a few tight clusters ("continents").
                 Inside a cluster messages are fast; between clusters they
                 are slow. This is the *heterogeneous* network where
                 Pastry's proximity awareness should help most.
  Homogeneous    every pair of nodes has the same latency. Proximity gives
                 no advantage here, so it is our *control* experiment.

A topology object is a *specification*. Anything random about the layout
itself (e.g. where the cluster centres are) is fixed by `bind(rng)`, which
the Network calls once with a seeded generator. This keeps a topology object
safe to reuse across many experiments with different seeds.
"""

from __future__ import annotations

import math
import random

import numpy as np

Position = tuple[float, float]


def euclidean(p: Position, q: Position) -> float:
    """Straight-line distance between two points (Pythagoras)."""
    return math.hypot(p[0] - q[0], p[1] - q[1])


class Topology:
    """Base class: how to place a node, and the latency between two places."""

    name = "base"

    def bind(self, rng: random.Random) -> "Topology":
        """Return a copy with any random layout decisions fixed.
        Topologies with no layout-level randomness just return themselves."""
        return self

    def sample_position(self, rng: random.Random) -> Position:
        raise NotImplementedError

    def latency(self, p: Position, q: Position) -> float:
        """One-way latency in ms between positions p and q (before jitter)."""
        return euclidean(p, q)

    def latencies_from(self, p: Position, xs: np.ndarray, ys: np.ndarray) -> np.ndarray:
        """Latency from p to many positions at once (numpy, for speed).
        Only used by experiment/test helpers that build large networks."""
        return np.hypot(xs - p[0], ys - p[1])


class UniformSquare(Topology):
    """Nodes placed uniformly at random in a `side` x `side` square.

    With the default side of 100, the mean distance between two random
    points is about 52 ms and the maximum (corner to corner) is about 141 ms.
    """

    name = "uniform"

    def __init__(self, side: float = 100.0):
        if side <= 0:
            raise ValueError("side must be positive")
        self.side = side

    def sample_position(self, rng: random.Random) -> Position:
        return (rng.uniform(0, self.side), rng.uniform(0, self.side))

    def __repr__(self) -> str:
        return f"UniformSquare(side={self.side})"


class Clustered(Topology):
    """Nodes grouped around `n_clusters` centres ("continents").

    Each node picks a cluster uniformly at random, then lands at
    centre + Gaussian noise with standard deviation `spread` on each axis.
    Cluster centres are drawn uniformly in a `side` x `side` square when the
    topology is bound to a seed (or you can pass `centers` explicitly).

    With the defaults (5 clusters, side 100, spread 5) two nodes in the same
    cluster are ~9 ms apart on average (spread * sqrt(pi)), while nodes in
    different clusters are as far apart as their centres, ~52 ms on average.
    Because the centres are random, the overall mean latency varies quite a
    lot from seed to seed, so always compare overlays on the *same* seeds.
    """

    name = "clustered"

    def __init__(self, n_clusters: int = 5, side: float = 100.0, spread: float = 5.0,
                 centers: list[Position] | None = None):
        if n_clusters < 1:
            raise ValueError("need at least one cluster")
        if centers is not None and len(centers) != n_clusters:
            raise ValueError("len(centers) must equal n_clusters")
        self.n_clusters = n_clusters
        self.side = side
        self.spread = spread
        self.centers = list(centers) if centers is not None else None

    def bind(self, rng: random.Random) -> "Clustered":
        if self.centers is not None:
            return self  # layout already fixed by the caller
        centers = [(rng.uniform(0, self.side), rng.uniform(0, self.side))
                   for _ in range(self.n_clusters)]
        return Clustered(self.n_clusters, self.side, self.spread, centers)

    def sample_position(self, rng: random.Random) -> Position:
        if self.centers is None:
            raise RuntimeError("Clustered topology must be bound to a seed first "
                               "(the Network does this for you)")
        cx, cy = self.centers[rng.randrange(self.n_clusters)]
        return (rng.gauss(cx, self.spread), rng.gauss(cy, self.spread))

    def __repr__(self) -> str:
        return (f"Clustered(n_clusters={self.n_clusters}, side={self.side}, "
                f"spread={self.spread})")


class Homogeneous(Topology):
    """Every pair of distinct nodes is exactly `latency_ms` apart.

    Positions are still sampled (uniformly) so that plots look the same, but
    they are ignored when computing latency. Use this as the control: if
    Pastry beats Chord on latency here, it can only be because of fewer hops,
    not because of proximity-aware routing.
    """

    name = "homogeneous"

    def __init__(self, latency_ms: float = 50.0, side: float = 100.0):
        if latency_ms < 0:
            raise ValueError("latency must be non-negative")
        self.latency_ms = latency_ms
        self.side = side

    def sample_position(self, rng: random.Random) -> Position:
        return (rng.uniform(0, self.side), rng.uniform(0, self.side))

    def latency(self, p: Position, q: Position) -> float:
        return self.latency_ms

    def latencies_from(self, p: Position, xs: np.ndarray, ys: np.ndarray) -> np.ndarray:
        return np.full(len(xs), self.latency_ms)

    def __repr__(self) -> str:
        return f"Homogeneous(latency_ms={self.latency_ms})"
