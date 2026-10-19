# Learning Guide: Chord vs. Pastry

This guide grows with the project. Each phase adds a section with four levels
(Beginner → Intermediate → Advanced → How to run), plus likely viva questions.
New terms go into the Glossary at the bottom.

---

## Work log

| Phase | Status | Date | Files added | After this phase I should be able to explain… |
|---|---|---|---|---|
| 0 – Setup & foundations | ✅ Done | 2026-10-12 | `requirements.txt`, `README.md`, `pytest.ini`, `.gitignore`, `sim/rng.py`, `sim/ids.py`, `experiments/phase0_consistent_hashing_demo.py`, `tests/test_phase0_foundations.py`, package `__init__.py` files, this guide | What a DHT is; why we hash nodes and keys into one ID space; why consistent hashing beats `hash % N`; why O(log N) hops and state matter; how seeded randomness makes experiments reproducible |
| 1 – Simulation core | ✅ Done | 2026-10-12 | `sim/topology.py`, `sim/network.py`, `sim/node.py`, `sim/metrics.py`, `experiments/phase1_network_demo.py`, `tests/test_phase1_sim_core.py` | How a discrete-event simulation works; what `yield` means in simpy; how a message's delay is computed; how RPC + timeout detects failures; why we have three topologies; what the metrics collector counts and why it is shared |
| 2 – Chord | ✅ Done | 2026-10-12 | `chord/node.py`, `chord/ring.py`, `experiments/phase2_chord_demo.py`, `tests/test_phase2_chord.py` | How Chord assigns keys (successor on the ring); finger tables and why a lookup takes ≈ ½·log₂N hops; recursive routing with acks; join, stabilize/notify, fix_fingers, check_predecessor; why correctness needs only successor pointers; how the successor list survives crashes; join cost O(log²N); every difference from the paper |
| 3 – Pastry | ✅ Done | 2026-10-12 | `sim/overlay.py`, `pastry/node.py`, `pastry/ring.py`, `experiments/phase3_pastry_demo.py`, `tests/test_phase3_pastry.py` (Chord refactored onto `sim/overlay.py`; `sim/topology.py` gained `latencies_from`) | Prefix routing and why it takes ≈ log₁₆N hops; leaf set vs routing table vs neighbourhood set; the rare case; proximity neighbour selection and route stretch; the join protocol; lazy repair; the stale-gossip problem and the ping-before-insert fix; every simplification from the paper |
| 4 – KV store & churn | ⏳ Next | | | |
| 5 – Experiments | ⬜ | | | |
| 6 – Analysis & write-up | ⬜ | | | |

---

## Phase 0: Setup and foundations

### 1. Beginner (no jargon)

**The problem.** Imagine a giant library with millions of books, but there is
no central building. The books are spread across thousands of people's homes,
and people keep moving in and out of town. You want a book called *"Dune"*.
Whose house is it in, and how do you find out without phoning everyone?

**Idea 1: one central catalogue.** One person keeps a list of where every book
is. That works until they get sick (a *single point of failure*) or get a
million calls an hour (a *bottleneck*). We want no "boss".

**Idea 2: a rule everyone can compute.** Give every house a number and every
book a number, using the same recipe for both. Then agree on one rule:
*"a book lives in the first house whose number is equal to or just after the
book's number, going round a circle."* Now anyone can work out where *Dune*
belongs without asking a central catalogue. That recipe-plus-rule is a
**Distributed Hash Table (DHT)**.

**Why a circle?** Picture a clock face with numbered houses placed around it.
When a new family moves in, they sit between two neighbours and take over
only the books between their neighbour and themselves. Nobody else is
affected. This is **consistent hashing**. Compare the naive rule *"book number
divided by number of houses, take the remainder"*. Add one house and almost
every book's remainder changes, so nearly the whole library has to move.
Our demo measures this: with 10 houses, adding one moves about 91% of books
with the naive rule, but only about 9% with the circle.

**Finding the right house quickly.** You know where the book *should* be, but
you don't know everyone's address. You only know a few people. If you could
only ask your next-door neighbour, finding a house on the other side of a city
of 1,000,000 would take up to a million steps. Chord and Pastry give each house
a small, cleverly chosen address book. Some entries are nearby, some halfway
round the circle, some a quarter of the way, and so on. Each question you ask
gets you *at least halfway closer*, so even with a million houses you need
about 20 steps. That "halving" is what **O(log N)** means.

**Chord vs. Pastry in one sentence each.**
- **Chord**: "Each step, jump as far round the clock as you can without
  overshooting." Simple and elegant, but ignores whether the next house is
  across the street or across the ocean.
- **Pastry**: "Each step, find someone whose number shares one more leading
  digit with the book's number, and among candidates prefer the one who is
  physically close." A bit more complex, but it should take fewer and shorter
  trips.

That difference is what this project measures.

### 2. Intermediate: how the project is organised

| Path | What it is | Built in |
|---|---|---|
| `requirements.txt` | Exact library versions, so any machine gets the same setup | 0 |
| `pytest.ini` | Tells `pytest` to look in `tests/` | 0 |
| `sim/rng.py` | `make_rng(seed, stream)` returns an independent random generator per component (`"network"`, `"churn"`, …), all derived from one master seed | 0 |
| `sim/ids.py` | `hash_to_id(data, bits)` gives the top *bits* bits of SHA-1. `in_interval(x, a, b)` is the "is x between a and b *clockwise* on the ring?" test that Chord uses everywhere | 0 |
| `experiments/phase0_consistent_hashing_demo.py` | Measures how many keys move when one server is added: modulo vs. ring | 0 |
| `tests/test_phase0_foundations.py` | 21 tests: dependencies import, seeding is reproducible, SHA-1 matches the official test vector, ring intervals wrap correctly, consistent hashing moves about 1/(N+1) of keys | 0 |
| `sim/network.py`, `sim/metrics.py`, node base | Simulated network and shared measurements | 1 |
| `chord/`, `pastry/` | The two overlays, same interface: `join`, `leave`, `fail`, `lookup(key)` | 2, 3 |
| `app/kvstore.py` | `put`/`get` on either overlay | 4 |
| `experiments/` | Parameter sweeps → CSV + PNG | 5 |

**Design decision: one shared interface.** Experiment code will never write
`if chord: … else: …`. It calls `node.lookup(key)` and the shared metrics
collector records hops and latency the same way for both. This keeps the
comparison fair: any difference in results comes from the algorithm, not
from different measurement code.

**Design decision: named random streams.** If the network jitter and the
churn generator shared one random generator, adding one extra jitter draw
would shift *every* later churn event. Then "with jitter" and "without jitter"
runs would differ in two ways at once. Separate streams prevent that.

### 3. Advanced: concepts in detail

#### 3.1 What a DHT provides

A DHT offers one primitive: **`lookup(key) → node responsible for key`**.
Everything else (put/get storage, replication) is built on top of it. Both
nodes and keys are mapped into a single identifier space `[0, 2^m)` by a hash
function `h`. A deterministic rule, `owner(k)`, picks the responsible node:

- **Chord:** `owner(k) = successor(k)`, the first node ID ≥ k clockwise on the
  ring mod 2^m.
- **Pastry:** `owner(k)` = the node whose ID is *numerically closest* to k
  (128-bit IDs, read as base-2^b digits, usually b = 4).

"Structured" P2P means the overlay graph is shaped by the IDs, which is what
makes a guaranteed bounded lookup possible. Unstructured systems like Gnutella
flood or random-walk instead and give no such guarantee.

#### 3.2 Consistent hashing (Karger et al., 1997)

With `owner(k) = h(k) mod N`, changing N to N+1 reassigns a key unless
`h(k) mod N == h(k) mod (N+1)`. That happens for only about 1/(N+1) of keys,
so **≈ N/(N+1) of all keys move**. With successor-on-a-ring, a new node
*n* takes over only the arc `(pred(n), n]`. Its expected size is 1/(N+1) of the
ring, so **≈ K/(N+1) keys move**, the minimum possible for staying balanced.

**Measured (this repo, `run()` averaged over 300 seeds, each a different ring):**

| N | modulo: keys moved | consistent: mean moved | theory 1/(N+1) | consistent: std. dev. | worst seed |
|---|---|---|---|---|---|
| 10 | ≈ 91% | 9.19% | 9.09% | 9.16 pp | 62.5% |
| 100 | ≈ 99% | 0.93% | 0.99% | 0.92 pp | 4.3% |

The **mean** matches theory. The **spread is huge**: the standard deviation is
about equal to the mean. With one random point per node, arc lengths roughly
follow an exponential distribution, so some nodes get tiny arcs and some get
huge ones. The largest arc is about (ln N)/N of the ring, which is O(log N)
times the fair share. The Chord paper fixes this with **virtual nodes**: each
physical node holds O(log N) IDs. We'll see this imbalance again in Phase 5's
keys-per-node measurements.

*Honesty note:* the first version of the demo used fixed server names, so
every seed tested the **same** ring. That ring happened to give 5.6% instead
of 9.1%. Averaging over seeds exposed the mistake; the fixed version derives
server IDs from the seed too.

#### 3.3 Why O(log N) matters

Two costs pull against each other:

| Design | Routing state per node | Hops per lookup | Problem |
|---|---|---|---|
| Know only your successor | O(1) | O(N) | 1,000,000 nodes → ~500,000 hops on average |
| Know everyone (full mesh) | O(N) | 1 | Every join/leave must update N tables; at high churn, the tables are never correct |
| **Chord / Pastry** | **O(log N)** | **O(log N)** | The trade-off point: both grow very slowly |

Concrete numbers for the N range we'll sweep:

| N | log₂N | Chord expected hops ≈ ½·log₂N | Pastry expected hops ≈ log₁₆N | Linear walk ≈ N/2 |
|---|---|---|---|---|
| 50 | 5.6 | 2.8 | 1.4 | 25 |
| 1,000 | 10.0 | 5.0 | 2.5 | 500 |
| 5,000 | 12.3 | 6.1 | 3.1 | 2,500 |
| 10⁶ | 19.9 | 10.0 | 5.0 | 500,000 |

- **Chord's ½·log₂N**: each finger hop at least halves the remaining ring
  distance. Averaged over random keys, about half the bits of the distance
  need "correcting", hence ½·log₂N (Stoica et al., Thm. 2 and Fig. 10).
- **Pastry's log₁₆N**: each hop fixes at least one more hex digit of shared
  prefix. That is 4 bits per hop instead of about 1 on average, so fewer hops
  but a larger table: about (2^b − 1)·log₁₆N routing entries plus the leaf set.

**Hops are not latency.** One hop across an ocean can cost more than three hops
inside a city. Chord picks the next hop purely by ID. Pastry, among the many
nodes that qualify for each table slot, prefers the physically closest one.
Rowstron & Druschel report that the total route is usually under 2× the
direct-path latency. This is why `sim/network.py` (Phase 1) places nodes in 2-D
space: without real distances, Pastry's main advantage can't be seen.

#### 3.4 Reproducibility design

- `derive_seed(master, stream) = first 64 bits of SHA-256("master:stream")`.
  We deliberately avoid Python's `hash()`, because it is salted differently in
  every process for strings.
- Each experiment configuration will be run with several seeds and reported as
  mean ± spread, as you required. One seed is one sample, not a result, as the
  demo above showed.

#### 3.5 Environment note (honest record)

The development Mac has a system `python3` of 3.9.6, which is too old. Homebrew
3.12.14 was tried first, but on this machine `platform.mac_ver()` returns an
empty string, which crashes pip during venv creation. Homebrew **Python 3.14.0**
works and meets the "3.11+" requirement, so the venv uses it. No code depends
on 3.14-only features.

### 4. How to run and test Phase 0

```bash
cd ~/chord-vs-pastry
source .venv/bin/activate                 # every new terminal

# (Only if setting up from scratch / on another machine)
python3.14 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# Run the tests (expect: 21 passed)
python -m pytest -v

# Consistent hashing demo
python -m experiments.phase0_consistent_hashing_demo
python -m experiments.phase0_consistent_hashing_demo --servers 100 --keys 50000 --seed 7

# Check reproducibility: both lines must print the same hash
python -m experiments.phase0_consistent_hashing_demo --seed 42 | md5
python -m experiments.phase0_consistent_hashing_demo --seed 42 | md5
```

Expected demo output (seed 42, defaults):
```
Adding 1 server to 10 servers holding 20000 keys (seed=42):
  modulo hashing     :  90.89% of keys moved
  consistent hashing :   6.70% of keys moved
  ideal (1/(N+1))    :   9.09%
```
(6.70% for this one ring is normal. The *average* over many rings is about 9.1%.
See §3.2.)

### Questions my professor might ask (Phase 0)

**Q1. What is a DHT, and what's the one operation it really provides?**
A decentralised system that maps keys to nodes with no central directory. Its
core primitive is `lookup(key) → responsible node`. Storage (`put`/`get`) is
an application built on top.

**Q2. Why not just use `hash(key) mod N`?**
When N changes, about N/(N+1) of keys move, which is nearly all of them. Under
churn that means constant mass data transfer. Consistent hashing moves only
about K/(N+1) keys. We measured 91% vs 9% at N=10.

**Q3. Does consistent hashing balance load perfectly?**
No. On average yes, but with one ID per node the largest arc is about O(log N)
times the fair share. We measured a standard deviation equal to the mean. Chord
fixes this with O(log N) virtual nodes per physical node.

**Q4. Why is O(log N) the "sweet spot"?**
O(1) state means O(N) hops. O(N) state means 1 hop but every membership change
touches every node. O(log N) for both keeps lookups short *and* keeps
maintenance cheap: about 20 hops/entries at a million nodes.

**Q5. Chord gets ½·log₂N hops and Pastry about log₁₆N. So Pastry is just better?**
Not automatically. Pastry pays for it with a bigger routing table (about
15·log₁₆N entries plus leaf and neighbourhood sets) and more complex
maintenance. Also, *hops* are not *latency*. We measure both, which is the
point of the project.

**Q6. Why SHA-1 if it's cryptographically broken?**
We need uniform, deterministic spreading of IDs, not collision resistance
against attackers. SHA-1 still provides that, and both papers use it. A
production system facing malicious nodes would worry about ID assignment
(Sybil attacks) regardless of the hash function.

**Q7. How do you guarantee your experiments are reproducible?**
All randomness comes from named streams derived from one master seed via
SHA-256. Nothing uses the global `random` module or Python's salted `hash()`.
Same seed means byte-identical output, which we checked with `md5`.

**Q8. Why simulate instead of using real sockets?**
To isolate the algorithm. A simulation gives exact control over latency, loss
and churn, can run 5,000 nodes on a laptop, and is fully repeatable. The
trade-off is realism: no real congestion, OS scheduling or NAT. That's
recorded as a limitation in Phase 6.

---

## Phase 1: Simulation core

### 1. Beginner (no jargon)

**A post office on paper.** Imagine you want to study how letters travel
between 1,000 towns, but you can't wait weeks for real letters. So you
pretend. You keep a **pile of envelopes, each stamped with the date it will
arrive**. You repeatedly:

1. pick the envelope with the *earliest* arrival date,
2. move your calendar forward to that date (skipping every boring day in
   between),
3. "deliver" it: the town that receives it reads it and may write new
   letters, which go back on the pile with their own arrival dates.

That is a **discrete-event simulation**. Nothing happens *between*
envelopes, so you can simulate a year of post in a few seconds. The `simpy`
library is the clerk who keeps the pile sorted and moves the calendar.

**How long does a letter take?** Every town has a spot on a map. A letter
takes as many milliseconds as the straight-line distance between the two
towns. Optionally we add a little random delay (**jitter**, like a slow
sorting office) and lose a few letters at random (**loss**).

**How do you know a town has burned down?** You don't, directly. You write
"please reply" and wait. If no reply comes within, say, one second, you
*assume* something went wrong. You can't tell whether the town is gone, your
letter got lost, or their reply got lost. That waiting rule is a **timeout**,
and it's the only way real distributed systems detect failures too.

**Three maps.** We can draw the towns three ways:
- *Evenly spread* over a square (like a flat country).
- *In a few tight clusters* (like continents). Mail inside a continent is
  quick; mail across an ocean is slow. This is where choosing *nearby*
  helpers should pay off, which is Pastry's big idea.
- *A magic world* where every letter takes exactly the same time. Choosing
  nearby helpers can't help here, so if Pastry still wins, it must be for a
  different reason. Scientists call this a **control**.

**One clerk counts everything.** A single counter at the post office records
every letter, whoever sent it. Chord and Pastry are measured by the same
clerk using the same rules, so the comparison is fair.

### 2. Intermediate: how the code is organised

| File | Class / function | What it does |
|---|---|---|
| `sim/topology.py` | `UniformSquare`, `Clustered`, `Homogeneous` | Place a node (`sample_position(rng)`) and give the one-way latency between two positions (`latency(p, q)`). `setup(rng)` fixes random layout choices (cluster centres) for one seed and returns a new object, so the original spec can be reused. |
| `sim/network.py` | `Message` | One message: `src`, `dst`, `kind`, `payload`, `tag`, plus RPC bookkeeping (`expects_reply`, `is_reply`, `reply_to`). |
| | `Network` | Owns the simpy `env`, all nodes and the metrics. `create_node(cls)` assigns an address (0, 1, 2, …) and a position. `send(msg)` counts the message, maybe drops it (loss), then schedules delivery after `latency + jitter`. On arrival it drops the message if the destination is dead, otherwise calls `node.receive(msg)`. |
| `sim/node.py` | `BaseNode` | `send` (one-way), `rpc` (request, wait, reply or `RpcTimeout`), `receive` (dispatch to `handle_<kind>`), `spawn` (start a process that ends quietly if the node crashes), `run_periodically` (randomised repeating task), `fail` (crash). Also declares `join/leave/lookup/routing_state_size`, which Chord and Pastry must implement. |
| `sim/metrics.py` | `Metrics`, `LookupRecord`, `summarize`, `percentile` | Message counts by kind and tag, lost/dropped counts, one record per lookup, per-node distributions, and summaries (mean, std, p50/p95/p99). |
| `experiments/phase1_network_demo.py` | | Pings random pairs on each topology and prints RTT statistics, then repeats with 10% loss. |
| `tests/test_phase1_sim_core.py` | | 40 tests (listed in §4). |

**Life of one RPC** (`a` asks `b` "ping"):

```
a.rpc(b, "ping")                         a's process pauses here ──────┐
  └─ Network.send(msg)                                                 │
       ├─ metrics.on_send   (counted, even if it is then lost)         │
       ├─ lost? (random < loss_rate)  → gone; a will time out          │
       └─ env.timeout(latency + jitter) ──► Network.arrive(msg)       │
                                              ├─ b dead? → dropped     │
                                              └─ b.receive(msg)        │
                                                   └─ b.handle_ping(msg) returns "pong"
                                                        └─ b._reply → Network.send(reply) …
                                                              … a.receive(reply) → wakes a ┘
```

**Writing a node.** Subclass `BaseNode` and add `handle_<kind>` methods. A
handler returns its answer directly, or contains `yield` if it must wait
(for example to ask another node first):

```python
class MyNode(BaseNode):
    def handle_ping(self, msg):                 # answers at once
        return "pong"

    def handle_ask_friend(self, msg):           # waits for another node
        answer = yield from self.rpc(msg.payload["friend"], "ping")
        return answer                           # becomes the reply
```

**Random streams used in Phase 1** (all derived from the one experiment
seed): `topology-layout` (cluster centres), `topology-positions` (where each
node lands), `network-links` (jitter and loss), and `node-<address>` (each
node's own timers).

### 3. Advanced: design and analysis

#### 3.1 Discrete-event simulation with simpy

simpy keeps a priority queue (a binary heap) of `(time, event)` pairs.
`env.run()` pops the earliest event, sets `env.now` to its time, and runs
its callbacks. A **process** is a Python generator. `yield event` suspends
it, and simpy resumes it with the event's value when the event fires.
Consequences:

- **Cost is per event, not per millisecond.** A message costs O(log E) heap
  work (E = pending events), whether its latency is 1 ms or 1 s.
- **Time is exact and deterministic.** `env.now` is a float that only
  changes when events fire, so tests can assert `t == 22.0` exactly. Ties
  are broken by insertion order, so a run with the same seed always
  replays identically. We checked this with `md5` on the demo output.
- **Delivery uses a timeout with a callback** instead of one process per
  message, which avoids a generator object per message. This matters when
  Phase 5 sends hundreds of thousands of messages.

#### 3.2 The latency model

`delay(a→b) = topology.latency(pos_a, pos_b) + U(0, jitter)`, and a message
is lost with probability `loss_rate`. Distances are in ms.

- **Why a Euclidean plane?** Rowstron & Druschel evaluated Pastry's
  locality on exactly this model (plus a sphere and a GT-ITM transit-stub
  topology). In a Euclidean space the **triangle inequality** holds
  (d(a,c) ≤ d(a,b) + d(b,c)). Pastry's locality argument relies on it:
  if your neighbour is close to you, your neighbour's neighbours are probably
  close to you too. Real Internet latencies sometimes violate it (routing
  detours), which is a known limitation of plane models.
- **Clustered = heterogeneous.** Intra-cluster one-way latency is about
  `spread·√π ≈ 8.9 ms`. Inter-cluster latency is about the distance between
  centres. That gives a bimodal distribution with a big payoff for picking
  a nearby next hop.
- **Homogeneous = control.** Every link costs 50 ms, so latency is just
  `hops × 50` (plus the reply). Any Pastry latency advantage here can only
  come from fewer hops, not from proximity.

**Measured** (`phase1_network_demo`, RTT = 2 × one-way, ms):

| Topology | seed | mean | p5 | p50 | p95 | max |
|---|---|---|---|---|---|---|
| uniform | 42 | 106.3 | 28.0 | 104.2 | 189.4 | 247.0 |
| clustered | 42 | 106.2 | 10.2 | 128.6 | 192.0 | 224.1 |
| homogeneous | 42 | 100.0 | 100.0 | 100.0 | 100.0 | 100.0 |
| uniform | 7 | 102.4 | 26.5 | 100.6 | 185.7 | 258.6 |
| clustered | 7 | 75.4 | 9.3 | 75.2 | 152.1 | 194.0 |

Uniform matches theory: the mean distance between two random points in a
unit square is ≈ 0.5214, so ≈ 52 ms one-way and ≈ 104 ms RTT.

**Finding:** the clustered topology's mean RTT changes a lot between seeds
(106 → 75 ms), because the 5 cluster centres land in different places.
Two rules for Phase 5 follow from this:
1. **Paired comparison.** For each seed, Chord and Pastry run on the *same*
   node positions. Report the difference (or ratio) per seed, not just two
   independent averages.
2. **Several seeds** per configuration, reporting mean and spread across seeds.

#### 3.3 RPCs, timeouts and failure detection

The failure model is **crash-stop**: a failed node stops instantly, sends
nothing more, and never comes back. A "rejoin" in Phase 4 will be a new
node with a new address. Other nodes learn about it only through timeouts.

A timeout is an **unreliable failure detector**. It can't distinguish
"dead" from "slow" or from "message lost". The choice of timeout is a
trade-off:
- too short: live-but-far nodes are wrongly declared dead (*false
  suspicion*), causing needless routing-table repairs;
- too long: lookups stall for a long time on a dead next hop.

`BaseNode.DEFAULT_RPC_TIMEOUT = 1000 ms`, about 4× the largest possible RTT
on the default uniform map (2 × 141 ms). Overlays may choose their own.

With independent loss probability *p*, an RPC needs two messages to survive,
so P(timeout) = 1 − (1 − p)². For p = 0.1 that's 19%. **Measured: 19.1%
(seed 42) and 18.9% (seed 7).** Every lost message produced exactly one
timeout (lost = timeouts in the demo output), as expected: a lost request
means no reply is ever sent.

Edge cases the tests pin down:
- A reply that arrives *after* the timeout is ignored (the pending entry is gone).
- A message in flight to a node that crashes before arrival is dropped.
- If the *caller* crashes while waiting, its process ends quietly
  (`NodeCrashed` is caught by `spawn`) and the reply is counted as
  `dropped_dead`.
- A message kind with no handler raises `UnknownMessageKind`. Bugs should be
  loud, not silent.

#### 3.4 Accounting: kinds and tags

Every message is counted when *sent*, including ones that are later lost, by
`kind` (`"ping"`, `"ping.reply"`) and by `tag` (why it was sent). Replies
inherit the request's tag, so an RPC tagged `"join"` counts as 2 join
messages. In Phase 5, "messages per join" will be the sum over the
`"join"` tag, and maintenance overhead the sum over `"maintenance"`. Both
overlays will use the same tag names.

`lookup_summary()` averages hops and latency over **successful** lookups only.
A lookup that times out has no meaningful latency, so failures are reported
separately as `success_rate`. Mixing them would make a failing overlay look
*faster*.

#### 3.5 Simplifications (and whether they could bias the comparison)

| Simplification | Effect on Chord vs. Pastry |
|---|---|
| No bandwidth limits or queueing; zero processing time | Pastry's larger messages (e.g. state tables sent during join) cost the same as tiny ones. **Favours Pastry** slightly on join cost in *time*, but we count *messages*, which is still fair. |
| Symmetric latency | Neutral. |
| Euclidean distances satisfy the triangle inequality | **Favours Pastry**: its locality heuristics work best when the triangle inequality holds. Stated as a limitation. |
| Crash-stop only (no network partitions, no Byzantine nodes) | Neutral; both papers assume the same. |
| A node "measuring" proximity will (in Phase 3) read `network.latency(a, b)` instead of averaging real pings | **Favours Pastry** slightly: real measurements are noisy. To be listed in Phase 3's simplifications. |

#### 3.6 A decision for Phase 2 to keep fair

Lookups can be **iterative** (the origin asks each hop in turn and gets told
the next one: 2 messages per hop, all via the origin) or **recursive** (each
hop forwards the request, and the last one replies to the origin). Pastry's
paper routes recursively. Chord's pseudocode reads as recursive RPC calls.
The style changes latency a lot, so **both overlays will use the same style**,
and it'll be documented in Phase 2.

#### 3.7 How the tests were checked

Tests that pass the first time can be too weak. As a check, the network
delay was temporarily multiplied by 1.01 (a 1% bug), and 6 of the 40 Phase 1
tests failed. The code was then restored. So the timing tests really detect
small errors.

### 4. How to run and test Phase 1

```bash
cd ~/chord-vs-pastry
source .venv/bin/activate

# All tests so far (expect: 61 passed = 21 Phase 0 + 40 Phase 1)
python -m pytest -v

# Only Phase 1
python -m pytest tests/test_phase1_sim_core.py -v

# The topology / RPC demo
python -m experiments.phase1_network_demo
python -m experiments.phase1_network_demo --nodes 500 --pings 5000 --seed 7
```

Expected demo output (default seed 42):
```
topology        mean      p5     p50     p95     max
uniform        106.3    28.0   104.2   189.4   247.0
clustered      106.2    10.2   128.6   192.0   224.1
homogeneous    100.0   100.0   100.0   100.0   100.0

Same pings on the uniform topology with 10% message loss:
  messages sent 3779, lost 383, RPC timeouts 383 of 2000 pings (19.1%; theory 1 - 0.9^2 = 19%)
```

What the Phase 1 tests check:
- **Timing:** one-way delay = distance (3-4-5 triangle gives exactly 5 ms);
  RPC = 2× distance; nested RPC adds up (22 ms); handler waiting time is
  included; self-messages take 0 ms; messages arrive in time order.
- **Jitter and loss:** jitter stays in [d, d + jitter] and averages d + jitter/2;
  30% loss drops 30% ± 3%; bad parameters are rejected.
- **Failures:** messages to dead nodes are dropped (including ones in
  flight); RPC to a dead node times out at exactly the timeout; late replies
  are ignored; a crashed caller stops quietly and can't send.
- **Periodic tasks:** they fire on schedule and stop when the node dies;
  jittered timers stay in range.
- **Topologies:** uniform positions stay in bounds; clustered nodes sit
  near a centre; homogeneous latency is constant; the same seed gives
  an identical simulation and a different seed gives a different one; the
  topology object isn't modified by `setup()`.
- **Metrics:** counts by kind and tag (replies inherit the tag); `percentile`
  equals numpy's; `summarize` handles empty input; `lookup_summary` excludes
  failed lookups from hops and latency; `reset` clears everything.

### Questions my professor might ask (Phase 1)

**Q1. Why a discrete-event simulation instead of threads or real sockets?**
It's deterministic (same seed, same result), fast (time jumps from event to
event, so idle time costs nothing), and scales to thousands of nodes on a
laptop. It also isolates the *algorithm*: there's no OS scheduling or real
network noise to confuse the comparison. The price is realism (see Q7).

**Q2. How is message latency computed, and why 2-D coordinates?**
One-way delay = Euclidean distance between the two nodes' coordinates (in ms)
plus optional uniform jitter. Coordinates give a *consistent geometry*:
some nodes are genuinely near each other. That's exactly what
proximity-aware routing (Pastry) exploits and Chord ignores. It's the same
"Euclidean plane" model the Pastry paper used.

**Q3. Why three topologies?**
Uniform is the paper's baseline. Clustered is the *heterogeneous* case named
in our hypothesis (cheap intra-continent links, expensive inter-continent
ones). Homogeneous is the **control**: with equal link costs, proximity
can't help, so it separates "Pastry is faster because of fewer hops" from
"Pastry is faster because it picks nearby hops".

**Q4. How does a node know another node has failed? Can it be sure?**
Only by a timeout on an RPC. It can never be sure: a timeout looks the same
whether the node crashed, the request was lost, or the reply was lost. In
an asynchronous system, crashed vs. slow can't be told apart, so failure
detectors are inherently unreliable. Timeout length trades false suspicions
against slow recovery.

**Q5. With 10% message loss, what fraction of RPCs fail?**
1 − 0.9² = 19%, because both request and reply must survive. We measured
19.1%.

**Q6. How do you make sure Chord and Pastry are measured identically?**
They share one `Network` (same latency, loss, jitter) and one `Metrics`
object. Every message is counted by the network itself at send time, by kind
and purpose tag. Both overlays report lookups through the same
`LookupRecord`. They also run on identical node positions for each seed
(paired comparison).

**Q7. What does your simulator *not* model? Could that change your conclusions?**
Bandwidth, queueing, CPU time, asymmetric routes, triangle-inequality
violations, NAT and partitions. Most are neutral between Chord and Pastry.
Two slightly favour Pastry: free large messages and a clean Euclidean
geometry. We state these as limitations rather than hide them.

**Q8. Why does the clustered network's average latency vary so much between seeds? What do you do about it?**
The cluster centres are random, so some seeds put continents close together
and some far apart (mean RTT 106 ms vs 75 ms for two seeds). Therefore we
(a) run several seeds and report spread, and (b) compare Chord and Pastry on
the *same* seed's layout (paired), so layout luck cancels out.

**Q9. What happens to a message in flight when its destination crashes?**
It's dropped on arrival (counted as `dropped_dead`), and the sender times out
if it was expecting a reply. Our network checks liveness at *arrival* time,
not send time, which is what a real network does.

---

## Phase 2: Chord

### 1. Beginner (no jargon)

**A round street.** Picture a very long circular street with house numbers
0 to 4,294,967,295 (that's 2³²), most of them empty plots. Each participating
computer lives at one number, picked by hashing its name. Each book also gets
a number in the same way. **Rule:** a book is kept at the first occupied
house at or after its number, walking clockwise. That house is the book's
*owner*.

**The problem.** If each house only knew its next-door neighbour, a search
might walk half-way round a street of thousands of houses.

**The trick: a card of shortcuts.** Every house keeps a small card with the
addresses of the houses that are 1, 2, 4, 8, 16 … plots ahead, all the way to
half-way round the street. That card is the **finger table**. To find a
book, you look at your card and phone the shortcut that gets **closest to the
book without going past it**. That person does the same with their card, and
so on. Each call at least halves the remaining distance, so even with 4,096
houses it took only about 6 calls.

**Moving in.** A newcomer asks any resident, "who will be my next-door
neighbour?" (a normal search for its own number), moves in before that
house, and copies its shortcut card.

**Keeping the street tidy.** Every second or so, each house asks its next
neighbour "who do you think is just before you?". If somebody new has moved
in between, it updates its notes. This is **stabilization**, and it's what
makes Chord work even when many people move at once.

**Fires.** Each house also remembers the next 8 houses, not just one (the
**successor list**). If the neighbour's house burns down (crashes), you
skip to the next one on your list. In our test we "burned down" 25% of
houses at once. Every search still found the right owner, it was just
slower for a while, and the street was fully repaired within a minute.

### 2. Intermediate: how the code is organised

| File | What it contains |
|---|---|
| `chord/node.py` | `NodeRef(id, addr)`: how nodes refer to each other (an immutable tuple). `ChordNode(BaseNode)`: all of Chord's state and protocol. |
| `chord/ring.py` | **Test/experiment-only** helpers with a global view: `create_nodes` (unique IDs), `build_static` (perfect state instantly), `Oracle` (true owner of any key), `check_ring`, `finger_accuracy`. Nodes never call these. |
| `experiments/phase2_chord_demo.py` | Part A: hops/latency/state on static rings of 16–4,096 nodes. Part B: a 100-node ring built by joins, then 25% crash. |
| `tests/test_phase2_chord.py` | 24 tests (listed in §4). |

**State of one `ChordNode`:**

| Field | Meaning |
|---|---|
| `id` | Position on the ring: SHA-1(`"chord-node:<seed>:<address>"`), top m = 32 bits |
| `predecessor` | The previous node (or `None` if unknown) |
| `successors` | The next r = 8 nodes clockwise. `successors[0]` is *the* successor |
| `fingers[i]` | successor(id + 2ⁱ) for i = 0..m−1 (`None` = unknown) |

**Paper pseudocode → our methods:**

| Paper | Here | Kind of message |
|---|---|---|
| `find_successor(id)` | `find_successor()` → shared `OverlayNode.route()`; Chord's `next_hop()` picks each step (since Phase 3, see §3.1 there) | `route` (+ `route.reply` = ack), `route_done` |
| `closest_preceding_node(id)` | `closest_preceding_node()` | local |
| `join(n')` | `join()` + `init_fingers()` | tag `join` |
| `stabilize()` / `notify(n')` | `stabilize()` / `handle_notify()` | `get_neighbors`, `notify` |
| `fix_fingers()` | `fix_fingers()` | `route` … |
| `check_predecessor()` | `check_predecessor()` | `ping` |
| voluntary leave | `leave()` | `predecessor_leaving`, `successor_leaving` |

**Message tags** (used for counting costs): `lookup`, `join`,
`maintenance` (stabilize, fix_fingers, check_predecessor), `leave`.

**Using it:**
```python
nodes = create_nodes(net, 1000)       # chord.ring
build_static(nodes)                   # or: join one by one + start_maintenance()
record = yield from nodes[0].lookup("my-key")   # LookupRecord: hops, latency, owner …
```

### 3. Advanced: algorithm, analysis, and differences from the paper

#### 3.1 Ownership and ring intervals

Key k is owned by successor(k), the first node whose ID is ≥ k mod 2^m.
Equivalently, node n owns the half-open arc **(predecessor(n), n]**. Every
decision in Chord is an interval test on the circle, which is why
`in_interval` (Phase 0) was tested on the wrap-around cases. Phase 2
additionally resolves *every one* of the 256 IDs on an m = 8 ring, including
the 255 → 0 wrap.

#### 3.2 The lookup: recursive routing with per-hop acks

At node n, for key k:

1. If k ∈ (pred(n), n], **I own it**: send `route_done` to the origin.
2. Else if k ∈ (n, successor], **my successor owns it**: forward with `final = True`.
3. Else forward to `closest_preceding_node(k)`: among my fingers *and* successor
   list, the node in (n, k) furthest from me.

**Why it terminates.** In step 3 the next node is strictly inside (n, k),
so the clockwise distance to k strictly decreases at every hop. It can't loop
while the state is correct. `MAX_HOPS = 64` is a safety net for broken
states during churn.

**Why it's correct.** Step 2 only relies on *successor* pointers. Fingers
only make it faster. A wrong finger can cost hops but never gives a wrong
answer, as long as successors are right. That's Chord's key design
principle: correctness depends on a small invariant, and performance on
everything else.

**Recursive vs iterative.** We forward hop by hop, and the owner replies
directly to the origin. Lookup latency = Σ link latencies along the path +
owner→origin. A test checks this equality for 300 lookups. Each forward is
an RPC answered by an immediate ack, so a dead next hop is noticed after
`HOP_TIMEOUT = 500 ms` and the forwarder tries the next best candidate. Acks
travel in parallel, so they add messages (2 per hop) but no latency. Pastry
(Phase 3) will use exactly the same mechanism.

#### 3.3 Hop count: why about ½·log₂N, and what we measured

**Intuition.** Measure distance in "number of nodes". If the key is d nodes
away, write d in binary. Each finger hop removes (at least) the highest
1-bit, so the number of hops ≈ the number of 1-bits in a log₂N-bit number.
For a random d that's about **half of log₂N**. The paper proves O(log N) w.h.p.
and reports a mean of about ½·log₂N in simulation.

**Measured** (`phase2_chord_demo`, static rings, uniform topology, 3 seeds ×
1,000 lookups, all 100% correct):

| N | mean hops (to owner) | std across seeds | p95 | ½·log₂N | difference | mean latency (ms) | routing state |
|---|---|---|---|---|---|---|---|
| 16 | 2.14 | 0.02 | 3 | 2.00 | +0.14 | 154.3 | 9.5 |
| 64 | 3.28 | 0.02 | 5 | 3.00 | +0.28 | 214.8 | 11.6 |
| 256 | 4.26 | 0.02 | 6 | 4.00 | +0.26 | 271.0 | 13.6 |
| 1,024 | 5.27 | 0.02 | 7 | 5.00 | +0.27 | 325.7 | 15.6 |
| 4,096 | 6.28 | 0.05 | 9 | 6.00 | +0.28 | 380.0 | 17.6 |

- **Growth rate:** +0.50 hops per doubling of N (5.27 → 6.28 over two
  doublings), exactly the ½·log₂N slope. A separate test checks this slope
  (256 → 2,048 nodes) to be within 0.4–0.6.
- **Level:** a steady **+0.27** above ½·log₂N. *Investigation:* our `hops`
  counts every forward *including the last one from the key's predecessor
  to the owner*. That is the right number for an application, because the
  data lives on the owner, and it's the same definition Pastry will use.
  The ½·log₂N argument counts finger hops to reach the *predecessor*. If we
  subtract that final hop (when present) we get about ½·log₂N − 0.7. The
  paper's text doesn't make it clear which of the two its plotted "path
  length" counts, so we don't claim an exact match. **What we can claim:**
  the slope is ½ per doubling, the level is within 0.3 hops of ½·log₂N, and
  the p95 grows logarithmically too.
- **Latency ≈ (hops + 1) × 52 ms.** For N = 4,096: (6.28 + 1) × 52 ≈ 379 ms,
  against 380 ms measured. 52 ms is the mean distance between two *random*
  points in the square. So each Chord hop is a random-length trip: Chord
  routing ignores physical location. This is exactly the opening Pastry's
  proximity routing targets.

#### 3.4 Routing state

The finger table has m = 32 slots, but only about log₂N of them are
*distinct* nodes. The low fingers (1, 2, 4 … positions ahead) all point to
the immediate successor. Counting distinct nodes in fingers ∪ successor
list ∪ predecessor gives 9.5 → 17.6 from N = 16 → 4,096, which is about
log₂N + 6, because the successor list (8) overlaps the smallest fingers.
**O(log N) state, as claimed.**

#### 3.5 Join, and its cost

1. `find_successor(my_id)` via the bootstrap node (we aren't on the ring yet,
   so routing starts at the bootstrap).
2. Copy the successor's successor list (1 RPC).
3. **Initialise fingers** (from SIGCOMM'01 `init_finger_table`). If
   finger i's start is not past finger i−1, reuse finger i−1 with no lookup.
   Only about log₂N of the 32 fingers need a real lookup.
4. `notify` the successor. The predecessor learns about us on its next
   `stabilize`.

Cost ≈ log₂N lookups × (½·log₂N hops × 2 messages + 1) ≈ log₂²N + 2·log₂N
messages. **Measured:** mean 40.6 messages per join while growing a ring to
100 nodes (log₂²100 + 2·log₂100 ≈ 57; many early joins were into much
smaller rings). Into a 1,000-node ring: mean **101 / 107 / 104** messages over 10 joins for seeds 1 / 2 / 3, range 94–114 (formula: ≈ 120).
This is the paper's **O(log²N)** bound. The original test assumed a constant
of 1 (cost < log₂²N), measured 101 against 99, and failed. The bound was
corrected to < 2·log₂²N with the reasoning above. It wasn't a code bug.

#### 3.6 Stabilization and convergence

Each node runs, with randomised timers (±50%):
- `stabilize` (~1 s): ask the successor for (predecessor, successor list).
  If the successor's predecessor x lies in (me, successor), x has joined in
  between, so adopt x. Copy the list, then `notify(successor)`.
- `notify(n')`: accept n' as predecessor **only if** it lies in
  (predecessor, me). This stops a stale message from a node further back
  replacing a better pointer.
- `fix_fingers` (~1 s): refresh one finger per call, cycling through all 32.
  Fingers whose start is ≤ successor are fixed locally with no messages.
- `check_predecessor` (~2 s): ping the predecessor and clear it if dead.

**Measured** (100 sequential joins, seed 1): ring pointer errors 16 → 3 → 0
at +0 s / +5 s / +30 s after the last join. Fingers 91% → 100% correct.
**Concurrent joins** (30 nodes joining through one bootstrap at the same
instant) also converge to a perfect ring. This is the situation the paper's
Theorem 4 (stabilization eventually fixes concurrent joins) covers.

#### 3.7 Failures and leaving

- **Crash:** the node just stops. Neighbours detect it by timeouts:
  `stabilize` skips a dead successor and uses the next one in the list;
  `check_predecessor` clears a dead predecessor; any RPC timeout calls
  `forget()`, which removes the node from all state.
- **Why a successor list of r = 8?** The ring only breaks if all r successors
  of some node fail together. If a fraction p of nodes fail independently,
  that has probability ≈ pʳ per node. For p = 0.25 that's 1.5 × 10⁻⁵, so
  about 0.0015 broken nodes expected out of 100. The paper recommends
  r = O(log N).
- **Measured** (100 nodes, 25% crash at once): before the crash, latency
  233 ms; **straight after**, 100% success and 100% correct, but 670 ms
  mean latency (lookups wait out 500 ms timeouts on dead hops); after 60 s of
  repair, ring and fingers 100% correct and latency back to 214 ms (fewer
  nodes, so fewer hops).
- **Graceful leave:** tell the successor its new predecessor, and give the
  predecessor our successor list. A test shows neighbours are correct
  *without any stabilization running*. (In Phase 4 the leaving node will also
  hand its keys to its successor.)

#### 3.8 Every difference from the paper

| # | Paper | Ours | Why / effect |
|---|---|---|---|
| 1 | m = 160 (full SHA-1) | m = 32 (configurable) | Faster `fix_fingers` cycle (32 entries). Collision chance for 5,000 nodes ≈ N²/2³³ ≈ 0.3%, and `create_nodes` re-hashes on collision. No effect on hops. |
| 2 | ID = SHA-1(IP address) | SHA-1(`seed:address`) | Each seed gives a different ring, so experiments can average over rings. |
| 3 | Paper's simulator was iterative; pseudocode recursive | Recursive, with per-hop acks | Same mechanism as Pastry, for a fair latency comparison (§3.2). |
| 4 | Node checks only k ∈ (n, successor] | Also k ∈ (pred, n]: owner answers its own key in 0 hops | Same 0-hop behaviour as Pastry. Affects about 1/N of lookups. |
| 5 | `closest_preceding_node` over fingers | Over fingers **and** successor list | Suggested in the ToN version. Helps route around failed fingers. |
| 6 | SIGCOMM'01 join also updates *other* nodes' fingers (`update_others`) | Not done; other nodes' `fix_fingers` find the newcomer | This is the ToN version's approach. Simpler and robust to concurrent joins. |
| 7 | Timers unspecified / periodic | Randomised ±50% around 1 s / 1 s / 2 s | Avoids lock-step behaviour. Periods will be varied in Phase 5. |
| 8 | No virtual nodes in base protocol (they're an optional add-on) | None | Load imbalance (Phase 0) will show in Phase 5's keys-per-node results. |
| 9 | Successor list length r = O(log N) | r = 8 fixed | Enough for N ≤ 5,000 (§3.7). |
| 10 | — | If all successors are lost, fall back to the nearest live finger | Lets `stabilize` walk back to the true successor instead of leaving the node isolated. |
| 11 | — | Failed nodes never return; a rejoin is a new node | Crash-stop model from Phase 1. |
| 12 | — | Origin gives up after 10 s, no end-to-end retry | Simpler. Under churn this lowers the success rate a little. Revisit in Phase 4. |

### 4. How to run and test Phase 2

```bash
cd ~/chord-vs-pastry && source .venv/bin/activate

python -m pytest -v                            # expect 85 passed
python -m pytest tests/test_phase2_chord.py -v # just Chord (24 tests)

python -m experiments.phase2_chord_demo        # ~2 s
python -m experiments.phase2_chord_demo --seeds 1 2 3 4 5 --lookups 2000
```

Expected end of the demo (seed 1):
```
   before crash                 success 100.0%  correct 100.0%  latency mean    233 ms  hops 3.57
   -- 25 nodes (25%) crash at t=178 s --
   immediately after crash      success 100.0%  correct 100.0%  latency mean    670 ms  hops 3.74
   +60 s: ring pointer errors 0, fingers correct 100.0%
   after 60 s of repair         success 100.0%  correct 100.0%  latency mean    214 ms  hops 3.37
```

What the 24 Phase 2 tests check:
- **Static correctness:** 1,000 random lookups on 10 / 100 / 1,000 nodes all
  find the true owner; every one of the 256 IDs on an m = 8 ring resolves
  correctly.
- **Cost:** mean hops within 0.75 of ½·log₂N (N = 10 / 100 / 1,000); slope
  0.4–0.6 hops per doubling; latency = exact sum of link latencies on the
  path; routing state O(log N); join messages between log₂N and 2·log₂²N.
- **Membership:** a 2-node ring forms; 60 sequential joins and 30
  concurrent joins converge to a perfect ring and perfect fingers; `notify`
  rejects a farther predecessor; `stabilize` adopts a node that joined in
  between.
- **Failures:** graceful leave repairs neighbours without stabilization;
  lookups stay correct right after 20% crash (no repair running); the ring
  fully repairs after 25% crash; a crashed node sends nothing afterwards and
  is removed from successor lists.
- **Other:** a single node owns everything at 0 hops; IDs stay unique on a
  16-slot ring; the same seed gives identical lookups.

**Mutation check** (are the tests strong enough?):
1. Making `closest_preceding_node` pick the *nearest* finger (no halving):
   9 tests failed.
2. Making `notify` accept *any* predecessor: **no test failed at first**,
   because the ring heals eventually and the convergence tests only look at
   the end state. Two direct unit tests (`notify` rule, `stabilize` adoption)
   were added, and they catch it.

### Questions my professor might ask (Phase 2)

**Q1. Why does a Chord lookup take O(log N) hops?**
finger[i] points 2ⁱ positions ahead, so from any node there's a finger
covering at least half of the remaining distance to the key. Each hop at
least halves the distance, so after O(log N) hops the remaining distance is
smaller than the gap between adjacent nodes, which is where the key's
predecessor sits.

**Q2. Then why is the average ½·log₂N and not log₂N?**
The number of hops is roughly the number of 1-bits in the distance (in
nodes), written in binary. A random log₂N-bit number has about half its
bits set. We measured ½·log₂N + 0.27 with slope exactly ½ per doubling. The
+0.27 is because we count the final hop to the owner (§3.3).

**Q3. What happens if a finger table entry is wrong?**
Lookups may take more hops, but stay correct. Correctness only depends on
successor pointers (the final step uses k ∈ (n, successor]). Fingers are a
performance optimisation. That's why `fix_fingers` can be lazy.

**Q4. How can many nodes join at the same time without breaking the ring?**
A joining node only sets its own successor. Stabilization ("is your
predecessor between us?") gradually splices nodes in, and `notify` only
accepts a closer predecessor. The paper proves that this converges once
joins stop. We tested 30 simultaneous joins and got a perfect ring.

**Q5. What is the successor list, and how long should it be?**
The next r nodes clockwise. If the successor crashes, the next one takes
over immediately. The ring breaks only if all r fail together:
probability ≈ pʳ. With r = 8 and 25% failures that's about 10⁻⁵. The paper
suggests r = O(log N).

**Q6. Recursive or iterative routing? Why does it matter?**
Iterative: the origin contacts every hop itself (2 trips per hop through the
origin). Recursive: the request travels forward and the answer comes straight
back, which is roughly half the latency. We use recursive for *both* overlays,
so latency differences come from the algorithms, not the routing style.

**Q7. How many messages does a join cost?**
O(log²N): about log₂N finger lookups, each about ½·log₂N hops. We measured
101–107 messages (mean over 10 joins, 3 seeds) at N = 1,000 against a predicted ≈ 120, and a mean of 41
while growing to 100 nodes.

**Q8. Your lookup latency is almost exactly (hops + 1) × 52 ms. Why?**
52 ms is the average distance between two random points in our square. Chord
picks hops by ID only, so each hop goes to a random place on the map, and the
reply is one more random trip back. This is the baseline Pastry's proximity
routing should beat.

**Q9. What is the difference between a leave and a crash in your Chord?**
A leaving node tells its predecessor and successor, so they repair at once.
A crashed node tells no one, so neighbours notice by timeouts and repair
through stabilization. In between, lookups still succeed by skipping dead
hops, but they're slower (670 ms vs 233 ms in our demo).

**Q10. Can a Chord lookup ever return the wrong node?**
Yes, briefly, under churn. For example, a node that just joined isn't yet
known to its predecessor, so lookups for its keys still go to the old owner.
In our static and post-repair tests it was 100% correct. Phase 4/5 will
measure correctness *during* churn instead of assuming it.

**Q11. What did you change from the paper?**
See the table in §3.8: m = 32 instead of 160, recursive routing with acks,
a (pred, n] ownership check, successor-list-aware `closest_preceding_node`, no
`update_others` at join, randomised timers, r = 8, and no virtual nodes.

---

## Phase 3: Pastry

### 1. Beginner (no jargon)

**Phone numbers instead of a round street.** Pastry gives every computer and
every book a long "phone number" of 32 hex digits (0–9, a–f), for example
`65a1fc04…`. A book belongs to the computer whose number is **closest** to
the book's number.

**Routing by matching digits.** Say you want book `d46a1c…`. You don't know
who holds it, but your address book is organised by how many leading digits
an entry shares with *your* number. You find someone whose number starts
with `d…`. They know someone starting with `d4…`, who knows someone with
`d46…`, and so on. Each call fixes at least one more digit. With 16 possible
digits, each call narrows the search 16-fold, so 4,096 computers need only
about 3 calls (16 × 16 × 16 = 4,096). Chord narrowed it 2-fold per call.

**Prefer the friend down the street.** Many people might start with `d4…`.
Pastry's trick: for each entry in your address book, pick the one who
lives **physically closest** to you. The first calls (which only need to
fix one digit) have lots of candidates, so they're almost always quick local
calls. Only the last call or two may cross an ocean. Chord can't do this,
because its "half-way round the circle" contact is one specific person,
wherever they live.

**Next-door neighbours by number.** Each computer also keeps the 8 people
whose numbers are just above its own and the 8 just below (the **leaf
set**). When the book's number is in that range, you know the exact owner.
This list is what makes Pastry *correct*. The big address book only makes
it *fast*.

**Moving in.** A newcomer asks a physically nearby member to send a "hello"
message towards the newcomer's own number. Everyone it passes through hands
over useful address-book pages, and the last person (the one with the
closest number) hands over their next-door list. The newcomer then tells
everyone it learned about that it has arrived.

**When people disappear.** Neighbours check on each other every second. If
a next-door neighbour stops answering, you ask the farthest neighbour on
that side for their list and fill the gap. Address-book entries that have
gone dead are only fixed when you actually try to use one (Pastry is "lazy"
about them).

### 2. Intermediate: how the code is organised

| File | What it contains |
|---|---|
| `sim/overlay.py` (**new, shared**) | `NodeRef`, `OverlayNode`: the recursive route / ack / timeout / `route_done` / `lookup` machinery, moved out of Chord so both overlays use *identical* forwarding code. Subclasses only supply `next_hop()`, `forget()`, `key_id()`, and optionally `on_visit()` / `deliver()`. |
| `chord/node.py` | Now `ChordNode(OverlayNode)`. Its old routing loop became `next_hop()`. A behaviour fingerprint (500 lookups with jitter, loss, crashes, maintenance) was byte-identical before and after the move. |
| `pastry/node.py` | `PastryNode(OverlayNode)`: ID arithmetic, state, Table 1 routing, join, repair, maintenance, leave. |
| `pastry/ring.py` | Test-only: `create_nodes`, `nearest_node`, `Oracle`, `build_static(proximity="optimal"/"random")`, `check_leafsets`, `table_quality`. |
| `sim/topology.py` | Gained `latencies_from()` (numpy) so the static builder can find the closest node per slot quickly. |
| `experiments/phase3_pastry_demo.py` | Part A static sweep, Part B join-built network, Part C crash recovery. |
| `tests/test_phase3_pastry.py` | 31 tests (listed in §4). |

**State of one `PastryNode`** (b = 4):

| Field | Paper name | Contents |
|---|---|---|
| `id` | nodeId | 128-bit, shown as 32 hex digits |
| `table[row][col]` | routing table R | A node sharing the first `row` digits with me whose next digit is `col`, chosen as the **physically closest** I know |
| `leaf_left`, `leaf_right` | leaf set L (\|L\| = 16) | 8 numerically nearest smaller IDs, 8 nearest larger (nearest first) |
| `neighborhood` | neighbourhood set M (\|M\| = 32) | 32 physically nearest nodes I know |

**Key methods:**

| Method | What it does |
|---|---|
| `digit(id, row)`, `shl(a, b)`, `dist(a, b)` | A digit of an ID, shared-prefix length, circular numeric distance |
| `next_hop(p)` | The paper's Table 1 routing rule |
| `consider(refs, leaf=True)` | Learn about nodes: each part of the state keeps a node only if it's better (numerically closer for L, physically closer for R and M) |
| `check_and_add_leaves(refs)` | Second-hand nodes are pinged before entering the leaf set (§3.6) |
| `join()`, `on_visit()`, `deliver()` | Join protocol: the join message collects state along its route |
| `forget()` + `repair_leaf / repair_table_slot / repair_neighborhood` | The paper's failure repair procedures |
| `maintain()` | Periodic keep-alive round |
| `leave()` / `handle_leaving()` | Graceful departure (an addition) |

### 3. Advanced: algorithm, analysis, and differences from the paper

#### 3.1 Sharing the routing machinery (fairness)

Both overlays now inherit `OverlayNode`. A route is the same sequence of
`route` RPCs with immediate acks, the same 500 ms hop timeout, the same "skip
a dead hop and choose again" loop, the same `route_done` to the origin, and
the same hop and latency accounting. **Only `next_hop()` differs.** Any
measured difference in hops, latency or failure behaviour therefore comes
from the routing *rule* and the state it maintains, not from plumbing. To
prove the refactor didn't change Chord, a fingerprint (hash of 500 lookup
results with jitter, 2% loss, 10% crashes and maintenance running, plus all
message counts) was recorded before and after. Both hashes were
`86979fa6…`.

#### 3.2 Routing: the paper's Table 1

For key D at node A (with l = shl(D, A)):

1. **Leaf set.** If D lies between my farthest left and farthest right
   leaf, forward to the leaf (or me) numerically closest to D. If that's me,
   I'm the destination.
2. **Routing table.** Else, if R[l][D's digit l] exists, forward there. That
   node shares at least l + 1 digits with D.
3. **Rare case.** Else forward to any known T ∈ L ∪ R ∪ M with
   shl(T, D) ≥ l and |T − D| < |A − D|. (We pick the numerically closest such T.)

**Termination.** Each hop either lengthens the shared prefix (step 2) or
keeps it while getting numerically closer (steps 1 and 3), so no loops are
possible while state is consistent.

**Correctness rests on the leaf set.** Step 1 gives the exact owner if
leaf sets are correct. A test removed **70%** of all routing-table entries
and every lookup was still correct, just with more hops. Like Chord's
successor pointers, the leaf set is the invariant. The routing table is
only an accelerator.

**Why the rare case always has a candidate (equivalent mutant).** If D is
outside my leaf range, it lies beyond my farthest leaf on one side, so every
leaf on that side sits between me and D. Each of them is numerically closer
to D and shares at least my prefix with D. So a T that satisfies the
conditions always exists. That's why deleting the "numerically closer"
condition in a mutation test changed nothing: in reachable states, the
closest prefix-qualifying node is always closer than me anyway.

#### 3.3 Hops: why about log₁₆N

Each table hop fixes at least one more hex digit. After k hops the message is
at a node sharing k digits with the key. Only about N/16ᵏ nodes share k
digits with a given key, so after about log₁₆N hops it's among the
numerically nearest few nodes, and then the leaf set finishes in one hop.
**Measured** (3 seeds × 1,000 lookups, all 100% correct):

| N | log₁₆N | Pastry hops (uniform) | Chord hops (Phase 2) | Pastry state | Chord state |
|---|---|---|---|---|---|
| 16 | 1.00 | 0.93 | 2.14 | 15.0 | 9.5 |
| 256 | 2.00 | 1.93 | 4.26 | 49.8 | 13.6 |
| 1,024 | 2.50 | 2.48 | 5.27 | 59.8 | 15.6 |
| 4,096 | 3.00 | 2.91 | 6.28 | 65.4 | 17.6 |

- **Slope:** +0.98 hops from 256 → 4,096 nodes (16×), matching 1 hop per
  16× growth (test: 0.7–1.2).
- **Slightly below log₁₆N**, because the leaf set (16 nodes) finishes the
  route as soon as the key is within about 8 nodes, which saves part of the
  last digit. The paper also reports averages at or slightly below log₁₆N.
- **The trade-off:** Pastry uses about half Chord's hops but keeps about 3.7×
  as much state (65 vs 18 entries at N = 4,096). The table holds about
  15 × ⌈log₁₆N⌉ entries; a test confirms it's between 15·⌊log₁₆N⌋ and
  15·⌈log₁₆N⌉ at N = 1,000. With L = 16 and M = 32 on top, that's the paper's
  (2ᵇ − 1)·⌈log₂ᵇN⌉ + |L| + |M| bound.

#### 3.4 Locality: what proximity-aware tables buy

**Route stretch** = latency along the route ÷ direct latency (origin →
owner). 1.0 means routing costs nothing extra. Same nodes, same IDs, only the
table-filling rule differs (paired comparison):

| N | topology | optimal: latency / stretch | random: latency / stretch | Chord latency (Phase 2) |
|---|---|---|---|---|
| 1,024 | uniform | 123.0 ms / 1.47 | 179.9 ms / 3.60 | 325.7 ms |
| 4,096 | uniform | 120.5 ms / 1.42 | 201.8 ms / 4.38 | 380.0 ms |
| 1,024 | clustered | 90.3 ms / 1.39 | 142.4 ms / 6.37 | (Phase 5) |
| 4,096 | clustered | 91.6 ms / 1.32 | 161.4 ms / 7.26 | (Phase 5) |

(Latency includes the reply from owner to origin. Stretch counts the forward
route only.)

- **Hop counts are identical** in both modes. Locality changes *which* node
  fills a slot, not how many hops are needed.
- **Why it works.** Row 0 has about N/16 candidates per slot, so the closest
  one is very close. Deeper rows have exponentially fewer candidates, so
  later hops are longer, but there are few of them. The total is dominated
  by the last one or two hops, which is why latency barely grows with N
  (120 → 121 ms from 1,024 to 4,096).
- **Clustered networks gain most**: random choice sends early hops across
  continents (stretch 7.3), while proximity-aware choice keeps them local (1.3).
- **Compared to the paper:** it reports Pastry routes roughly 30–40% longer
  than the direct path in its Euclidean-plane simulations. Our 1.3–1.5 is in
  that range. *(Check the exact figure in the paper's locality section
  before quoting it in the report.)*
- **Pastry is about 3× faster than Chord at N = 4,096 on the uniform map.**
  Phase 5 will separate how much of that comes from fewer hops (log₁₆ vs
  ½·log₂) and how much from locality, using the homogeneous topology as the
  control.

#### 3.5 Join, and what it costs

1. Route a `join` message with key = my ID, starting at a **physically
   nearby** node A. At every node P on the route, `on_visit` adds P, P's
   neighbourhood set, and P's routing-table rows 0..shl(P, me) to the
   message. Those rows are usable by me because P shares that prefix with me.
2. The final node Z (numerically closest to me) adds its leaf set in `deliver`.
3. `consider()` everything: the leaf set gets the numerically nearest,
   each table slot the physically nearest, M the physically nearest.
4. *(Optional, `join_refine`)* Ask every node in R ∪ M for its state and keep
   closer nodes (the paper's locality improvement step).
5. Announce myself (plus my leaf set) to every node in L ∪ R ∪ M. They
   `consider()` me.

**Measured** (300 nodes joining one by one, clustered, seed 1):

| | messages/join (mean / last 50) | table complete | entry latency ÷ best possible | lookup latency |
|---|---|---|---|---|
| with refine | 125 / 144 | 99.4% | 1.038× | 102.8 ms |
| without refine | 50 / 57 | 99.4% | 1.045× | 102.9 ms |
| static "optimal" build, same nodes | — | 100% | 1.000× | 102.3 ms |

- **Join-built tables are almost optimal.** Only 4% worse per entry, and
  lookup latency is within 0.6 ms of the ideal build. Part of the reason is
  simplification #1 below: our nodes measure proximity for free, so they can
  judge *every* node they hear about. A real node would ping fewer
  candidates, so expect somewhat worse quality.
- **The refinement step isn't worth it here.** It costs 2.5× the messages
  for 0.7% better entries and no visible latency gain. We keep it on by
  default (it's in the paper) and Phase 5 can report both settings.
- **Join cost vs Chord:** Chord used about 41 messages per join while growing
  to 100 nodes and about 104 at N = 1,000 (Phase 2). Pastry without refine is
  similar; with refine it's more expensive. The paper's bound is
  O(log₂ᵇN) messages, but with a large constant (|L| + |M| + table size).

#### 3.6 Failure handling, and a problem we found and fixed

The paper's repair procedures, as implemented:
- **Leaf set:** when a leaf fails, ask the live leaf with the largest index
  on that side for its leaf set, and merge.
- **Routing table slot R[l][d]:** ask other nodes in row l for *their* R[l][d],
  then nodes in row l + 1 (we try up to 4 nodes). This is lazy: only done when
  a dead entry is noticed.
- **Neighbourhood set:** ask a remaining member for its neighbourhood set.
- **Keep-alive** (protocol not specified in the paper; ours): every ~1 s,
  swap leaf sets with the two immediate neighbours, ping one other leaf
  (round-robin), and ping one M member (round-robin).

**Problem: dead nodes never left the leaf sets.** After crashing 25% of 200
nodes, there were still 540 dead entries in leaf sets 60 s later, and the
number wasn't falling. *Diagnosis:* node A detects that X is dead and
removes it, then merges a neighbour's leaf set that still contains X (the
neighbour hasn't noticed yet), and X is back. Stale entries spread by gossip
faster than detection removes them. Chord doesn't suffer from this, because
`stabilize` *replaces* the successor list with a fresh copy each round
instead of merging into it.

*Fixes tried:*
1. Remember failed nodes for 30 s and refuse to re-learn them: 540 → 80,
   still not converging (the memory expires while others still gossip).
2. Unlimited memory: converges, but under message loss a *live* node that
   misses one ping would be locked out permanently. Rejected.
3. **Ping before insert (chosen):** a second-hand node may enter the leaf
   set only after it answers a ping (`check_and_add_leaves`). The 30 s memory is kept
   to avoid pinging the same dead node repeatedly. A wrongly suspected live
   node gets back in after one successful ping. Later Pastry versions use
   the same idea of probing before adding.
4. A leak remained: gossip received while repairing *table* or *M* slots went
   through `consider()`, which also updated the leaf set. Fixed with
   `consider(..., leaf=False)` for second-hand data.

**Result** (300 nodes, 25% crash, seed 1):

| time after crash | leaf-set errors | dead entries in M | dead entries in tables |
|---|---|---|---|
| 10 s | 181 | 1,299 | 653 |
| 30 s | 37 | 672 | 354 |
| 60 s | **0** | 227 | 229 |
| 120 s | 0 | 8 | 154 |
| 240 s | 0 | **0** | 152 (lazy: stays until used) |

Lookups were 100% successful and correct the whole time. Latency was 121 ms
before the crash, 527 ms immediately after (waiting out timeouts), and 130 ms
after 240 s. The remaining stale table entries occasionally cost a timeout.
**Maintenance comparison** (to be measured properly in Phase 5): Chord's
state was fully clean 30–60 s after a 25% crash. Pastry's leaf sets take
about 60 s, M takes 2–4 minutes, and routing tables never fully clean up
under lazy repair. This supports the "Chord has simpler maintenance" half of
the hypothesis, though so far only qualitatively.

#### 3.7 Complexity summary

| | Chord | Pastry (b = 4) |
|---|---|---|
| Hops | ≈ ½·log₂N | ≈ log₁₆N = ¼·log₂N |
| Routing state | O(log N): fingers + r + 1 | (2ᵇ−1)·log₂ᵇN + \|L\| + \|M\| |
| Join messages | O(log²N) | O(log₂ᵇN) with a large constant (+ refinement) |
| Correctness invariant | successor pointers | leaf sets |
| Locality | none | proximity-aware routing table |
| Repair | periodic stabilize / fix_fingers | leaf keep-alive + lazy table repair |

#### 3.8 Every simplification or difference from the paper

| # | Paper | Ours | Effect on the comparison |
|---|---|---|---|
| 1 | Proximity is *measured* (e.g. pings, RTT) | Read directly from the simulator (`network.latency`), free and exact | **Favours Pastry**: tables are higher quality, at no message cost. Stated as a limitation. |
| 2 | Joining node is assumed to know a nearby node (finding one is outside the paper) | The experiment harness gives it the physically nearest live node | **Favours Pastry** slightly: best-case bootstrap. |
| 3 | Nodes on the join route each send state to X | State is piggybacked on the join message itself | Fewer messages counted per join (one per hop fewer). |
| 4 | X takes row i from the i-th node on the route, M from A, L from Z | Every node contributes all rows 0..shl(P, X) **and** its M; Z adds L | More information per join, so tables are at least as good. |
| 5 | Second-stage locality step: request state from nodes in R and M | Same idea; full state requested from every node in R ∪ M, in parallel; optional (`join_refine`) | Measured: 2.5× messages, about 1% better entries. |
| 6 | After joining, X sends its full state to nodes in L ∪ R ∪ M | X sends its own ref + leaf set | Fewer bytes; same message count. |
| 7 | Neighbours exchange keep-alives (protocol unspecified) | Every ~1 s ± 50%: leaf-set swap with 2 neighbours, ping 1 leaf, ping 1 M member | Our design choice; sets Pastry's maintenance cost. |
| 8 | — (not discussed) | **Ping-before-insert** for second-hand leaf candidates, plus a 30 s failed-node memory | Needed for leaf sets to converge after crashes (§3.6). Costs extra pings under churn. |
| 9 | Table repair: ask same row, then row + 1, "casting a wider net" | Same, but at most 4 nodes asked per slot; no periodic table maintenance | Stale entries persist until used (as in the paper's lazy scheme). |
| 10 | M repair: ask other members | Ask the nearest remaining member | Simpler; M converges in 2–4 min. |
| 11 | All departures are treated as failures | Graceful `leave()` added: leaves are notified with replacements | Needed for the shared interface; repairs leaf sets at once. |
| 12 | Rare case: forward to *any* qualifying T | The numerically closest qualifying T | Deterministic; never worse. |
| 13 | Ties in "numerically closest" unspecified | Smaller ID wins, everywhere (nodes and oracle) | Only matters for exact ties (practically never at 128 bits). |
| 14 | Recursive routing | Recursive, with per-hop acks shared with Chord (`sim/overlay.py`) | Same mechanism for both overlays (fairness). |
| 15 | nodeId random or hash of IP / public key | SHA-1(`seed:address`), top 128 bits | Different ring per seed; no collisions expected. |
| 16 | Randomised routing to tolerate malicious nodes; security | Not implemented | Out of scope (crash-stop model). |
| 17 | Application API: `route`, `deliver`, `forward`, `newLeafs` upcalls | `route()`, `deliver()`, `on_visit()` (≈ `forward`); no `newLeafs` | Phase 4 adds what the KV store needs. |
| 18 | b, \|L\|, \|M\| are parameters (typical b = 4; \|L\| and \|M\| = 2ᵇ or 2·2ᵇ) | b = 4, \|L\| = 16, \|M\| = 32 defaults (configurable) | Matches typical values. |
| 19 | Message size matters in reality | Unlimited and free (join message grows with collected rows) | **Favours Pastry** slightly (see Phase 1 §3.5). |
| 20 | — | "Static optimal" builder (every slot = closest possible node) for large-N experiments | An idealisation. Justified by §3.5: join-built tables reached 1.04× optimal and the same lookup latency. |

### 4. How to run and test Phase 3

```bash
cd ~/chord-vs-pastry && source .venv/bin/activate

python -m pytest -v                               # expect 116 passed (~10 s)
python -m pytest tests/test_phase3_pastry.py -v   # just Pastry (31 tests)

python -m experiments.phase3_pastry_demo          # ~25 s
python -m experiments.phase3_pastry_demo --seeds 1 2 3 4 5 --sizes 256 1024 4096
```

What the 31 Phase 3 tests check:
- **ID arithmetic:** digits, shared prefix length, circular distance (with wrap).
- **Static correctness:** 1,000 lookups on 10 / 100 / 1,000 nodes in both
  table modes always reach the numerically closest node; boundary keys
  (on node IDs, ±1, midpoints, 0, 2¹²⁸ − 1) route correctly.
- **Cost:** hops within 0.4 of log₁₆N; +0.7 to +1.2 hops from 256 to 4,096
  nodes; latency = sum of link latencies; table size about 15·log₁₆N; static
  state is perfect.
- **Rare case and locality:** the rare case picks a valid node; lookups stay
  correct with 70% of table entries deleted; proximity-aware tables give the
  same hops but at least 20% lower latency (uniform and clustered).
- **Membership:** 2 nodes find each other; 120 sequential joins give correct
  leaf sets, more than 95% complete tables and near-optimal proximity; 40
  concurrent joins converge; join cost lies between 1× and 4× the state
  size; refine costs more than no refine.
- **Failures:** graceful leave fixes leaf sets without maintenance; lookups
  stay correct right after a 20% crash; leaf sets fully repair within 60 s
  after a 25% crash; M sets are clean after 240 s; a crashed node sends
  nothing.
- **Other:** same seed gives identical results; bad parameters are rejected.

**Mutation check:**

| Planted bug | Caught by |
|---|---|
| Table keeps the *farthest* node (locality reversed) | `test_sequential_joins_build_correct_and_local_state` |
| Leaf-set gossip accepted without ping | `test_leaf_sets_repair_after_mass_crash` |
| Leaf step ignores numeric closeness | `test_lookups_always_reach_numerically_closest_node` |
| Rare case drops "numerically closer" | Not caught: an *equivalent mutant* (§3.2 explains why it can't change behaviour) |

### Questions my professor might ask (Phase 3)

**Q1. How does Pastry route, and why about log₁₆N hops?**
Each hop goes to a node sharing at least one more leading hex digit with the
key. Only about N/16ᵏ nodes share k digits with the key, so after about
log₁₆N hops you're among its numerical neighbours, and the leaf set finishes
the route. We measured 2.91 hops at N = 4,096 (log₁₆N = 3).

**Q2. What are the leaf set, routing table and neighbourhood set for?**
The leaf set (numerically nearest nodes) guarantees correctness and handles
the final hop. The routing table (prefix-organised, filled with physically
close nodes) makes routes short and local. The neighbourhood set (physically
nearest nodes) helps keep locality during joins and repair. It's only used for
routing in the rare case.

**Q3. What is the "rare case"?**
The table slot for the next digit is empty (or its node has died). Then
forward to any known node at least as good in prefix and numerically closer
to the key. Progress is still guaranteed. Our routes stayed correct with 70%
of table entries removed.

**Q4. Why does correctness depend only on the leaf set?**
If the key is inside my leaf range, the numerically closest leaf *is* the
owner (all nodes in that range are in my leaf set). The table only decides
how fast we get there. It's the same principle as Chord's successor pointers
vs fingers.

**Q5. How does Pastry exploit locality, and how much does it help?**
Among all nodes that could fill a table slot, it keeps the physically
closest. Early hops (many candidates) become very short. Same hops, but
latency drops from 202 to 121 ms (uniform) and stretch from 7.3 to 1.3
(clustered) at N = 4,096.

**Q6. Pastry needs half the hops of Chord. What does it pay for that?**
State: about 65 entries per node vs about 18 at N = 4,096 (3.7×). Plus
heavier maintenance: three structures to keep fresh, slower cleanup after
crashes, and more complex join logic.

**Q7. What is route stretch?**
Route latency divided by direct latency between origin and destination. It
measures how much the overlay's detours cost. Pastry with proximity-aware
tables: 1.3–1.5. Without: 3.6–7.3.

**Q8. Dead nodes kept reappearing in your leaf sets. Why, and how did you fix it?**
Nodes merge each other's leaf sets, so a node that hadn't yet detected a
crash kept re-infecting nodes that had. We made nodes ping any second-hand
leaf candidate before inserting it, and briefly remember failed nodes. Chord
avoids the problem because stabilize replaces the successor list rather than
merging it.

**Q9. Your join-built tables are almost as good as perfect ones. Is that realistic?**
Partly not: our nodes measure latency for free and exactly, so they can
pick the best of every node they hear about. Real nodes ping fewer
candidates and get noisy measurements. It's listed as simplification #1
and as a limitation.

**Q10. Is the paper's second-stage join refinement worth it?**
Not in our simulation: 2.5× the join messages for about 1% better table
entries and no measurable latency gain, because our first stage already
finds near-optimal entries.

**Q11. What happens when a routing-table entry points to a dead node?**
A lookup that tries it times out (500 ms), forgets the entry, picks another
hop, and starts a lazy repair (ask nodes in the same row, then the next row).
Entries that are never used stay stale. We counted 152 still there 4 minutes
after a 25% crash.

**Q12. List your simplifications.**
See the table in §3.8. The ones that favour Pastry: free and exact proximity
measurement, best-case bootstrap, free large messages, and piggybacked join
state. The ones we added: graceful leave, a keep-alive protocol,
ping-before-insert, and bounded table repair.

---

## Glossary

| Term | Meaning |
|---|---|
| **Node / peer** | One participating computer. In P2P, every node is both client and server. |
| **Peer-to-peer (P2P)** | A system with no central server; nodes cooperate as equals. |
| **Overlay network** | A "virtual" network built on top of the real one: node A's neighbours are chosen by the algorithm, not by physical cables. |
| **Structured overlay** | An overlay whose links are determined by node IDs (Chord, Pastry), so lookups take a bounded number of hops. *Unstructured* overlays (Gnutella) use flooding. |
| **DHT (Distributed Hash Table)** | A structured overlay that answers "which node is responsible for key k?" with no central directory. |
| **Key** | The name of a piece of data (e.g. `"Dune"`), hashed into an ID. |
| **Identifier (ID) / ID space** | A number in `[0, 2^m)` given to every node and key. Chord: m-bit; Pastry: 128-bit. |
| **Hash function** | A function that turns any input into a fixed-size, random-looking number. Same input, same output. |
| **SHA-1** | The 160-bit hash function used by both papers. |
| **Consistent hashing** | Placing nodes and keys on the same ring so that adding or removing a node moves only about K/N keys. |
| **Ring / identifier circle** | The ID space with 2^m − 1 wrapping round to 0. |
| **Successor / predecessor** | The next / previous node clockwise / anticlockwise on the ring. In Chord, a key belongs to its successor. |
| **Hop** | One message forwarded from one node to the next during a lookup. |
| **Routing table** | The small "address book" a node keeps so it can forward lookups. |
| **Lookup** | Finding the node responsible for a key. |
| **Churn** | Nodes continually joining, leaving and crashing. |
| **Big-O, O(log N)** | How a cost grows with N, ignoring constants. O(log N) means doubling N adds only a constant amount. |
| **Load balance** | How evenly keys are spread across nodes. |
| **Virtual node** | One physical node holding several IDs on the ring, to even out load. |
| **Proximity / locality** | How physically close (low-latency) two nodes are. Pastry uses this; Chord does not. |
| **Discrete-event simulation** | A simulation that jumps from event to event (e.g. "message arrives at t=12.3 ms") instead of ticking a real clock. |
| **Seed** | The starting number for a random generator. Same seed, same sequence. |
| **Single point of failure** | One component whose failure brings down the whole system. |
| **Event queue** | The simulator's to-do list of future events, always processed earliest first. |
| **simpy process** | A Python generator run by simpy. Each `yield` pauses it until an event (a timeout, a reply) happens in simulated time. |
| **Generator / `yield` / `yield from`** | A Python function that can pause and resume. `yield from other()` runs another generator inside this one and gets its return value. |
| **Latency** | Time for one message to travel from sender to receiver (one-way). |
| **RTT (round-trip time)** | Time to send a request and get the reply: about 2 × latency. |
| **Jitter** | Random variation added to latency. |
| **Message loss** | A message silently disappearing in the network. |
| **RPC (remote procedure call)** | Send a request to another node and wait for its reply, like calling a function on another machine. |
| **Timeout** | Giving up on a reply after a fixed wait. The only way to suspect a failure. |
| **Crash-stop failure** | A node stops suddenly and permanently, without telling anyone. |
| **Failure detector** | The mechanism (here: timeouts) by which nodes suspect others have failed. It can be wrong. |
| **Topology** | The "physical" layout of nodes, which determines link latencies. |
| **Euclidean plane model** | Nodes as points in 2-D; latency = straight-line distance. Used in the Pastry paper. |
| **Triangle inequality** | d(a,c) ≤ d(a,b) + d(b,c): a detour is never shorter than the direct path. |
| **Heterogeneous network** | One where link latencies differ greatly (e.g. intra- vs inter-continent). |
| **Control (experiment)** | A setting where the effect being tested *can't* occur, used to rule out other explanations. |
| **Percentile (p50, p95)** | The value below which that % of measurements fall. p95 shows the "slow tail". |
| **Message tag** | Our label for *why* a message was sent (lookup / join / maintenance), used to count costs per purpose. |
| **Paired comparison** | Comparing two systems on exactly the same inputs (same seed, same node positions) so random luck cancels out. |
| **Finger table** | Chord's shortcut list: finger[i] = the first node at or after (my ID + 2ⁱ). About log₂N distinct entries. |
| **Closest preceding node** | The known node that is nearest to the key *without passing it*: Chord's next hop. |
| **Successor list** | The next r nodes clockwise. Lets Chord survive the crash of its immediate successor. |
| **Stabilization** | Chord's periodic repair protocol: `stabilize` + `notify` fix successor/predecessor pointers. |
| **fix_fingers / check_predecessor** | Periodic tasks that refresh one finger per call / clear a dead predecessor. |
| **Bootstrap node** | Any existing member a new node contacts in order to join. |
| **Recursive routing** | Each hop forwards the request onward; the final node answers the origin directly. |
| **Iterative routing** | The origin contacts every hop itself and is told the next hop each time. |
| **Ack (acknowledgement)** | A short "got it" reply; lets the sender detect a dead next hop by timeout. |
| **Invariant** | A property the protocol must keep true (Chord: every node's successor pointer is correct). |
| **Convergence** | Repair protocols bringing all routing state back to correct after changes stop. |
| **Oracle / ground truth** | Test code with a global view that knows the true answer, used to check lookups. |
| **Static ring** | A ring whose state was filled in perfectly by the test harness, used to measure routing in isolation. |
| **Mutation testing** | Deliberately inserting a bug to check that the tests notice it. |
| **Base 2ᵇ digits** | Pastry writes IDs as digits in base 2ᵇ. With b = 4 that's hex (16 values per digit), 32 digits for 128 bits. |
| **Shared prefix length (shl)** | How many leading digits two IDs have in common. |
| **Prefix routing** | Routing that fixes one more leading digit of the destination ID at each hop (Pastry, Tapestry). |
| **Numerically closest** | Pastry's owner rule: the node whose ID has the smallest (circular) difference from the key. |
| **Leaf set** | Pastry's \|L\| numerically nearest nodes (half smaller, half larger). Guarantees correct delivery. |
| **Routing table (Pastry)** | Row l, column d holds a node sharing l digits with me whose next digit is d. |
| **Neighbourhood set** | Pastry's \|M\| physically nearest known nodes, used to maintain locality. |
| **Proximity metric** | The measure of "physically close" (here: network latency). |
| **Proximity neighbour selection** | Filling each routing-table slot with the physically closest qualifying node. |
| **Rare case** | Pastry's fallback when the needed table slot is empty: any node at least as good in prefix and numerically closer. |
| **Route stretch** | Route latency ÷ direct latency between the same two nodes. 1.0 means no detour cost. |
| **Lazy repair** | Fixing a stale entry only when it's actually used and found dead. |
| **Keep-alive** | A periodic small message to check that a neighbour is still alive. |
| **Gossip (here)** | Learning about nodes second-hand from another node's state. May be stale. |
| **Ping-before-insert** | Only adding a second-hand node after it answers a ping, to stop stale gossip. |
| **Piggybacking** | Carrying extra data on a message that's being sent anyway. |
| **Equivalent mutant** | A planted code change that can't alter behaviour, so no test can catch it. |
