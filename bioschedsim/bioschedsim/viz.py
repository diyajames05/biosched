"""Evaluation layer - Gantt charts and comparison plots (matplotlib, PNG)."""
from __future__ import annotations

from typing import Dict, List, Sequence

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

# Fixed categorical order (never cycled): colour follows the entity.
PALETTE = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#6250d6", "#e34948"]
CAT_COLORS = {"gc_content": PALETTE[0], "kmer_count": PALETTE[1],
              "motif_search": PALETTE[2], "alignment": PALETTE[3]}
INK, MUTED, GRID = "#0b0b0b", "#52514e", "#e4e3df"


def algo_colors(names: Sequence[str]) -> Dict[str, str]:
    return {n: PALETTE[i % len(PALETTE)] for i, n in enumerate(names)}


def _style(ax, title=None, xlabel=None, ylabel=None):
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color(MUTED)
    ax.tick_params(colors=MUTED, labelsize=9)
    ax.grid(axis="y", color=GRID, linewidth=0.8)
    ax.set_axisbelow(True)
    if title:
        ax.set_title(title, color=INK, fontsize=11, loc="left")
    if xlabel:
        ax.set_xlabel(xlabel, color=MUTED, fontsize=9)
    if ylabel:
        ax.set_ylabel(ylabel, color=MUTED, fontsize=9)


def gantt(results, path: str, max_time: float | None = None) -> None:
    """One Gantt panel per algorithm; rows = cores, colour = task category."""
    fig, axes = plt.subplots(len(results), 1, figsize=(12, 1.1 + 0.55 * results[0].num_cores * len(results)),
                             sharex=True, squeeze=False)
    for ax, r in zip(axes[:, 0], results):
        for s in r.segments:
            ax.barh(s.core_id, s.end - s.start, left=s.start, height=0.7,
                    color=CAT_COLORS[s.category], edgecolor="white", linewidth=1)
            if s.end - s.start > (max_time or r.makespan_end) * 0.025:
                ax.text((s.start + s.end) / 2, s.core_id, f"T{s.task_id}", ha="center",
                        va="center", fontsize=7, color="white")
        ax.set_yticks(range(r.num_cores), [f"core {i}" for i in range(r.num_cores)])
        ax.invert_yaxis()
        _style(ax, r.algorithm)
        ax.grid(axis="x", color=GRID, linewidth=0.8)
        ax.grid(axis="y", visible=False)
    axes[-1, 0].set_xlabel("simulated time (ms)", color=MUTED, fontsize=9)
    if max_time:
        axes[-1, 0].set_xlim(0, max_time)
    handles = [plt.Rectangle((0, 0), 1, 1, color=c) for c in CAT_COLORS.values()]
    fig.legend(handles, list(CAT_COLORS), loc="upper center", ncol=4, frameon=False, fontsize=9)
    fig.tight_layout(rect=(0, 0, 1, 0.96))
    fig.savefig(path, dpi=150)
    plt.close(fig)


def bar_comparison(summary: Dict[str, Dict[str, tuple]], metrics: List[tuple], path: str, title: str) -> None:
    """summary[algo][metric] = (mean, std). metrics = [(key, label), ...]."""
    algos = list(summary)
    colors = algo_colors(algos)
    cols = min(3, len(metrics))
    rows = (len(metrics) + cols - 1) // cols
    fig, axes = plt.subplots(rows, cols, figsize=(4.2 * cols, 3.2 * rows), squeeze=False)
    for ax, (key, label) in zip(axes.flat, metrics):
        means = [summary[a][key][0] for a in algos]
        stds = [summary[a][key][1] for a in algos]
        ax.bar(range(len(algos)), means, yerr=stds, width=0.62, color=[colors[a] for a in algos],
               edgecolor="white", linewidth=2, error_kw={"ecolor": MUTED, "elinewidth": 1, "capsize": 3})
        ax.set_xticks(range(len(algos)), algos, rotation=30, ha="right", fontsize=8)
        _style(ax, label)
    for ax in list(axes.flat)[len(metrics):]:
        ax.axis("off")
    fig.suptitle(title, x=0.01, ha="left", color=INK, fontsize=12)
    fig.tight_layout()
    fig.savefig(path, dpi=150)
    plt.close(fig)


def line_sweep(xs, series: Dict[str, List[float]], path: str, title: str, xlabel: str,
               ylabel: str, logy: bool = False, xlog: bool = False) -> None:
    colors = algo_colors(list(series))
    fig, ax = plt.subplots(figsize=(7, 4.2))
    for name, ys in series.items():
        ax.plot(xs, ys, marker="o", markersize=5, linewidth=2, color=colors[name], label=name)
    if logy:
        ax.set_yscale("log")
    if xlog:
        ax.set_xscale("log")
    _style(ax, title, xlabel, ylabel)
    ax.legend(frameon=False, fontsize=8, ncol=2)
    fig.tight_layout()
    fig.savefig(path, dpi=150)
    plt.close(fig)


def grouped_bars(groups: List[str], series: Dict[str, List[float]], path: str, title: str, ylabel: str) -> None:
    algos = list(series)
    colors = algo_colors(algos)
    n = len(algos)
    w = 0.8 / n
    fig, ax = plt.subplots(figsize=(9, 4.2))
    for i, a in enumerate(algos):
        ax.bar([g + (i - (n - 1) / 2) * w for g in range(len(groups))], series[a], width=w,
               color=colors[a], edgecolor="white", linewidth=1.5, label=a)
    ax.set_xticks(range(len(groups)), groups)
    _style(ax, title, None, ylabel)
    ax.legend(frameon=False, fontsize=8, ncol=3)
    fig.tight_layout()
    fig.savefig(path, dpi=150)
    plt.close(fig)


def gantt_single(result, path: str, max_time: float | None = None) -> None:
    """Gantt chart of one algorithm, idle intervals shown hatched."""
    M = result.num_cores
    fig, ax = plt.subplots(figsize=(12, 1.4 + 0.6 * M))
    for i in result.idle:
        ax.barh(i.core_id, i.end - i.start, left=i.start, height=0.7, color="none",
                edgecolor=GRID, hatch="////", linewidth=0)
    for s in result.segments:
        ax.barh(s.core_id, s.end - s.start, left=s.start, height=0.7,
                color=CAT_COLORS[s.category], edgecolor="white", linewidth=1)
        if s.end - s.start > (max_time or result.makespan_end) * 0.025:
            ax.text((s.start + s.end) / 2, s.core_id, f"T{s.task_id}", ha="center",
                    va="center", fontsize=7, color="white")
    ax.set_yticks(range(M), [f"core {i}" for i in range(M)])
    ax.invert_yaxis()
    _style(ax, f"Gantt chart - {result.algorithm}", "simulated time (ms)")
    ax.grid(axis="x", color=GRID, linewidth=0.8)
    ax.grid(axis="y", visible=False)
    if max_time:
        ax.set_xlim(0, max_time)
    handles = [plt.Rectangle((0, 0), 1, 1, color=c) for c in CAT_COLORS.values()]
    handles.append(plt.Rectangle((0, 0), 1, 1, facecolor="none", edgecolor=MUTED, hatch="////"))
    ax.legend(handles, list(CAT_COLORS) + ["idle"], loc="upper center", bbox_to_anchor=(0.5, -0.35),
              ncol=5, frameon=False, fontsize=8)
    fig.tight_layout()
    fig.savefig(path, dpi=150)
    plt.close(fig)


def wait_distribution(per_task_waits: Dict[str, List[float]], path: str, title: str) -> None:
    """Empirical CDF of per-task waiting time (log x) plus a box plot."""
    import numpy as np
    algos = list(per_task_waits)
    colors = algo_colors(algos)
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(12, 4.4), gridspec_kw={"width_ratios": [1.3, 1]})
    for a in algos:
        w = np.sort(np.maximum(np.asarray(per_task_waits[a]), 1e-3))
        ax1.plot(w, np.arange(1, len(w) + 1) / len(w), linewidth=2, color=colors[a], label=a)
    ax1.set_xscale("log")
    _style(ax1, "Cumulative distribution", "waiting time (ms, log)", "share of tasks ≤ x")
    ax1.legend(frameon=False, fontsize=8, loc="lower right")
    bp = ax2.boxplot([np.maximum(per_task_waits[a], 1e-3) for a in algos], whis=(5, 95),
                     showfliers=False, patch_artist=True, widths=0.55)
    for patch, a in zip(bp["boxes"], algos):
        patch.set_facecolor(colors[a])
        patch.set_edgecolor("white")
    for med in bp["medians"]:
        med.set_color(INK)
    ax2.set_yscale("log")
    ax2.set_xticks(range(1, len(algos) + 1), algos, rotation=30, ha="right", fontsize=8)
    _style(ax2, "Box: p5–p95 whiskers", None, "waiting time (ms, log)")
    fig.suptitle(title, x=0.01, ha="left", color=INK, fontsize=12)
    fig.tight_layout()
    fig.savefig(path, dpi=150)
    plt.close(fig)


def ci_bars(summary, metrics: List[tuple], path: str, title: str) -> None:
    """Bars = mean, whiskers = 95 % CI. `summary` is analysis.summarize_runs output."""
    algos = list(dict.fromkeys(summary["algorithm"]))
    colors = algo_colors(algos)
    cols = min(3, len(metrics))
    rows = (len(metrics) + cols - 1) // cols
    fig, axes = plt.subplots(rows, cols, figsize=(4.2 * cols, 3.2 * rows), squeeze=False)
    for ax, (key, label) in zip(axes.flat, metrics):
        sub = summary[summary.metric == key].set_index("algorithm").loc[algos]
        err = sub["ci_half"].fillna(0).values
        ax.bar(range(len(algos)), sub["mean"].values, yerr=err, width=0.62,
               color=[colors[a] for a in algos], edgecolor="white", linewidth=2,
               error_kw={"ecolor": MUTED, "elinewidth": 1, "capsize": 3})
        ax.set_xticks(range(len(algos)), algos, rotation=30, ha="right", fontsize=8)
        _style(ax, label)
    for ax in list(axes.flat)[len(metrics):]:
        ax.axis("off")
    fig.suptitle(title, x=0.01, ha="left", color=INK, fontsize=12)
    fig.tight_layout()
    fig.savefig(path, dpi=150)
    plt.close(fig)
