"""ONE metrics collector shared by both overlays.

Why shared? If Chord and Pastry each counted their own hops and messages, a
difference in the results might come from a difference in *counting*, not in
the algorithms. With a single collector, the network counts every message the
same way no matter who sent it, and both overlays report lookups through the
same `record_lookup` call.

What is collected
-----------------
- Messages: every message the network carries, counted by `kind` (e.g.
  "find_successor", "find_successor.reply") and by `tag`, a label for *why*
  it was sent (e.g. "lookup", "join", "maintenance"). Also how many were lost
  by the network and how many were sent to a node that had crashed.
- Lookups: one `LookupRecord` per lookup (hops, latency, success, ...).
- Distributions: any per-node quantity sampled at a moment in time, e.g.
  routing-table size or keys stored per node.

`summarize()` turns a list of numbers into count/mean/std/percentiles.
"""

from __future__ import annotations

import math
import statistics
from collections import Counter
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Iterable

if TYPE_CHECKING:  # only for type hints; avoids a circular import at runtime
    from sim.network import Message

# Standard names for distributions, so both overlays use the same spelling.
ROUTING_TABLE_SIZE = "routing_table_size"
KEYS_PER_NODE = "keys_per_node"


def percentile(values: Iterable[float], q: float) -> float:
    """The q-th percentile (0 <= q <= 100) using linear interpolation.

    This is the same definition as numpy's default, written out so you can
    see what it does: sort the values, find the (fractional) position
    (n-1) * q/100, and interpolate between the two neighbouring values.
    Example: p95 of 1..100 is 95.05.
    """
    xs = sorted(values)
    if not xs:
        return math.nan
    if not 0 <= q <= 100:
        raise ValueError("q must be in [0, 100]")
    pos = (len(xs) - 1) * q / 100.0
    lo = math.floor(pos)
    hi = math.ceil(pos)
    return xs[lo] + (xs[hi] - xs[lo]) * (pos - lo)


def summarize(values: Iterable[float]) -> dict[str, float]:
    """Describe a list of numbers. `std` is the population standard deviation
    (we describe *this* set of values, not estimate a wider population).
    Empty input gives count 0 and NaN everywhere else."""
    xs = list(values)
    if not xs:
        nan = math.nan
        return {"count": 0, "mean": nan, "std": nan, "min": nan, "p50": nan,
                "p95": nan, "p99": nan, "max": nan}
    return {
        "count": len(xs),
        "mean": statistics.fmean(xs),
        "std": statistics.pstdev(xs),
        "min": min(xs),
        "p50": percentile(xs, 50),
        "p95": percentile(xs, 95),
        "p99": percentile(xs, 99),
        "max": max(xs),
    }


@dataclass
class LookupRecord:
    """Everything we want to know about one lookup.

    owner   address of the node the overlay *claims* is responsible for the key
            (None if the lookup failed).
    success the overlay produced an answer before giving up.
    correct the answer matched the ground truth (filled in by experiment code
            that knows the true owner; None if nobody checked).
    """

    overlay: str
    origin: int
    key: str
    start_time: float
    end_time: float
    hops: int
    owner: int | None
    success: bool
    correct: bool | None = None
    path: tuple[int, ...] = ()

    @property
    def latency(self) -> float:
        """Simulated time from issuing the lookup to getting the answer (ms)."""
        return self.end_time - self.start_time


@dataclass
class Metrics:
    """Counters and records for one simulation run."""

    sent_by_kind: Counter = field(default_factory=Counter)
    sent_by_tag: Counter = field(default_factory=Counter)
    delivered: int = 0
    lost: int = 0           # dropped at random by the network (loss_rate)
    dropped_dead: int = 0   # arrived at a node that had crashed or left
    lookups: list[LookupRecord] = field(default_factory=list)
    distributions: dict[str, list[float]] = field(default_factory=dict)

    # --- called by the Network for every message -------------------------
    def on_send(self, msg: "Message") -> None:
        self.sent_by_kind[msg.kind] += 1
        self.sent_by_tag[msg.tag] += 1

    def on_deliver(self, msg: "Message") -> None:
        self.delivered += 1

    def on_lost(self, msg: "Message") -> None:
        self.lost += 1

    def on_dropped_dead(self, msg: "Message") -> None:
        self.dropped_dead += 1

    @property
    def total_sent(self) -> int:
        return sum(self.sent_by_kind.values())

    # --- called by overlays / experiments --------------------------------
    def record_lookup(self, record: LookupRecord) -> None:
        self.lookups.append(record)

    def record_distribution(self, name: str, values: Iterable[float]) -> None:
        """Store a snapshot of a per-node quantity (replaces any earlier one
        with the same name)."""
        self.distributions[name] = list(values)

    def lookup_summary(self) -> dict[str, float]:
        """Headline lookup numbers. Hops and latency are averaged over
        *successful* lookups only (a failed lookup has no meaningful latency);
        failures show up in success_rate instead."""
        n = len(self.lookups)
        ok = [r for r in self.lookups if r.success]
        checked = [r for r in ok if r.correct is not None]
        hops = summarize(r.hops for r in ok)
        lat = summarize(r.latency for r in ok)
        return {
            "lookups": n,
            "success_rate": len(ok) / n if n else math.nan,
            "correct_rate": (sum(r.correct for r in checked) / len(checked)
                             if checked else math.nan),
            "hops_mean": hops["mean"], "hops_p95": hops["p95"],
            "latency_mean": lat["mean"], "latency_p95": lat["p95"],
        }

    def reset(self) -> None:
        """Forget everything (e.g. after a warm-up period, before measuring)."""
        self.sent_by_kind.clear()
        self.sent_by_tag.clear()
        self.delivered = self.lost = self.dropped_dead = 0
        self.lookups.clear()
        self.distributions.clear()
