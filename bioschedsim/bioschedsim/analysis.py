"""Module 7 - Experimental analysis and reporting (pandas).

* Aggregates repeated runs (one row per seed x algorithm) into mean, std,
  95 % confidence interval (Student t), median and min/max.
* Compares the adaptive scheduler with each baseline using *paired*
  differences over the same seeds: absolute difference with its 95 % CI,
  relative change, and a Wilcoxon signed-rank p-value (when SciPy is present).

  Relative change (lower-is-better metric, baseline b, candidate a):
        rel = (b - a) / |b| * 100 %     (positive = improvement)
  Higher-is-better metrics flip the sign. If the baseline is zero or near
  zero - |b| < 1e-6, or |b| below a tenth of the candidate's value, where a
  percentage would be dominated by the tiny denominator - only the absolute
  difference is reported.

* Every table carries an ``evidence`` column ("simulation" or "hardware"),
  so simulated improvements are never presented as hardware results.
"""
from __future__ import annotations

import math
from typing import Dict, Iterable, List, Optional

import pandas as pd

# direction of "better" for the main metrics
HIGHER_IS_BETTER = {"throughput_per_s", "cpu_utilization", "jain_fairness"}
NEAR_ZERO = 1e-6
NEAR_ZERO_FRACTION = 0.1


def near_zero(base: float, cand: float) -> bool:
    return abs(base) < NEAR_ZERO or abs(base) < NEAR_ZERO_FRACTION * abs(cand)


def t_critical(df: int, conf: float = 0.95) -> float:
    if df <= 0:
        return float("nan")
    try:
        from scipy import stats
        return float(stats.t.ppf(0.5 + conf / 2, df))
    except ImportError:            # normal approximation fallback
        return 1.96


def runs_to_frame(runs: Dict[int, Dict[str, dict]], **extra) -> pd.DataFrame:
    """runs[seed][algorithm] = summary dict -> tidy DataFrame."""
    rows = []
    for seed, by_algo in runs.items():
        for algo, m in by_algo.items():
            rows.append({**extra, "seed": seed, **m, "algorithm": algo})
    return pd.DataFrame(rows)


def summarize_runs(df: pd.DataFrame, metrics: Iterable[str], by=("algorithm",), conf=0.95) -> pd.DataFrame:
    """Long table: group x metric -> n, mean, std, ci_low, ci_high, median, min, max."""
    by = list(by)
    out = []
    order = list(dict.fromkeys(df["algorithm"]))
    for keys, g in df.groupby(by, sort=False):
        keys = keys if isinstance(keys, tuple) else (keys,)
        for m in metrics:
            x = g[m].astype(float)
            n = len(x)
            mean, std = x.mean(), (x.std(ddof=1) if n > 1 else 0.0)
            half = t_critical(n - 1, conf) * std / math.sqrt(n) if n > 1 else float("nan")
            out.append({**dict(zip(by, keys)), "metric": m, "n": n, "mean": mean, "std": std,
                        "ci_low": mean - half, "ci_high": mean + half, "ci_half": half,
                        "median": x.median(), "min": x.min(), "max": x.max(),
                        "evidence": g["evidence"].iloc[0] if "evidence" in g else "simulation"})
    res = pd.DataFrame(out)
    if "algorithm" in by:
        res["_o"] = res["algorithm"].map({a: i for i, a in enumerate(order)})
        res = res.sort_values([c for c in by if c != "algorithm"] + ["_o"], kind="stable").drop(columns="_o")
    return res.reset_index(drop=True)


def wide(summary: pd.DataFrame, value="mean") -> pd.DataFrame:
    return summary.pivot_table(index="algorithm", columns="metric", values=value, sort=False)


def compare_to_baselines(df: pd.DataFrame, candidate: str, metrics: Iterable[str],
                         conf: float = 0.95) -> pd.DataFrame:
    """Paired comparison candidate vs every other algorithm over identical seeds."""
    try:
        from scipy.stats import wilcoxon
    except ImportError:
        wilcoxon = None
    piv = {m: df.pivot_table(index="seed", columns="algorithm", values=m) for m in metrics}
    rows = []
    for base in [a for a in dict.fromkeys(df["algorithm"]) if a != candidate]:
        for m in metrics:
            p = piv[m][[base, candidate]].dropna()
            b, a = p[base], p[candidate]
            diff = a - b                                     # candidate - baseline
            n = len(diff)
            half = t_critical(n - 1, conf) * diff.std(ddof=1) / math.sqrt(n) if n > 1 else float("nan")
            bmean, amean = b.mean(), a.mean()
            sign = 1 if m in HIGHER_IS_BETTER else -1
            rel = None if near_zero(bmean, amean) else sign * (amean - bmean) / abs(bmean) * 100
            pval = None
            if wilcoxon is not None and n >= 5 and (diff != 0).any():
                try:
                    pval = float(wilcoxon(a, b).pvalue)
                except ValueError:
                    pval = None
            better = (diff.mean() > 0) if m in HIGHER_IS_BETTER else (diff.mean() < 0)
            rows.append({"baseline": base, "metric": m, "n_pairs": n, "baseline_mean": bmean,
                         "candidate_mean": amean, "abs_diff": diff.mean(),
                         "abs_diff_ci_low": diff.mean() - half, "abs_diff_ci_high": diff.mean() + half,
                         "rel_change_pct": rel,
                         "report_as": "absolute" if rel is None else "relative",
                         "candidate_better": bool(better), "wilcoxon_p": pval,
                         "evidence": df["evidence"].iloc[0] if "evidence" in df else "simulation"})
    return pd.DataFrame(rows)


# --------------------------------------------------------------- markdown
def _fmt(v, nd=2):
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return "–"
    if isinstance(v, (int,)) and not isinstance(v, bool):
        return str(v)
    return f"{v:.{nd}f}"


def md_ci_table(summary: pd.DataFrame, metrics: List[tuple], nd=2) -> str:
    """Markdown: rows = algorithms, cells = mean [95% CI]."""
    head = "| Algorithm | " + " | ".join(lbl for _, lbl in metrics) + " |"
    lines = [head, "|---|" + "---|" * len(metrics)]
    for algo in dict.fromkeys(summary["algorithm"]):
        cells = []
        for key, _ in metrics:
            r = summary[(summary.algorithm == algo) & (summary.metric == key)]
            if r.empty:
                cells.append("–")
                continue
            r = r.iloc[0]
            if r["n"] > 1:
                cells.append(f"{_fmt(r['mean'], nd)} [{_fmt(r['ci_low'], nd)}, {_fmt(r['ci_high'], nd)}]")
            else:
                cells.append(_fmt(r["mean"], nd))
        lines.append(f"| {algo} | " + " | ".join(cells) + " |")
    return "\n".join(lines)


def md_comparison(comp: pd.DataFrame, metrics: List[tuple]) -> str:
    """Markdown: rows = baselines, cells = relative change (abs diff [CI], p)."""
    head = "| Adaptive vs | " + " | ".join(lbl for _, lbl in metrics) + " |"
    lines = [head, "|---|" + "---|" * len(metrics)]
    for base in dict.fromkeys(comp["baseline"]):
        cells = []
        for key, _ in metrics:
            r = comp[(comp.baseline == base) & (comp.metric == key)].iloc[0]
            absd = f"Δ {r['abs_diff']:+.2f} [{r['abs_diff_ci_low']:+.2f}, {r['abs_diff_ci_high']:+.2f}]"
            p = "" if r["wilcoxon_p"] is None or pd.isna(r["wilcoxon_p"]) else f", p={r['wilcoxon_p']:.3f}"
            if r["report_as"] == "relative":
                cells.append(f"**{r['rel_change_pct']:+.1f} %** ({absd}{p})")
            else:
                cells.append(f"{absd}{p} (baseline ≈ 0: absolute only)")
        lines.append(f"| {base} | " + " | ".join(cells) + " |")
    return "\n".join(lines)
