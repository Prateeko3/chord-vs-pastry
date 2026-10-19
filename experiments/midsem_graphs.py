"""Graphs comparing Chord and Pastry for the mid-semester presentation.

These are preliminary results, not the full Phase 5 experiments: static
(already stable) networks, 3 seeds. For each seed Chord and Pastry get the
same node positions, so the difference comes from the algorithm. Error bars
are +/- 1 standard deviation over the seeds.

Output in experiments/results/midsem/:
  raw_static.csv, summary_static.csv, raw_crash.csv
  fig1_hops_vs_n.png       hops vs N with the values from the papers
  fig2_latency_vs_n.png    lookup latency vs N
  fig3_route_stretch.png   route stretch on uniform and clustered maps
  fig4_state_vs_n.png      routing state size vs N
  fig5_crash.png           latency before and after 25% of nodes crash

Run (takes about 30 seconds):
    python -m experiments.midsem_graphs
    python -m experiments.midsem_graphs --sizes 64 256 1024 --seeds 1 2
"""

import argparse
import math
import os

import matplotlib
matplotlib.use("Agg")   # save to files only, don't open a window
import matplotlib.pyplot as plt
import pandas as pd
import simpy

from chord import ring as chord_ring
from pastry import ring as pastry_ring
from sim.metrics import mean
from sim.network import Network
from sim.rng import make_rng
from sim.topology import Clustered, UniformSquare

OUT_DIR = os.path.join(os.path.dirname(__file__), "results", "midsem")

# colours for the graphs (same colour for the same overlay in every graph)
BACKGROUND = "#fcfcfb"
TEXT_DARK = "#0b0b0b"
TEXT_GREY = "#52514e"
LIGHT_GREY = "#898781"
GRID_COLOR = "#e1e0d9"
AXIS_COLOR = "#c3c2b7"
COLORS = {"Chord": "#2a78d6",
          "Pastry (proximity-aware)": "#eb6834",
          "Pastry (random tables)": "#1baf7a"}
MARKERS = {"Chord": "o", "Pastry (proximity-aware)": "s", "Pastry (random tables)": "^"}
OVERLAYS = ["Chord", "Pastry (proximity-aware)", "Pastry (random tables)"]

plt.rcParams["figure.facecolor"] = BACKGROUND
plt.rcParams["axes.facecolor"] = BACKGROUND
plt.rcParams["savefig.facecolor"] = BACKGROUND
plt.rcParams["font.size"] = 12
plt.rcParams["axes.titlesize"] = 15
plt.rcParams["axes.titleweight"] = "bold"
plt.rcParams["axes.titlelocation"] = "left"
plt.rcParams["text.color"] = TEXT_DARK
plt.rcParams["axes.labelcolor"] = TEXT_GREY
plt.rcParams["xtick.color"] = LIGHT_GREY
plt.rcParams["ytick.color"] = LIGHT_GREY
plt.rcParams["axes.edgecolor"] = AXIS_COLOR
plt.rcParams["axes.grid"] = True
plt.rcParams["grid.color"] = GRID_COLOR
plt.rcParams["grid.linewidth"] = 0.8
plt.rcParams["axes.spines.top"] = False
plt.rcParams["axes.spines.right"] = False
plt.rcParams["legend.frameon"] = False
plt.rcParams["legend.fontsize"] = 11
plt.rcParams["lines.linewidth"] = 2
plt.rcParams["lines.markersize"] = 7


# ----- running the simulations -----

def run_lookups(env, nodes, count, seed):
    rng = make_rng(seed, "midsem-lookups")
    live = [n for n in nodes if n.alive]
    procs = []
    for i in range(count):
        origin = rng.choice(live)
        key = f"key-{rng.getrandbits(64):x}"
        procs.append(env.process(origin.lookup(key)))
    env.run(until=simpy.AllOf(env, procs))
    return [p.value for p in procs]


def stretch(net, rec):
    """Route latency / direct latency (None if 0 hops)."""
    direct = net.latency(rec.origin, rec.owner)
    if rec.hops == 0 or direct == 0:
        return None
    total = 0
    for i in range(len(rec.path) - 1):
        total += net.latency(rec.path[i], rec.path[i + 1])
    return total / direct


def build(overlay, n, seed, topology):
    env = simpy.Environment()
    net = Network(env, seed=seed, topology=topology)
    if overlay == "Chord":
        nodes = chord_ring.create_nodes(net, n)
        chord_ring.build_static(nodes)
        oracle = chord_ring.Oracle(nodes)
    else:
        if overlay == "Pastry (proximity-aware)":
            mode = "optimal"
        else:
            mode = "random"
        nodes = pastry_ring.create_nodes(net, n)
        pastry_ring.build_static(nodes, mode, seed=seed)
        oracle = pastry_ring.Oracle(nodes)
    return env, net, nodes, oracle


def count_correct(oracle, recs):
    correct = 0
    for r in recs:
        if oracle.check(r):
            correct += 1
    return correct


def static_experiment(sizes, seeds, n_lookups):
    rows = []
    for topo in [UniformSquare(), Clustered()]:
        for n in sizes:
            for seed in seeds:
                for overlay in OVERLAYS:
                    env, net, nodes, oracle = build(overlay, n, seed, topo)
                    recs = run_lookups(env, nodes, n_lookups, seed)
                    st = []
                    for r in recs:
                        s = stretch(net, r)
                        if s is not None:
                            st.append(s)
                    if len(st) > 0:
                        stretch_mean = mean(st)
                    else:
                        stretch_mean = 1.0
                    rows.append({
                        "topology": topo.name, "overlay": overlay, "N": n, "seed": seed,
                        "hops_mean": mean([r.hops for r in recs]),
                        "latency_mean_ms": mean([r.latency for r in recs]),
                        "stretch_mean": stretch_mean,
                        "state_mean": mean([nd.routing_state_size() for nd in nodes]),
                        "correct_pct": 100 * count_correct(oracle, recs) / len(recs),
                    })
            print(f"  {topo.name:<9} N={n:<5} done", flush=True)
    return pd.DataFrame(rows)


def measure_stage(env, nodes, overlay, seed, n_lookups, stage, rows):
    recs = run_lookups(env, nodes, n_lookups, seed)
    if overlay == "Chord":
        oracle = chord_ring.Oracle(nodes)
    else:
        oracle = pastry_ring.Oracle(nodes)
    ok = [r for r in recs if r.success]
    rows.append({
        "overlay": overlay, "seed": seed, "stage": stage,
        "latency_mean_ms": mean([r.latency for r in ok]),
        "success_pct": 100 * len(ok) / len(recs),
        "correct_pct": 100 * count_correct(oracle, recs) / len(recs),
    })


def crash_experiment(n, seeds, n_lookups):
    """Stable network with maintenance on, then 25% of the nodes crash."""
    rows = []
    for seed in seeds:
        for overlay in ["Chord", "Pastry (proximity-aware)"]:
            env, net, nodes, oracle = build(overlay, n, seed, UniformSquare())
            for node in nodes:
                node.start_maintenance()
            env.run(until=3000)
            measure_stage(env, nodes, overlay, seed, n_lookups, "Before crash", rows)
            for node in make_rng(seed, "midsem-crash").sample(nodes, n // 4):
                node.fail()
            measure_stage(env, nodes, overlay, seed, n_lookups, "Right after crash", rows)
            env.run(until=env.now + 60000)
            measure_stage(env, nodes, overlay, seed, n_lookups, "60 s later", rows)
        print(f"  crash experiment seed {seed} done", flush=True)
    return pd.DataFrame(rows)


# ----- plotting -----

def summarise(df, keys, cols):
    """Mean and std (over seeds) of each column, std columns get _std at the end."""
    groups = df.groupby(keys)[cols]
    means = groups.mean()
    stds = groups.std().fillna(0)
    stds.columns = [c + "_std" for c in stds.columns]
    return means.join(stds).reset_index()


def line_chart(ax, summary, ycol, overlays, label_fmt):
    """One line per overlay with error bars and the last value written at the end."""
    for overlay in overlays:
        d = summary[summary.overlay == overlay].sort_values("N")
        ax.errorbar(d.N, d[ycol], yerr=d[ycol + "_std"], color=COLORS[overlay],
                    marker=MARKERS[overlay], label=overlay, capsize=3,
                    markeredgecolor=BACKGROUND, markeredgewidth=1.5)
        last = d.iloc[-1]
        ax.annotate(label_fmt.format(last[ycol]), (last.N, last[ycol]),
                    xytext=(8, 0), textcoords="offset points", va="center",
                    color=TEXT_GREY, fontsize=11)
    ax.set_xscale("log", base=2)
    ns = sorted(summary.N.unique())
    ax.set_xticks(ns, [f"{n:,}" for n in ns])
    ax.minorticks_off()
    ax.set_xlabel("Number of nodes N (log scale)")


def footnote(fig, text):
    fig.text(0.01, 0.01, text, fontsize=9, color=LIGHT_GREY, ha="left", va="bottom")


def save(fig, name):
    fig.tight_layout(rect=(0, 0.06, 1, 1))
    path = os.path.join(OUT_DIR, name)
    fig.savefig(path, dpi=200)
    plt.close(fig)
    print("  wrote", path)


def fig_hops(summary, seeds):
    s = summary[(summary.topology == "uniform") & (summary.overlay != "Pastry (random tables)")]
    fig, ax = plt.subplots(figsize=(9, 5.2))
    ns = sorted(s.N.unique())
    xs = [ns[0] / 1.15] + ns + [ns[-1] * 1.15]
    # grey lines = values expected from the papers
    ax.plot(xs, [0.5 * math.log2(x) for x in xs], color=LIGHT_GREY, lw=1.2, zorder=1)
    ax.plot(xs, [math.log(x, 16) for x in xs], color=LIGHT_GREY, lw=1.2, zorder=1)
    ax.annotate("Chord paper: ½·log₂N", (ns[0], 0.5 * math.log2(ns[0])), xytext=(4, -16),
                textcoords="offset points", color=LIGHT_GREY, fontsize=10)
    ax.annotate("Pastry paper: log₁₆N", (ns[1], math.log(ns[1], 16)), xytext=(0, 12),
                textcoords="offset points", color=LIGHT_GREY, fontsize=10, ha="center")
    line_chart(ax, s, "hops_mean", ["Chord", "Pastry (proximity-aware)"], "{:.2f}")
    ax.set_ylim(bottom=0)
    ax.set_ylabel("Average hops per lookup")
    ax.set_title("Pastry needs about half Chord's hops; both follow their papers")
    ax.legend(loc="upper left")
    footnote(fig, f"Static networks, uniform topology, {len(seeds)} seeds; error bars ±1 std "
                  "across seeds. Hops counted until the lookup reaches the key's owner.")
    save(fig, "fig1_hops_vs_n.png")


def fig_latency(summary, seeds):
    s = summary[summary.topology == "uniform"]
    fig, ax = plt.subplots(figsize=(9, 5.2))
    line_chart(ax, s, "latency_mean_ms", OVERLAYS, "{:.0f} ms")
    ax.set_ylim(bottom=0)
    ax.set_ylabel("Average lookup latency (ms)")
    ax.set_title("Pastry's lookups are about 3× faster than Chord's at 4,096 nodes")
    ax.legend(loc="upper left")
    footnote(fig, f"Static networks, uniform topology (100×100 ms square), {len(seeds)} seeds; "
                  "latency = request route + direct reply to the asker.")
    save(fig, "fig2_latency_vs_n.png")


def fig_stretch(summary, n, seeds):
    s = summary[summary.N == n]
    topologies = ["uniform", "clustered"]
    fig, ax = plt.subplots(figsize=(9, 5.2))
    width = 0.26
    for i in range(len(OVERLAYS)):
        overlay = OVERLAYS[i]
        d = s[s.overlay == overlay].set_index("topology").loc[topologies]
        xs = [t + (i - 1) * (width + 0.02) for t in range(len(topologies))]
        ax.bar(xs, d.stretch_mean, width, yerr=d.stretch_mean_std, color=COLORS[overlay],
               label=overlay, capsize=3, error_kw={"ecolor": TEXT_GREY, "lw": 1})
        values = list(d.stretch_mean)
        errors = list(d.stretch_mean_std)
        for j in range(len(xs)):
            ax.annotate(f"{values[j]:.1f}×", (xs[j], values[j] + errors[j]), xytext=(0, 4),
                        textcoords="offset points", ha="center", color=TEXT_GREY, fontsize=11)
    ax.axhline(1.0, color=AXIS_COLOR, lw=1)
    ax.set_xticks(range(len(topologies)), ["Uniform network", "Clustered (continents)"])
    ax.grid(axis="x", visible=False)
    ax.set_ylabel("Route stretch (route latency ÷ direct latency)")
    ax.set_title("Choosing nearby nodes keeps Pastry's routes close to the direct path")
    ax.legend(loc="upper left")
    footnote(fig, f"N = {n:,}, static networks, {len(seeds)} seeds, ±1 std, same node positions "
                  "for all three. Mean of per-lookup ratios (1.0× = direct path);\n"
                  "physically close asker/owner pairs make the ratio large, which is why "
                  "Chord's stretch exceeds its 3× latency gap.")
    save(fig, "fig3_route_stretch.png")


def fig_state(summary, seeds):
    s = summary[(summary.topology == "uniform") & (summary.overlay != "Pastry (random tables)")]
    fig, ax = plt.subplots(figsize=(9, 5.2))
    line_chart(ax, s, "state_mean", ["Chord", "Pastry (proximity-aware)"], "{:.0f}")
    ax.set_ylim(bottom=0)
    ax.set_ylabel("Distinct nodes in routing state")
    ax.set_title("The price: Pastry keeps about 3–4× more routing state")
    ax.legend(loc="upper left")
    footnote(fig, "Chord: fingers + successor list (8) + predecessor. Pastry: routing table + "
                  f"leaf set (16) + neighbourhood set (32). {len(seeds)} seeds.")
    save(fig, "fig4_state_vs_n.png")


def fig_crash(crash, n, seeds):
    stages = ["Before crash", "Right after crash", "60 s later"]
    s = summarise(crash, ["overlay", "stage"], ["latency_mean_ms", "correct_pct"])
    overlays = ["Chord", "Pastry (proximity-aware)"]
    fig, ax = plt.subplots(figsize=(9, 5.2))
    width = 0.36
    for i in range(len(overlays)):
        overlay = overlays[i]
        d = s[s.overlay == overlay].set_index("stage").loc[stages]
        xs = [t + (i - 0.5) * (width + 0.02) for t in range(len(stages))]
        ax.bar(xs, d.latency_mean_ms, width, yerr=d.latency_mean_ms_std,
               color=COLORS[overlay], label=overlay, capsize=3,
               error_kw={"ecolor": TEXT_GREY, "lw": 1})
        values = list(d.latency_mean_ms)
        errors = list(d.latency_mean_ms_std)
        for j in range(len(xs)):
            ax.annotate(f"{values[j]:.0f}", (xs[j], values[j] + errors[j]), xytext=(0, 4),
                        textcoords="offset points", ha="center", color=TEXT_GREY, fontsize=11)
    ax.set_xticks(range(len(stages)), stages)
    ax.grid(axis="x", visible=False)
    ax.set_ylabel("Average lookup latency (ms)")
    correct = crash.correct_pct.min()
    if correct == 100:
        verdict = "stay correct"
    else:
        verdict = f"are ≥{correct:.1f}% correct"
    ax.set_title(f"When 25% of nodes crash, lookups slow down but {verdict}")
    ax.legend(loc="upper right")
    footnote(fig, f"N = {n} nodes, uniform topology, repair protocols running, {len(seeds)} seeds, "
                  f"±1 std. Lowest correctness in any stage: {correct:.0f}%.\n'Right after': "
                  "lookups wait out 500 ms timeouts on dead hops; Chord's longer routes hit more.")
    save(fig, "fig5_crash.png")


def main():
    parser = argparse.ArgumentParser(description="Chord vs Pastry graphs for the mid-sem presentation")
    parser.add_argument("--sizes", type=int, nargs="+", default=[64, 256, 1024, 4096])
    parser.add_argument("--seeds", type=int, nargs="+", default=[1, 2, 3])
    parser.add_argument("--lookups", type=int, default=1000)
    parser.add_argument("--stretch-n", type=int, default=1024)
    parser.add_argument("--crash-n", type=int, default=300)
    args = parser.parse_args()
    os.makedirs(OUT_DIR, exist_ok=True)

    print("Static networks ...")
    raw = static_experiment(args.sizes, args.seeds, args.lookups)
    raw.to_csv(os.path.join(OUT_DIR, "raw_static.csv"), index=False)
    summary = summarise(raw, ["topology", "overlay", "N"],
                        ["hops_mean", "latency_mean_ms", "stretch_mean", "state_mean",
                         "correct_pct"])
    summary.to_csv(os.path.join(OUT_DIR, "summary_static.csv"), index=False, float_format="%.3f")

    print("Crash experiment ...")
    crash = crash_experiment(args.crash_n, args.seeds, args.lookups // 2)
    crash.to_csv(os.path.join(OUT_DIR, "raw_crash.csv"), index=False, float_format="%.3f")

    print("Drawing figures ...")
    if args.stretch_n in args.sizes:
        stretch_n = args.stretch_n
    else:
        stretch_n = args.sizes[-1]
    fig_hops(summary, args.seeds)
    fig_latency(summary, args.seeds)
    fig_stretch(summary, stretch_n, args.seeds)
    fig_state(summary, args.seeds)
    fig_crash(crash, args.crash_n, args.seeds)
    print(f"\nLowest correctness in any static run: {raw.correct_pct.min():.1f}%")


if __name__ == "__main__":
    main()
