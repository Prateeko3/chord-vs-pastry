"""Turning names and keys into numeric identifiers (consistent hashing basics).

Both Chord and Pastry give every node and every key an identifier (ID) taken
from the same huge number space, by running a cryptographic hash (SHA-1) over
something: a node's address, or a key's name.

- Chord uses an m-bit space: IDs are 0 .. 2**m - 1 arranged on a circle.
- Pastry uses 128-bit IDs written in base 2**b (b = 4 -> hexadecimal digits).

SHA-1 outputs 160 bits. To get an m-bit ID we keep the top m bits. Because
SHA-1's output looks uniformly random, IDs spread evenly around the space,
which is what gives DHTs their load balance.

Note: SHA-1 is no longer considered secure for cryptography, but here we only
need "spreads inputs evenly and deterministically", which it still does. We
use it because both original papers do.
"""

from __future__ import annotations

import hashlib

SHA1_BITS = 160


def sha1_int(data: str | bytes) -> int:
    """Return the full 160-bit SHA-1 hash of `data` as a Python integer."""
    if isinstance(data, str):
        data = data.encode("utf-8")
    return int.from_bytes(hashlib.sha1(data).digest(), "big")


def hash_to_id(data: str | bytes, bits: int) -> int:
    """Hash `data` into an identifier in the range [0, 2**bits).

    We keep the most significant `bits` bits of the SHA-1 digest (shifting
    away the rest). For bits=160 this is the full digest.
    """
    if not 1 <= bits <= SHA1_BITS:
        raise ValueError(f"bits must be between 1 and {SHA1_BITS}, got {bits}")
    return sha1_int(data) >> (SHA1_BITS - bits)


def in_interval(x: int, a: int, b: int, *, inclusive_right: bool = False,
                inclusive_left: bool = False) -> bool:
    """Is x inside the interval from a to b going *clockwise* around the ring?

    On a ring, the interval (a, b) "wraps around" when a > b. For example on a
    ring of size 64, the interval (60, 4) contains 61, 62, 63, 0, 1, 2, 3.
    Chord uses this test constantly ("is the key between me and my
    successor?"), so we write it once here and test it carefully.

    By default both ends are open: (a, b). Set inclusive_right for (a, b],
    inclusive_left for [a, b).

    Special case a == b: the open interval (a, a) means "the whole ring
    except a" (this is how Chord treats a ring with a single node).
    """
    if inclusive_left and x == a:
        return True
    if inclusive_right and x == b:
        return True
    if a == b:
        return x != a
    if a < b:
        return a < x < b
    # Wrapping case: a > b, the interval passes through 0.
    return x > a or x < b
