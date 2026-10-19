# Chord vs. Pastry: Comparative Performance Analysis of Structured P2P Overlay Routing

A university Distributed Systems project. Two classic distributed hash table
(DHT) overlays are implemented **from scratch**, following the original papers:

- **Chord**: I. Stoica, R. Morris, D. Karger, M. F. Kaashoek, H. Balakrishnan.
  *Chord: A Scalable Peer-to-peer Lookup Service for Internet Applications.*
  SIGCOMM 2001 (extended version in IEEE/ACM ToN, 2003).
- **Pastry**: A. Rowstron, P. Druschel. *Pastry: Scalable, decentralized
  object location and routing for large-scale peer-to-peer systems.*
  Middleware 2001.

Both overlays run inside a single-machine **discrete-event simulation**
(`simpy`) with a 2-D latency model. A key-value store sits on top of each one,
and the experiments compare hops, latency, state size, maintenance cost, load
balance and robustness under churn.

> **Hypothesis.** Pastry's proximity-aware routing gives lower lookup
> *latency* on heterogeneous networks, while Chord has simpler maintenance.

## Project layout

```
chord-vs-pastry/
├── sim/            simulation core (Phase 0-1)
│   ├── rng.py      seeded, named random streams (reproducibility)
│   ├── ids.py      SHA-1 → identifier hashing, ring-interval helper
│   ├── topology.py node placement + latency: uniform / clustered / homogeneous
│   ├── network.py  Message + Network: delivery after latency, jitter, loss
│   ├── node.py     BaseNode: send, rpc (with timeout), handlers, periodic tasks
│   ├── metrics.py  shared metrics collector (messages, lookups, distributions)
│   └── overlay.py  recursive routing shared by both overlays (route/ack/timeout/lookup)
├── chord/          Chord overlay (Phase 2)
│   ├── node.py     ChordNode: fingers, successor list, lookup, join, stabilize, leave
│   └── ring.py     test/experiment helpers: unique IDs, static builder, oracle
├── pastry/         Pastry overlay (Phase 3)
│   ├── node.py     PastryNode: routing table, leaf set, neighbourhood set, join, repair
│   └── ring.py     test/experiment helpers: static builder (optimal/random), oracle
├── app/            key-value store on either overlay (Phase 4)
├── experiments/    parameter sweeps → CSV + PNG     (Phase 5)
│   └── results/    generated CSV tables and PNG plots (committed, so the team can study them)
├── tests/          pytest correctness tests
└── docs/
    └── LEARNING_GUIDE.md   explanations, glossary, work log, viva questions
```

## Setup

Requires **Python 3.11 or newer**. On macOS with Homebrew, use
`python3.14` (or any working 3.11+). See the note in the learning guide about
the system `python3` (3.9) and a broken Homebrew 3.12 build on the development
machine.

```bash
cd ~/chord-vs-pastry
python3.14 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Run

```bash
source .venv/bin/activate
python -m pytest                                    # all tests
python -m experiments.phase0_consistent_hashing_demo   # Phase 0 demo
python -m experiments.phase1_network_demo              # Phase 1 demo
python -m experiments.phase2_chord_demo                # Phase 2 demo
python -m experiments.phase3_pastry_demo               # Phase 3 demo
```

## Reproducibility

Every random choice comes from `sim.rng.make_rng(seed, "<stream-name>")`.
The same seed always gives identical results. Nothing uses the global `random`
module.

## Status

See the work log at the top of [docs/LEARNING_GUIDE.md](docs/LEARNING_GUIDE.md).
# chord-vs-pastry
