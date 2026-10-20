"""Module 7 (validation) - execute a schedule on real hardware.

The simulator's results are *simulation* evidence. This module runs the same
scheduling policy code as a real user-space dispatcher: tasks arrive in real
time, the policy picks the next task whenever one of W worker processes is
free, and the worker actually executes the bioinformatics operation on the
task's genome window. Waiting, response and turnaround are measured with
wall-clock time, so the results are *hardware* evidence for a user-space
dispatcher (still not an OS kernel scheduler).

Limitations (reported, not hidden):
  * Non-preemptive only: a Python worker cannot be paused mid-task, so
    SRTF / RR / quantum-based Adaptive are run as their non-preemptive
    variants (Adaptive-NP uses the same scoring without a quantum).
  * Measured waiting includes inter-process communication latency.
  * Other processes on the machine add noise; repeat runs to estimate it.
"""
from __future__ import annotations

import dataclasses
import multiprocessing as mp
import os
import queue
import time
from typing import List, Optional, Sequence

from . import bio_ops
from .schedulers import SchedContext, Scheduler
from .simulator import Core, SimResult, SimTask
from .workload import Task, materialise_inputs

_GENOME: Optional[List[str]] = None


def _init_worker(genome):
    global _GENOME
    _GENOME = genome


def _execute(category: str, params: dict):
    a, b, p = materialise_inputs(category, params, _GENOME)
    t0 = time.perf_counter()
    bio_ops.run_operation(category, p, a, b)
    return os.getpid(), (time.perf_counter() - t0) * 1000.0


def _warm(_):
    return os.getpid()


def execute_on_hardware(tasks: Sequence[Task], genome: List[str], scheduler: Scheduler,
                        workers: int = 2) -> SimResult:
    """Run `tasks` through `scheduler` on `workers` real processes."""
    if scheduler.preemptive or scheduler.quantum:
        raise ValueError(f"{scheduler.name}: hardware mode supports non-preemptive, "
                         "quantum-free policies only")
    for t in tasks:
        if "win_a" not in t.params:
            raise ValueError("hardware mode needs dataset-based tasks (generate_from_dataset)")
    try:
        mpctx = mp.get_context("fork")
    except ValueError:
        mpctx = mp.get_context()
    runtime = {t.task_id: SimTask(t.info()) for t in tasks}
    pending = sorted(runtime.values(), key=lambda s: (s.task.arrival, s.task.task_id))
    by_id = {t.task_id: t for t in tasks}
    n = len(pending)
    ctx = SchedContext(workers, None)
    est_sum = 0.0
    done_q: "queue.Queue" = queue.Queue()
    measured, pid_busy, overhead_ms = {}, {}, 0.0
    decisions = 0

    with mpctx.Pool(workers, initializer=_init_worker, initargs=(genome,)) as pool:
        pool.map(_warm, range(workers * 2))           # start-up cost outside the timed region
        t0 = time.perf_counter()
        now = lambda: (time.perf_counter() - t0) * 1000.0  # noqa: E731
        ready: List[SimTask] = []
        ai = done = 0
        free = workers
        seq = 0
        while done < n:
            t = now()
            while ai < n and pending[ai].task.arrival <= t:
                st = pending[ai]
                ai += 1
                ctx.arrived += 1
                est_sum += st.task.est_burst
                ctx.mean_est_burst = est_sum / ctx.arrived
                seq += 1
                st.ready_seq, st.state = seq, "READY"
                ready.append(st)
            while free and ready:
                t = now()
                k0 = time.perf_counter()
                keys = scheduler.keys(ready, t, ctx)
                overhead_ms += (time.perf_counter() - k0) * 1000
                decisions += 1
                best = ready.pop(min(range(len(ready)), key=keys.__getitem__))
                best.state, best.first_start = "RUNNING", now()
                free -= 1
                info = by_id[best.task.task_id]
                pool.apply_async(_execute, (info.category, info.params),
                                 callback=lambda r, tid=info.task_id: done_q.put((tid, r, time.perf_counter())),
                                 error_callback=lambda e: done_q.put(("error", e, 0)))
            wait_s = None
            if ai < n:
                wait_s = max(0.0, (pending[ai].task.arrival - now()) / 1000.0)
            try:
                item = done_q.get(timeout=wait_s)
            except queue.Empty:
                continue
            while True:
                tid, r, tc = item
                if tid == "error":
                    raise RuntimeError(f"worker failed: {r!r}")
                pid, b_ms = r
                st = runtime[tid]
                st.completion = (tc - t0) * 1000.0
                st.executed = b_ms
                st.state = "TERMINATED"
                measured[tid] = b_ms
                pid_busy[pid] = pid_busy.get(pid, 0.0) + b_ms
                done += 1
                free += 1
                try:
                    item = done_q.get_nowait()
                except queue.Empty:
                    break

    # first_start is the dispatch time; the measured burst B is the time the
    # worker spent inside the operation, everything else counts as waiting
    workload = {tid: dataclasses.replace(by_id[tid], burst=max(measured[tid], 1e-6)) for tid in by_id}
    for st in runtime.values():
        st.first_start = min(st.first_start, st.completion - measured[st.task.task_id])
    res = SimResult(scheduler.name, workers, list(runtime.values()), workload, evidence="hardware")
    res.makespan_start = min(t.arrival for t in tasks)
    res.makespan_end = max(s.completion for s in runtime.values())
    res.cores = [Core(i, busy_time=b) for i, b in enumerate(sorted(pid_busy.values(), reverse=True))]
    res.decisions, res.sched_overhead_ms = decisions, overhead_ms
    res.sched_stats = dict(ctx.stats)
    return res
