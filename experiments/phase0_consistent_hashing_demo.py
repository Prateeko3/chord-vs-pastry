"""Phase 0 demo: why DHTs use *consistent* hashing instead of `hash % N`.

We place K keys on N servers using two strategies, then add ONE server and
count how many keys have to move to a different server.

  1. Naive modulo hashing:   server = hash(key) % N
  2. Consistent hashing:     put servers and keys on a ring of IDs; each key
                             belongs to the first server clockwise from it
                             (its "successor"), exactly as in Chord.

Run:
    python -m experiments.phase0_consistent_hashing_demo
    python -m experiments.phase0_consistent_hashing_demo --servers 100 --keys 50000 --seed 7
"""

from __future__ import annotations

import argparse
import bisect

from sim.ids import hash_to_id
from sim.rng import make_rng

ID_BITS = 32  # a small ring is plenty for a demo


def assign_modulo(keys: list[str], n_servers: int) -> dict[str, int]:
    """Naive strategy: server index = hash(key) mod N."""
    return {k: hash_to_id(k, ID_BITS) % n_servers for k in keys}


def assign_consistent(keys: list[str], server_names: list[str]) -> dict[str, str]:
    """Ring strategy: each key goes to the first server ID >= the key's ID,
    wrapping around to the smallest server ID if needed."""
    ring = sorted((hash_to_id(s, ID_BITS), s) for s in server_names)
    ring_ids = [rid for rid, _ in ring]
    owner = {}
    for k in keys:
        kid = hash_to_id(k, ID_BITS)
        i = bisect.bisect_left(ring_ids, kid)   # first server ID >= kid
        owner[k] = ring[i % len(ring)][1]       # % len wraps past the top of the ring
    return owner


def run(n_servers: int, n_keys: int, seed: int) -> dict[str, float]:
    rng = make_rng(seed, "phase0-demo")
    # Random key names (seeded, so the run is reproducible).
    keys = [f"key-{rng.getrandbits(64):016x}" for _ in range(n_keys)]
    # Server names also come from the seed, so each seed gives a different
    # ring layout (otherwise every seed would test the same ring).
    servers = [f"server-{rng.getrandbits(64):016x}" for _ in range(n_servers + 1)]
    new_server = servers.pop()

    before_mod = assign_modulo(keys, n_servers)
    after_mod = assign_modulo(keys, n_servers + 1)
    moved_mod = sum(before_mod[k] != after_mod[k] for k in keys)

    before_ring = assign_consistent(keys, servers)
    after_ring = assign_consistent(keys, servers + [new_server])
    moved_ring = sum(before_ring[k] != after_ring[k] for k in keys)

    return {
        "moved_modulo_pct": 100.0 * moved_mod / n_keys,
        "moved_consistent_pct": 100.0 * moved_ring / n_keys,
        "ideal_pct": 100.0 / (n_servers + 1),  # the new server's fair share
    }


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--servers", type=int, default=10)
    p.add_argument("--keys", type=int, default=20000)
    p.add_argument("--seed", type=int, default=42)
    a = p.parse_args()

    r = run(a.servers, a.keys, a.seed)
    print(f"Adding 1 server to {a.servers} servers holding {a.keys} keys (seed={a.seed}):")
    print(f"  modulo hashing     : {r['moved_modulo_pct']:6.2f}% of keys moved")
    print(f"  consistent hashing : {r['moved_consistent_pct']:6.2f}% of keys moved")
    print(f"  ideal (1/(N+1))    : {r['ideal_pct']:6.2f}%")


if __name__ == "__main__":
    main()
