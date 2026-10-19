"""Metrics collector used by both Chord and Pastry.

We use one collector for both overlays so that messages, hops and latency are
counted in exactly the same way. The network calls on_send() etc. for every
message, and the overlays call record_lookup() after every lookup.

Messages are counted by kind (like "route" or "route.reply") and by tag,
which says why the message was sent ("lookup", "join", "maintenance" ...).
"""

import math

ROUTING_TABLE_SIZE = "routing_table_size"
KEYS_PER_NODE = "keys_per_node"


def mean(values):
    values = list(values)
    # fsum adds floats more accurately than sum()
    return math.fsum(values) / len(values)


def std(values):
    """Population standard deviation."""
    values = list(values)
    m = mean(values)
    total = 0.0
    for v in values:
        total += (v - m) ** 2
    return math.sqrt(total / len(values))


def percentile(values, q):
    """q-th percentile with linear interpolation (same as numpy's default).
    For example the 95th percentile of 1..100 is 95.05."""
    xs = sorted(values)
    if len(xs) == 0:
        return math.nan
    if q < 0 or q > 100:
        raise ValueError("q must be in [0, 100]")
    pos = (len(xs) - 1) * q / 100.0
    lo = math.floor(pos)
    hi = math.ceil(pos)
    return xs[lo] + (xs[hi] - xs[lo]) * (pos - lo)


def summarize(values):
    """count, mean, std, min, p50, p95, p99 and max of a list of numbers."""
    xs = list(values)
    if len(xs) == 0:
        result = {"count": 0}
        for name in ["mean", "std", "min", "p50", "p95", "p99", "max"]:
            result[name] = math.nan
        return result
    return {
        "count": len(xs),
        "mean": mean(xs),
        "std": std(xs),
        "min": min(xs),
        "p50": percentile(xs, 50),
        "p95": percentile(xs, 95),
        "p99": percentile(xs, 99),
        "max": max(xs),
    }


class LookupRecord:
    """Information about one lookup.

    owner is the address of the node that the overlay says is responsible
    for the key (None if the lookup failed). correct is filled in later by
    test/experiment code that knows the real owner.
    """

    def __init__(self, overlay, origin, key, start_time, end_time, hops, owner,
                 success, correct=None, path=()):
        self.overlay = overlay
        self.origin = origin
        self.key = key
        self.start_time = start_time
        self.end_time = end_time
        self.hops = hops
        self.owner = owner
        self.success = success
        self.correct = correct
        self.path = path
        self.latency = end_time - start_time   # in ms


class Metrics:
    def __init__(self):
        self.sent_by_kind = {}
        self.sent_by_tag = {}
        self.delivered = 0
        self.lost = 0           # lost because of the loss rate
        self.dropped_dead = 0   # reached a node that was dead
        self.lookups = []
        self.distributions = {}

    # these are called by the Network
    def on_send(self, msg):
        self.sent_by_kind[msg.kind] = self.sent_by_kind.get(msg.kind, 0) + 1
        self.sent_by_tag[msg.tag] = self.sent_by_tag.get(msg.tag, 0) + 1

    def on_deliver(self, msg):
        self.delivered += 1

    def on_lost(self, msg):
        self.lost += 1

    def on_dropped_dead(self, msg):
        self.dropped_dead += 1

    def total_sent(self):
        return sum(self.sent_by_kind.values())

    def count_tag(self, tag):
        """How many messages were sent with this tag so far."""
        return self.sent_by_tag.get(tag, 0)

    # these are called by the overlays / experiments
    def record_lookup(self, record):
        self.lookups.append(record)

    def record_distribution(self, name, values):
        self.distributions[name] = list(values)

    def lookup_summary(self):
        """Hops and latency are averaged only over successful lookups,
        failed ones are counted in success_rate."""
        n = len(self.lookups)
        ok = [r for r in self.lookups if r.success]
        checked = [r for r in ok if r.correct is not None]
        hops = summarize([r.hops for r in ok])
        lat = summarize([r.latency for r in ok])
        if n > 0:
            success_rate = len(ok) / n
        else:
            success_rate = math.nan
        if len(checked) > 0:
            correct_rate = sum(r.correct for r in checked) / len(checked)
        else:
            correct_rate = math.nan
        return {
            "lookups": n,
            "success_rate": success_rate,
            "correct_rate": correct_rate,
            "hops_mean": hops["mean"],
            "hops_p95": hops["p95"],
            "latency_mean": lat["mean"],
            "latency_p95": lat["p95"],
        }

    def reset(self):
        self.sent_by_kind = {}
        self.sent_by_tag = {}
        self.delivered = 0
        self.lost = 0
        self.dropped_dead = 0
        self.lookups = []
        self.distributions = {}
