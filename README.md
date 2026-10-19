# Chord vs Pastry

Distributed Systems course project (Monsoon 2026, Option 1).

Comparative performance analysis of structured P2P overlay routing. We
implemented two DHT algorithms from their original papers and compare them:

- **Chord**: Stoica, Morris, Karger, Kaashoek, Balakrishnan. "Chord: A Scalable
  Peer-to-peer Lookup Service for Internet Applications", SIGCOMM 2001
  (longer version in IEEE/ACM Transactions on Networking, 2003).
- **Pastry**: Rowstron, Druschel. "Pastry: Scalable, decentralized object
  location and routing for large-scale peer-to-peer systems", Middleware 2001.

Everything runs on one machine as a simulation using `simpy`. Every node has
a position on a 2D map and the message delay is the distance between the
nodes, so we can see if Pastry's "pick nearby nodes" idea really helps.

What we want to check: Pastry should have lower lookup latency when the
network has very different link delays, and Chord should be simpler to
maintain.

## Folders

```
sim/          simulator: network, nodes, metrics, topologies
  rng.py        random generators from one seed
  ids.py        SHA-1 hashing and the ring interval check
  topology.py   node positions and latency (uniform / clustered / homogeneous)
  network.py    sends messages with delay, jitter and loss
  node.py       base node: send, rpc with timeout, periodic tasks
  metrics.py    counts messages, hops, latency (same for both overlays)
  overlay.py    routing code used by both Chord and Pastry
chord/        Chord (node.py) + helper functions for tests (ring.py)
pastry/       Pastry (node.py) + helper functions for tests (ring.py)
app/          key-value store (phase 4, not done yet)
experiments/  demo scripts and the graphs
  results/      csv files and graphs
tests/        pytest tests
docs/         LEARNING_GUIDE.md (our notes for every phase)
```

## Setup

You need Python 3.11 or newer.

```bash
cd chord-vs-pastry
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## How to run

```bash
source .venv/bin/activate
python -m pytest                                      # all tests (116)
python -m experiments.phase0_consistent_hashing_demo  # modulo vs consistent hashing
python -m experiments.phase1_network_demo             # ping times on the 3 topologies
python -m experiments.phase2_chord_demo               # Chord results
python -m experiments.phase3_pastry_demo              # Pastry results
python -m experiments.midsem_graphs                   # graphs in experiments/results/midsem/
```

All random numbers come from one seed, so running a script again gives the
same numbers.

## Status

Done: simulator, Chord, Pastry, tests, first graphs.
Next: key-value store and churn, full experiments, report.
More details are in [docs/LEARNING_GUIDE.md](docs/LEARNING_GUIDE.md).
