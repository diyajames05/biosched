"""Modules 1 & 2 - Workload generation, task profiling and burst estimation.

Turns bioinformatics inputs into a standard workload. Every task record has:
task ID, category (operation), input size, arrival time, *estimated* burst,
*actual* burst, priority, optional deadline and operation parameters.

Information separation (Module 2):
    Task      - full immutable record, including the actual burst. Only the
                simulation engine and the metric calculator read `burst`.
    TaskInfo  - the scheduler-visible view: same fields *without* the actual
                burst, so no policy can leak future information.

Generators
    A. generate_synthetic     - controlled sizes / arrivals; burst = cost-model
                                estimate x log-normal estimation error.
    B. generate_from_dataset  - windows cut from a validated FASTA file; every
                                task is really executed and timed here.

Time unit everywhere: milliseconds (ms).
"""
from __future__ import annotations

import csv
import dataclasses
import json
import math
import os
import platform
import random
import statistics
import time
from dataclasses import asdict, dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

from . import bio_ops
from .bio_ops import CATEGORIES

PRIORITY_LEVELS = (1, 2, 3, 4, 5)  # 1 = most important, 5 = least important

# size classes on the *estimated* burst (ms), used for short/medium/long mixes
SIZE_CLASSES = (("short", 0.0, 5.0), ("medium", 5.0, 50.0), ("long", 50.0, math.inf))


def size_class(est_burst: float) -> str:
    for name, lo, hi in SIZE_CLASSES:
        if lo <= est_burst < hi:
            return name
    return "long"


@dataclass(frozen=True)
class TaskInfo:
    """Scheduler-visible task metadata (no actual burst)."""
    task_id: int
    category: str
    arrival: float
    est_burst: float
    priority: int
    input_size: int
    deadline: Optional[float] = None
    params: Dict = field(default_factory=dict, compare=False, hash=False)


@dataclass(frozen=True)
class Task:
    """Canonical, immutable task record."""
    task_id: int
    category: str
    arrival: float          # A_i  (ms)
    est_burst: float        # estimate visible to the scheduler (ms)
    burst: float            # actual CPU service time B_i (ms) - simulator only
    priority: int           # 1 (highest) .. 5 (lowest)
    input_size: int         # bases processed (L, or L1*L2 cells for alignment)
    deadline: Optional[float] = None   # absolute soft deadline (ms) or None
    params: Dict = field(default_factory=dict, compare=True, hash=False)

    def info(self) -> TaskInfo:
        return TaskInfo(self.task_id, self.category, self.arrival, self.est_burst, self.priority,
                        self.input_size, self.deadline, dict(self.params))

    @property
    def size_class(self) -> str:
        return size_class(self.est_burst)


# --------------------------------------------------------------- cost model
def _features(category: str, p: dict) -> List[float]:
    """Feature vector for the complexity-based linear cost model."""
    if category == "gc_content":
        return [1.0, p["length"]]
    if category == "kmer_count":
        return [1.0, p["length"], p["length"] * p["k"]]
    if category == "motif_search":
        return [1.0, p["length"], p["length"] * p["motif_len"]]
    if category == "alignment":
        return [1.0, p["len_a"] * p["len_b"]]
    raise ValueError(category)


# Coefficients (ms) fitted with `calibrate()` on the development machine
# (Linux x86-64, Python 3.13). Run `python -m bioschedsim calibrate` to refit.
DEFAULT_COEF: Dict[str, List[float]] = {
    "gc_content": [0.01, 6.2e-06],
    "kmer_count": [0.0, 7.7e-05, 1.95e-05],
    "motif_search": [1.8, 3.9e-04, 0.0],
    "alignment": [0.0, 2.1e-04],
}


class CostModel:
    """Burst-time estimator: predicts execution time (ms) from task parameters only."""

    def __init__(self, coef: Optional[Dict[str, List[float]]] = None):
        self.coef = {k: list(v) for k, v in (coef or DEFAULT_COEF).items()}
        self.r2: Dict[str, float] = {}

    def predict(self, category: str, params: dict) -> float:
        f = _features(category, params)
        return max(0.01, sum(c * x for c, x in zip(self.coef[category], f)))

    def fit(self, samples: Sequence[tuple]) -> "CostModel":
        """samples: (category, params, measured_ms). Least squares per category."""
        import numpy as np
        for cat in CATEGORIES:
            rows = [(p, y) for c, p, y in samples if c == cat]
            if len(rows) < 3:
                continue
            X = np.array([_features(cat, p) for p, _ in rows])
            y = np.array([y for _, y in rows])
            beta, *_ = np.linalg.lstsq(X, y, rcond=None)
            self.coef[cat] = [float(b) for b in beta]
            pred = X @ beta
            ss_res = float(((y - pred) ** 2).sum())
            ss_tot = float(((y - y.mean()) ** 2).sum()) or 1e-12
            self.r2[cat] = 1 - ss_res / ss_tot
        return self

    def save(self, path: str) -> None:
        with open(path, "w") as fh:
            json.dump({"coef": self.coef, "r2": self.r2, "machine": machine_info()}, fh, indent=2)

    @classmethod
    def load(cls, path: str) -> "CostModel":
        with open(path) as fh:
            d = json.load(fh)
        m = cls(d["coef"])
        m.r2 = d.get("r2", {})
        return m


# --------------------------------------------------- task parameter sampling
def _log_uniform(rng: random.Random, lo: float, hi: float) -> float:
    return math.exp(rng.uniform(math.log(lo), math.log(hi)))


# Input-size ranges per category (bases). `scale` shrinks/enlarges all of them.
SIZE_RANGES = {
    "gc_content": (50_000, 2_000_000),
    "kmer_count": (10_000, 200_000),
    "motif_search": (10_000, 150_000),
    "alignment": (150, 1_200),          # per sequence; work is len_a * len_b
}

# Category mixes used to control workload heterogeneity.
MIXES = {
    "low":    {"gc_content": 0.5, "kmer_count": 0.5, "motif_search": 0.0, "alignment": 0.0},
    "medium": {"gc_content": 0.3, "kmer_count": 0.3, "motif_search": 0.25, "alignment": 0.15},
    "high":   {"gc_content": 0.45, "kmer_count": 0.15, "motif_search": 0.1, "alignment": 0.3},
}


def sample_params(category: str, rng: random.Random, scale: float = 1.0) -> dict:
    lo, hi = SIZE_RANGES[category]
    lo, hi = max(1, lo * scale), max(2, hi * scale)
    if category == "gc_content":
        return {"length": int(_log_uniform(rng, lo, hi))}
    if category == "kmer_count":
        return {"length": int(_log_uniform(rng, lo, hi)), "k": rng.randint(4, 12)}
    if category == "motif_search":
        m = rng.randint(6, 16)
        return {"length": int(_log_uniform(rng, lo, hi)), "motif_len": m, "max_mismatches": 1}
    if category == "alignment":
        s = math.sqrt(scale)  # quadratic work: scale each side by sqrt(scale)
        a_lo, a_hi = max(10, SIZE_RANGES["alignment"][0] * s), SIZE_RANGES["alignment"][1] * s
        return {"len_a": int(_log_uniform(rng, a_lo, a_hi)), "len_b": int(_log_uniform(rng, a_lo, a_hi))}
    raise ValueError(category)


def input_size(category: str, p: dict) -> int:
    return p["len_a"] * p["len_b"] if category == "alignment" else p["length"]


def _poisson_arrivals(n: int, rate: float, rng: random.Random) -> List[float]:
    t, out = 0.0, []
    for _ in range(n):
        out.append(round(t, 4))
        t += rng.expovariate(rate)
    return out


def _finalise(tasks: List[Task], num_cores: int, load: float, rng: random.Random,
              deadline_fraction: float, deadline_slack: Tuple[float, float]) -> List[Task]:
    """Assign Poisson arrivals (offered load rho = lambda*E[B]/M) and optional deadlines.

    Note: the arrival rate uses the mean *estimated* burst, so it never peeks
    at the actual bursts either.
    """
    mean_b = statistics.fmean(t.est_burst for t in tasks)
    rate = load * num_cores / mean_b
    out = []
    for t, a in zip(tasks, _poisson_arrivals(len(tasks), rate, rng)):
        dl = None
        if deadline_fraction > 0 and rng.random() < deadline_fraction:
            dl = round(a + t.est_burst * rng.uniform(*deadline_slack) + rng.uniform(0, mean_b), 4)
        out.append(dataclasses.replace(t, arrival=a, deadline=dl))
    return out


def _choose_category(rng: random.Random, mix: Dict[str, float]) -> str:
    cats = [c for c in CATEGORIES if mix.get(c, 0) > 0]
    return rng.choices(cats, weights=[mix[c] for c in cats])[0]


# ------------------------------------------------- A. synthetic generation
def generate_synthetic(n: int = 200, num_cores: int = 4, load: float = 0.8,
                       mix: str | Dict[str, float] = "medium", seed: int = 0,
                       estimate_error: float = 0.25, size_scale: float = 1.0,
                       cost_model: Optional[CostModel] = None,
                       deadline_fraction: float = 0.3,
                       deadline_slack: Tuple[float, float] = (2.0, 6.0)) -> List[Task]:
    """Synthetic workload.

    estimate_error    : sigma of the log-normal error between estimated and
                        actual burst (0 -> perfect knowledge).
    load              : offered load rho = lambda * E[B] / M  (>1 means overload)
    size_scale        : multiplies all sequence lengths (sequence-length sweeps)
    deadline_fraction : share of tasks that get a soft deadline
                        deadline = A + est * U(slack) + U(0, mean est)
    """
    rng = random.Random(seed)
    mix_w = MIXES[mix] if isinstance(mix, str) else mix
    cm = cost_model or CostModel()
    tasks = []
    for i in range(n):
        cat = _choose_category(rng, mix_w)
        p = sample_params(cat, rng, size_scale)
        est = cm.predict(cat, p)
        actual = est * math.exp(rng.gauss(0, estimate_error)) if estimate_error > 0 else est
        tasks.append(Task(i, cat, 0.0, round(est, 4), round(max(actual, 0.01), 4),
                          rng.choice(PRIORITY_LEVELS), input_size(cat, p), None, p))
    # separate RNG stream for arrivals/deadlines so changing deadline settings
    # does not change task sizes
    return _finalise(tasks, num_cores, load, random.Random(seed * 7919 + 1),
                     deadline_fraction, deadline_slack)


# ------------------------------------------- B. dataset-based generation
def machine_info() -> dict:
    info = {
        "platform": platform.platform(),
        "processor": platform.processor() or platform.machine(),
        "cpu_count": os.cpu_count(),
        "python": platform.python_version(),
        "implementation": platform.python_implementation(),
        "timer": "time.perf_counter, median of repeats, ms",
    }
    try:
        import psutil  # optional hardware monitoring
        info["memory_total_gb"] = round(psutil.virtual_memory().total / 2**30, 2)
        info["physical_cores"] = psutil.cpu_count(logical=False)
        f = psutil.cpu_freq()
        if f:
            info["cpu_freq_mhz"] = round(f.current)
    except Exception:
        pass
    return info


class HardwareMonitor:
    """Samples CPU utilisation and memory while profiling (needs psutil)."""

    def __init__(self):
        try:
            import psutil
            self.ps = psutil
            psutil.cpu_percent(None)
        except ImportError:
            self.ps = None
        self.samples: List[Tuple[float, float]] = []

    def sample(self):
        if self.ps:
            self.samples.append((self.ps.cpu_percent(None), self.ps.virtual_memory().percent))

    def summary(self) -> dict:
        if not self.samples:
            return {"hardware_monitoring": "psutil not installed" if not self.ps else "no samples"}
        cpu = [s[0] for s in self.samples]
        mem = [s[1] for s in self.samples]
        return {"cpu_percent_mean": round(statistics.fmean(cpu), 1), "cpu_percent_max": max(cpu),
                "mem_percent_mean": round(statistics.fmean(mem), 1), "samples": len(cpu)}


def _time_op(category: str, p: dict, a: str, b: str, repeats: int) -> float:
    times = []
    for _ in range(repeats):
        t0 = time.perf_counter()
        bio_ops.run_operation(category, p, a, b)
        times.append((time.perf_counter() - t0) * 1000.0)
    return statistics.median(times)


def _pick_window(genome: List[str], L: int, rng: random.Random) -> Tuple[int, int, int]:
    r = rng.randrange(len(genome))
    L = min(L, len(genome[r]))
    return r, rng.randrange(0, len(genome[r]) - L + 1), L


def materialise_inputs(category: str, p: dict, genome: List[str]) -> Tuple[str, str, dict]:
    """Rebuild the exact input sequences of a task from its stored coordinates."""
    def cut(key):
        r, s, L = p[key]
        return genome[r][s:s + L]
    if category == "alignment":
        return cut("win_a"), cut("win_b"), p
    seq = cut("win_a")
    if category == "motif_search":
        p = {**p, "motif": cut("win_motif")}
    return seq, "", p


def _assign_windows(category: str, p: dict, genome: List[str], rng: random.Random) -> dict:
    p = dict(p)
    if category == "alignment":
        p["win_a"] = _pick_window(genome, p["len_a"], rng)
        p["win_b"] = _pick_window(genome, p["len_b"], rng)
        p["len_a"], p["len_b"] = p["win_a"][2], p["win_b"][2]
    else:
        p["win_a"] = _pick_window(genome, p["length"], rng)
        p["length"] = p["win_a"][2]
        if category == "motif_search":
            p["win_motif"] = _pick_window(genome, p["motif_len"], rng)  # a motif that occurs
    return p


def load_genome(fasta: Optional[str], rng: random.Random, synth_len: int = 2_000_000,
                report: Optional[dict] = None) -> List[str]:
    """Validated sequences from a FASTA file, or one locally generated sequence."""
    if fasta:
        recs, rep = bio_ops.load_sequences(fasta)
        if report is not None:
            report.update(rep)
        return [s for _, s in recs]
    if report is not None:
        report.update({"file": None, "parser": "random_dna", "records_valid": 1, "total_bases": synth_len})
    return [bio_ops.random_dna(synth_len, rng, gc_bias=0.5)]


def calibrate(fasta: Optional[str] = None, seed: int = 0, points: int = 8,
              size_scale: float = 0.3, repeats: int = 3, verbose: bool = True) -> CostModel:
    """Measure each operation over a range of sizes and fit the cost model."""
    rng = random.Random(seed)
    genome = load_genome(fasta, rng)
    samples = []
    for cat in CATEGORIES:
        for _ in range(points):
            p = _assign_windows(cat, sample_params(cat, rng, size_scale), genome, rng)
            a, b, pp = materialise_inputs(cat, p, genome)
            samples.append((cat, p, _time_op(cat, pp, a, b, repeats)))
    cm = CostModel().fit(samples)
    if verbose:
        for cat in CATEGORIES:
            print(f"  {cat:13s} coef={['%.3g' % c for c in cm.coef[cat]]}  R^2={cm.r2.get(cat, float('nan')):.3f}")
    return cm


def generate_from_dataset(n: int = 60, fasta: Optional[str] = None, num_cores: int = 4,
                          load: float = 0.8, mix: str | Dict[str, float] = "medium",
                          seed: int = 0, size_scale: float = 0.3, repeats: int = 3,
                          cost_model: Optional[CostModel] = None, deadline_fraction: float = 0.3,
                          genome: Optional[List[str]] = None,
                          profile: Optional[dict] = None) -> List[Task]:
    """Dataset-based workload: every task is really executed and timed.

    burst      = measured runtime on this machine (median of `repeats`)
    est_burst  = cost-model prediction (what a scheduler knows beforehand)
    params     = operation parameters + genome coordinates (win_*) so the
                 identical task can be re-executed later (hardware validation)
    profile    = optional dict that receives input-validation, machine and
                 hardware-monitoring information
    """
    rng = random.Random(seed)
    report: dict = {}
    genome = genome or load_genome(fasta, rng, report=report)
    mix_w = MIXES[mix] if isinstance(mix, str) else mix
    cm = cost_model or CostModel()
    mon = HardwareMonitor()
    tasks = []
    for i in range(n):
        cat = _choose_category(rng, mix_w)
        p = _assign_windows(cat, sample_params(cat, rng, size_scale), genome, rng)
        a, b, pp = materialise_inputs(cat, p, genome)
        measured = _time_op(cat, pp, a, b, repeats)
        mon.sample()
        tasks.append(Task(i, cat, 0.0, round(cm.predict(cat, p), 4), round(max(measured, 0.01), 4),
                          rng.choice(PRIORITY_LEVELS), input_size(cat, p), None, p))
    if profile is not None:
        profile.update({"input": report, "machine": machine_info(), "monitor": mon.summary(),
                        "repeats": repeats, "size_scale": size_scale})
    return _finalise(tasks, num_cores, load, rng, deadline_fraction, (2.0, 6.0))


# ------------------------------------------------------------- CSV/JSON I/O
FIELDS = ["task_id", "category", "arrival", "est_burst", "burst", "priority", "input_size",
          "deadline", "params"]


def _to_row(t: Task) -> dict:
    d = asdict(t)
    d["params"] = json.dumps(d["params"])
    d["deadline"] = "" if t.deadline is None else t.deadline
    return d


def _from_dict(r: dict) -> Task:
    params = r["params"]
    if isinstance(params, str):
        params = json.loads(params or "{}")
    params = {k: tuple(v) if isinstance(v, list) else v for k, v in params.items()}
    dl = r.get("deadline")
    return Task(int(r["task_id"]), r["category"], float(r["arrival"]), float(r["est_burst"]),
                float(r["burst"]), int(r["priority"]), int(r["input_size"]),
                None if dl in (None, "") else float(dl), params)


def save_workload(tasks: Sequence[Task], path: str) -> None:
    """Write a workload as CSV or JSON (chosen by file extension)."""
    if path.endswith(".json"):
        with open(path, "w") as fh:
            json.dump([asdict(t) for t in tasks], fh, indent=1)
        return
    with open(path, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=FIELDS)
        w.writeheader()
        for t in tasks:
            w.writerow(_to_row(t))


def load_workload(path: str) -> List[Task]:
    if path.endswith(".json"):
        with open(path) as fh:
            return [_from_dict(d) for d in json.load(fh)]
    with open(path) as fh:
        return [_from_dict(r) for r in csv.DictReader(fh)]
