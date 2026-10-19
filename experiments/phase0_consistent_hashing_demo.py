"""Phase 0 demo: why DHTs use consistent hashing and not hash % N.

We put K keys on N servers in two ways, then add one server and count how
many keys have to move:
  1. modulo hashing:      server = hash(key) % N
  2. consistent hashing:  servers and keys on a ring, each key goes to the
                          first server clockwise from it (like Chord)

Run:
    python -m experiments.phase0_consistent_hashing_demo
    python -m experiments.phase0_consistent_hashing_demo --servers 100 --keys 50000 --seed 7
"""

import argparse
import bisect

from sim.ids import hash_to_id
from sim.rng import make_rng

ID_BITS = 32   # small ring is enough for this demo


def assign_modulo(keys, n_servers):
    """server number = hash(key) mod N"""
    owner = {}
    for k in keys:
        owner[k] = hash_to_id(k, ID_BITS) % n_servers
    return owner


def assign_consistent(keys, server_names):
    """Each key goes to the first server with ID >= key ID (wraps around)."""
    ring = []
    for s in server_names:
        ring.append((hash_to_id(s, ID_BITS), s))
    ring.sort()
    ring_ids = [r[0] for r in ring]
    owner = {}
    for k in keys:
        kid = hash_to_id(k, ID_BITS)
        i = bisect.bisect_left(ring_ids, kid)
        owner[k] = ring[i % len(ring)][1]   # % len(ring) goes back to the start
    return owner


def run(n_servers, n_keys, seed):
    rng = make_rng(seed, "phase0-demo")
    keys = []
    for i in range(n_keys):
        keys.append(f"key-{rng.getrandbits(64):016x}")
    # server names also depend on the seed, so each seed gives a different ring
    servers = []
    for i in range(n_servers + 1):
        servers.append(f"server-{rng.getrandbits(64):016x}")
    new_server = servers.pop()

    before_mod = assign_modulo(keys, n_servers)
    after_mod = assign_modulo(keys, n_servers + 1)
    moved_mod = 0
    for k in keys:
        if before_mod[k] != after_mod[k]:
            moved_mod += 1

    before_ring = assign_consistent(keys, servers)
    after_ring = assign_consistent(keys, servers + [new_server])
    moved_ring = 0
    for k in keys:
        if before_ring[k] != after_ring[k]:
            moved_ring += 1

    return {
        "moved_modulo_pct": 100.0 * moved_mod / n_keys,
        "moved_consistent_pct": 100.0 * moved_ring / n_keys,
        "ideal_pct": 100.0 / (n_servers + 1),   # fair share of the new server
    }


def main():
    parser = argparse.ArgumentParser(description="Phase 0 demo: modulo vs consistent hashing")
    parser.add_argument("--servers", type=int, default=10)
    parser.add_argument("--keys", type=int, default=20000)
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()

    r = run(args.servers, args.keys, args.seed)
    print(f"Adding 1 server to {args.servers} servers holding {args.keys} keys (seed={args.seed}):")
    print(f"  modulo hashing     : {r['moved_modulo_pct']:6.2f}% of keys moved")
    print(f"  consistent hashing : {r['moved_consistent_pct']:6.2f}% of keys moved")
    print(f"  ideal (1/(N+1))    : {r['ideal_pct']:6.2f}%")


if __name__ == "__main__":
    main()
