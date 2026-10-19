"""Phase 0 tests: libraries are installed, the random seeds work, and the
hashing / ring interval functions are correct."""

import importlib

import pytest

from sim.ids import hash_to_id, in_interval, sha1_int
from sim.rng import make_rng


# ----- environment -----

@pytest.mark.parametrize("module", ["simpy", "pandas", "matplotlib", "numpy"])
def test_dependencies_import(module):
    importlib.import_module(module)


# ----- random numbers -----

def test_same_seed_same_numbers():
    a = make_rng(42, "network")
    b = make_rng(42, "network")
    assert [a.random() for _ in range(5)] == [b.random() for _ in range(5)]


def test_different_streams_are_independent():
    a = make_rng(42, "network")
    b = make_rng(42, "churn")
    assert [a.random() for _ in range(5)] != [b.random() for _ in range(5)]


def test_different_seeds_differ():
    assert make_rng(1, "x").random() != make_rng(2, "x").random()


# ----- hashing -----

def test_sha1_known_value():
    # known SHA-1 value of "abc" (standard test vector)
    assert sha1_int("abc") == 0xA9993E364706816ABA3E25717850C26C9CD0D89D


def test_hash_to_id_keeps_top_bits_and_stays_in_range():
    full = sha1_int("node-1")
    for bits in (1, 8, 32, 128, 160):
        v = hash_to_id("node-1", bits)
        assert 0 <= v < 2 ** bits
        assert v == full >> (160 - bits)


def test_hash_to_id_rejects_bad_bits():
    with pytest.raises(ValueError):
        hash_to_id("x", 0)
    with pytest.raises(ValueError):
        hash_to_id("x", 161)


def test_ids_spread_roughly_evenly():
    # 16000 names into 16 buckets, each should get about 1000 (we allow 15%)
    counts = [0] * 16
    for i in range(16000):
        counts[hash_to_id(f"key-{i}", 4)] += 1
    assert all(850 <= c <= 1150 for c in counts), counts


# ----- ring intervals -----

@pytest.mark.parametrize("x,a,b,expected", [
    (5, 2, 10, True),
    (2, 2, 10, False),    # left end not included
    (10, 2, 10, False),   # right end not included
    (62, 60, 4, True),    # goes past 0
    (0, 60, 4, True),
    (30, 60, 4, False),
    (7, 7, 7, False),     # (a, a) is the whole ring except a
    (8, 7, 7, True),
])
def test_in_interval_open(x, a, b, expected):
    assert in_interval(x, a, b) is expected


def test_in_interval_inclusive_ends():
    assert in_interval(10, 2, 10, inclusive_right=True)
    assert in_interval(2, 2, 10, inclusive_left=True)
    assert in_interval(4, 60, 4, inclusive_right=True)
    assert in_interval(7, 7, 7, inclusive_right=True)  # (a, a] is the whole ring


# ----- consistent hashing demo -----

def test_consistent_hashing_moves_far_fewer_keys():
    from experiments.phase0_consistent_hashing_demo import run
    n = 10
    results = [run(n_servers=n, n_keys=2000, seed=s) for s in range(100)]
    mean_mod = sum(r["moved_modulo_pct"] for r in results) / len(results)
    mean_ring = sum(r["moved_consistent_pct"] for r in results) / len(results)
    ideal = 100.0 / (n + 1)
    # modulo hashing moves almost all keys (N/(N+1), about 91%)
    assert mean_mod > 85
    # consistent hashing moves about 1/(N+1) of the keys. One ring can be
    # lucky or unlucky so we compare the average over 100 rings.
    assert abs(mean_ring - ideal) < 2.5, (mean_ring, ideal)
