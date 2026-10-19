"""Random number generators for the simulation.

Every part of the simulation (network, churn, each node ...) gets its own
random.Random object made from the main seed plus a name. So running with the
same seed always gives the same results, and adding random calls in one part
does not change the numbers in another part.
"""

import hashlib
import random


def derive_seed(master_seed, stream):
    # Hash "seed:name" with sha256 and use the first 8 bytes as the new seed.
    # (Python's hash() changes every run for strings, so we can't use it.)
    text = (str(master_seed) + ":" + stream).encode("utf-8")
    digest = hashlib.sha256(text).digest()
    return int.from_bytes(digest[:8], "big")


def make_rng(master_seed, stream):
    """Return a random.Random for one part of the simulation.
    Example: make_rng(42, "network") and make_rng(42, "churn")."""
    return random.Random(derive_seed(master_seed, stream))
