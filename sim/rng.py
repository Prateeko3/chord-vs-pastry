"""Reproducible randomness for the whole project.

WHY THIS FILE EXISTS
--------------------
Experiments must be repeatable: if you run the same experiment with the same
seed twice, you must get *exactly* the same numbers. That only works if no
code ever calls the global `random.random()` (whose state is shared and easy to
disturb). Instead, every component asks this module for its *own* random
generator, derived from one master seed.

WHY "NAMED STREAMS"?
--------------------
Imagine the network jitter and the churn generator shared one generator. If you
later add one extra jitter draw, every churn event after it changes too, and you
can no longer compare "with jitter" vs "without jitter" fairly. Giving each
component its own stream (e.g. "network", "churn", "workload") keeps them
independent: changing how one component uses randomness does not disturb the
others.
"""

from __future__ import annotations

import hashlib
import random


def derive_seed(master_seed: int, stream: str) -> int:
    """Turn (master_seed, stream name) into a new, well-mixed integer seed.

    We hash the two together with SHA-256 so that streams "network" and
    "churn" get unrelated seeds even though they come from the same master.
    Python's built-in hash() is NOT used because it is randomised per process
    for strings, which would break reproducibility.
    """
    text = f"{master_seed}:{stream}".encode("utf-8")
    digest = hashlib.sha256(text).digest()
    # Use the first 8 bytes (64 bits) of the digest as the new seed.
    return int.from_bytes(digest[:8], "big")


def make_rng(master_seed: int, stream: str) -> random.Random:
    """Return an independent `random.Random` for one named component.

    Example:
        net_rng = make_rng(42, "network")
        churn_rng = make_rng(42, "churn")
    """
    return random.Random(derive_seed(master_seed, stream))
