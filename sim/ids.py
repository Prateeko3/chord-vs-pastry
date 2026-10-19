"""Hashing names/keys to IDs, and the ring interval check used by Chord.

Both papers use SHA-1. SHA-1 gives 160 bits; if we want an m-bit ID we keep
the top m bits. Chord uses m-bit IDs on a ring, Pastry uses 128-bit IDs.
(SHA-1 is not secure anymore but we only need it to spread IDs evenly.)
"""

import hashlib

SHA1_BITS = 160


def sha1_int(data):
    """SHA-1 of a string (or bytes) as a big integer."""
    if isinstance(data, str):
        data = data.encode("utf-8")
    return int.from_bytes(hashlib.sha1(data).digest(), "big")


def hash_to_id(data, bits):
    """Hash data to an ID between 0 and 2^bits - 1 (keeps the top bits)."""
    if bits < 1 or bits > SHA1_BITS:
        raise ValueError("bits must be between 1 and 160, got " + str(bits))
    return sha1_int(data) >> (SHA1_BITS - bits)


def in_interval(x, a, b, inclusive_right=False, inclusive_left=False):
    """Check if x is between a and b going clockwise on the ring.

    By default the interval is open (a, b). If a > b the interval wraps
    around zero, e.g. on a ring of size 64, (60, 4) = {61, 62, 63, 0, 1, 2, 3}.
    If a == b, (a, a) means the whole ring except a (Chord uses this when
    there is only one node).
    """
    if inclusive_left and x == a:
        return True
    if inclusive_right and x == b:
        return True
    if a == b:
        return x != a
    if a < b:
        return a < x < b
    # a > b so the interval goes past 0
    return x > a or x < b
