"""Module 6 - Per-task and system-wide performance metrics.

For task i:   T_i = C_i - A_i      turnaround
              W_i = T_i - B_i      waiting       (B_i = actual CPU service)
              R_i = S_i - A_i      response      (S_i = first start of execution)
              SD_i = T_i / B_i     slowdown (normalised turnaround)
              lateness_i = C_i - deadline_i   (tasks with a deadline)

System level:
    throughput        = N / (C_max - A_min)                    tasks per second
    CPU utilisation   = sum(B_i) / (M * (C_max - A_min))        normalised by M*T
    idle fraction     = total idle core time / (M * T)
    Jain fairness     = (sum x)^2 / (N * sum x^2), x_i = 1/SD_i  (1 = perfectly fair)
    starved tasks     = #tasks with W_i > starvation_factor * mean(B)
    deadline miss rate= share of tasks with a deadline that finish after it
    percentiles       = p50 / p90 / p95 / p99 of W, T and R
"""
from __future__ import annotations

import statistics
from collections import defaultdict
from typing import Dict, List

from .simulator import SimResult
from .workload import size_class

PCTS = (50, 90, 95, 99)


def per_task(result: SimResult) -> List[dict]:
    rows = []
    for st in result.tasks:
        info = st.task
        B = result.workload[info.task_id].burst
        T = st.completion - info.arrival
        W = max(T - B, 0.0)
        rows.append({
            "algorithm": result.algorithm, "task_id": info.task_id, "category": info.category,
            "size_class": size_class(info.est_burst), "priority": info.priority,
            "input_size": info.input_size, "arrival": info.arrival, "burst": B,
            "est_burst": info.est_burst, "deadline": info.deadline,
            "first_start": st.first_start, "completion": st.completion,
            "turnaround": T, "waiting": W, "response": st.first_start - info.arrival,
            "slowdown": T / B, "preemptions": st.preemptions, "dispatches": st.dispatches,
            "missed_deadline": (None if info.deadline is None else st.completion > info.deadline + 1e-9),
            "lateness": (None if info.deadline is None else st.completion - info.deadline),
        })
    return rows


def pct(xs, q):
    xs = sorted(xs)
    if not xs:
        return 0.0
    k = (len(xs) - 1) * q / 100
    lo, hi = int(k), min(int(k) + 1, len(xs) - 1)
    return xs[lo] + (xs[hi] - xs[lo]) * (k - lo)


def summarize(result: SimResult, starvation_factor: float = 10.0) -> Dict:
    rows = per_task(result)
    n = len(rows)
    W = [r["waiting"] for r in rows]
    T = [r["turnaround"] for r in rows]
    R = [r["response"] for r in rows]
    SD = [r["slowdown"] for r in rows]
    span = result.makespan_end - result.makespan_start
    busy = sum(r["burst"] for r in rows)
    mean_b = statistics.fmean(r["burst"] for r in rows)
    inv = [1 / s for s in SD]
    capacity = result.num_cores * span
    idle_total = sum(i.end - i.start for i in result.idle)
    starv_thr = starvation_factor * mean_b

    out = {
        "algorithm": result.algorithm,
        "evidence": result.evidence,
        "cores": result.num_cores,
        "tasks": n,
        "avg_waiting": statistics.fmean(W),
        "avg_turnaround": statistics.fmean(T),
        "avg_response": statistics.fmean(R),
        "max_waiting": max(W),
        "std_waiting": statistics.pstdev(W),
        "avg_slowdown": statistics.fmean(SD),
        "max_slowdown": max(SD),
        "throughput_per_s": n / span * 1000 if span > 0 else 0.0,
        "cpu_utilization": busy / capacity if span > 0 else 0.0,
        "idle_fraction": idle_total / capacity if span > 0 else 0.0,
        "jain_fairness": sum(inv) ** 2 / (n * sum(x * x for x in inv)),
        "starved_tasks": sum(1 for w in W if w > starv_thr),
        "max_wait_over_mean_burst": max(W) / mean_b,
        "makespan": span,
        "context_switches": result.context_switches,
        "preemptions": sum(r["preemptions"] for r in rows),
        "decisions": result.decisions,
        "key_evaluations": result.key_evaluations,
        "sched_overhead_ms": result.sched_overhead_ms,
        "starvation_overrides": result.sched_stats.get("starvation_overrides", 0),
    }
    for p in PCTS:
        out[f"p{p}_waiting"] = pct(W, p)
        out[f"p{p}_turnaround"] = pct(T, p)
        out[f"p{p}_response"] = pct(R, p)
    dl = [r for r in rows if r["deadline"] is not None]
    out["deadline_tasks"] = len(dl)
    out["deadline_miss_rate"] = (sum(r["missed_deadline"] for r in dl) / len(dl)) if dl else 0.0
    out["avg_lateness_missed"] = (statistics.fmean(r["lateness"] for r in dl if r["missed_deadline"])
                                  if any(r["missed_deadline"] for r in dl) else 0.0)
    hi = [r["waiting"] for r in rows if r["priority"] <= 2]
    out["wait_high_priority"] = statistics.fmean(hi) if hi else 0.0
    lo = [r["waiting"] for r in rows if r["priority"] >= 4]
    out["wait_low_priority"] = statistics.fmean(lo) if lo else 0.0
    for key in ("category", "size_class"):
        groups = defaultdict(list)
        for r in rows:
            groups[r[key]].append(r)
        for g, rs in groups.items():
            out[f"wait_{g}"] = statistics.fmean(r["waiting"] for r in rs)
            out[f"slowdown_{g}"] = statistics.fmean(r["slowdown"] for r in rs)
    for c in result.cores:
        out[f"util_core{c.core_id}"] = c.busy_time / span if span > 0 else 0.0
    return out


def check_invariants(result: SimResult) -> None:
    """Sanity checks used by the tests and every experiment run."""
    for st in result.tasks:
        t = st.task
        B = result.workload[t.task_id].burst
        assert st.completion is not None, f"task {t.task_id} never completed"
        assert st.state == "TERMINATED"
        assert abs(st.executed - B) < 1e-6, f"task {t.task_id} executed {st.executed} != {B}"
        assert st.first_start >= t.arrival - 1e-9
        assert st.completion >= st.first_start + B - 1e-6
    by_core, by_task = defaultdict(list), defaultdict(list)
    for s in result.segments:
        by_core[s.core_id].append((s.start, s.end))
        by_task[s.task_id].append((s.start, s.end))
    for segs in list(by_core.values()) + list(by_task.values()):
        segs.sort()
        for (a0, a1), (b0, b1) in zip(segs, segs[1:]):
            assert b0 >= a1 - 1e-6, f"overlapping segments {a0, a1} / {b0, b1}"
    # busy + idle + context-switch time never exceeds capacity
    span = result.makespan_end - result.makespan_start
    for c in result.cores:
        idle = sum(i.end - i.start for i in result.idle if i.core_id == c.core_id)
        assert c.busy_time + idle <= span + 1e-6, f"core {c.core_id} over-accounted"
