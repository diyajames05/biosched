"""Phase 5 / Module 6 - Multicore discrete-event simulator with monitoring.

Models M CPU cores, task arrivals, dispatching, preemption, time-quantum
expiry, context-switch cost, idle intervals and completions in *simulated*
time. It never touches the operating system's real scheduler.

Event loop (time jumps from one event to the next):
    1. admit tasks whose arrival time <= now              (NEW -> READY)
    2. scheduling decision: fill idle cores; for preemptive policies
       displace a running task if a ready task ranks strictly better
    3. record idle intervals of cores that are still empty
    4. next event = min(next arrival, each core's completion / quantum end)
    5. advance, settle completions (RUNNING -> TERMINATED) and quantum
       expiries (RUNNING -> READY)

Information hiding: run-time states (SimTask) carry only TaskInfo (no actual
burst). The actual remaining work of each task lives in the simulator's
private ``_rem`` table, which policies never receive.

Logged event types: ARRIVE, DISPATCH, PREEMPT, QUANTUM_EXPIRE, COMPLETE,
IDLE_START, IDLE_END.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence

from .schedulers import SchedContext, Scheduler
from .workload import Task, TaskInfo

EPS = 1e-9
DONE_TOL = 1e-7
INF = float("inf")


@dataclass
class Event:
    time: float
    task_id: Optional[int]
    core_id: Optional[int]
    event: str
    from_state: str
    to_state: str


@dataclass
class Segment:
    core_id: int
    task_id: int
    category: str
    start: float
    end: float


@dataclass
class IdleInterval:
    core_id: int
    start: float
    end: float


class SimTask:
    """Mutable run-time state of one task inside one simulation run."""

    __slots__ = ("task", "executed", "first_start", "completion", "ready_seq", "state",
                 "core", "preemptions", "dispatches")

    def __init__(self, info: TaskInfo):
        self.task = info                 # scheduler-visible info, no actual burst
        self.executed = 0.0              # CPU time received so far
        self.first_start: Optional[float] = None
        self.completion: Optional[float] = None
        self.ready_seq = 0
        self.state = "NEW"
        self.core: Optional["Core"] = None
        self.preemptions = 0
        self.dispatches = 0

    def executed_at(self, now: float) -> float:
        if self.core is not None:
            return self.executed + max(0.0, now - self.core.seg_start)
        return self.executed

    def est_remaining(self, now: float) -> float:
        """Remaining work as a scheduler can know it: estimate - work done."""
        return max(self.task.est_burst - self.executed_at(now), 1e-6)

    def waited(self, now: float) -> float:
        """Accumulated waiting time W_i(t) = (t - A_i) - CPU received."""
        return max(0.0, now - self.task.arrival - self.executed_at(now))


@dataclass
class Core:
    core_id: int
    task: Optional[SimTask] = None
    seg_start: float = 0.0
    slice_end: float = INF
    last_task_id: Optional[int] = None
    busy_time: float = 0.0
    switch_time: float = 0.0
    idle_since: Optional[float] = None


@dataclass
class SimResult:
    algorithm: str
    num_cores: int
    tasks: List[SimTask]
    workload: Dict[int, Task]                       # canonical inputs (for metrics)
    events: List[Event] = field(default_factory=list)
    segments: List[Segment] = field(default_factory=list)
    idle: List[IdleInterval] = field(default_factory=list)
    decisions_log: List[dict] = field(default_factory=list)
    decisions: int = 0
    key_evaluations: int = 0
    sched_overhead_ms: float = 0.0
    context_switches: int = 0
    makespan_start: float = 0.0
    makespan_end: float = 0.0
    sched_stats: Dict[str, int] = field(default_factory=dict)
    cores: list = field(default_factory=list)
    workload_fingerprint: str = ""
    evidence: str = "simulation"


class Simulator:
    def __init__(self, scheduler: Scheduler, num_cores: int = 1, context_switch: float = 0.0,
                 log_events: bool = True, log_decisions: bool = False):
        if num_cores < 1:
            raise ValueError("num_cores must be >= 1")
        self.sched = scheduler
        self.M = num_cores
        self.cs = context_switch
        self.log_events = log_events
        self.log_decisions = log_decisions

    # ------------------------------------------------------------ helpers
    def _log(self, t, st, core, ev, frm, to):
        if self.log_events:
            self.result.events.append(Event(round(t, 6), None if st is None else st.task.task_id,
                                            None if core is None else core.core_id, ev, frm, to))

    def _keys(self, cands, now):
        t0 = time.perf_counter()
        k = self.sched.keys(cands, now, self.ctx)
        self.result.sched_overhead_ms += (time.perf_counter() - t0) * 1000
        self.result.decisions += 1
        self.result.key_evaluations += len(cands)
        return k

    def _next_stop(self, core: Core) -> float:
        if core.task is None:
            return INF
        return min(core.seg_start + self._rem[core.task.task.task_id], core.slice_end)

    def _enqueue(self, st: SimTask):
        self._seq += 1
        st.ready_seq = self._seq
        st.state = "READY"
        self.ready.append(st)

    def _record_decision(self, now, core, chosen, keys):
        if not self.log_decisions:
            return
        entry = {"time": round(now, 6), "core_id": core.core_id, "task_id": chosen.task.task_id,
                 "ready_size": len(self.ready)}
        if hasattr(self.sched, "explain"):
            ex = self.sched.explain(self.ready, now, self.ctx)
            mine = next(e for e in ex if e["task_id"] == chosen.task.task_id)
            entry.update({k: (round(v, 4) if isinstance(v, float) else v)
                          for k, v in mine.items() if k != "task_id"})
            order = sorted(range(len(self.ready)), key=keys.__getitem__)
            if len(order) > 1:
                entry["runner_up"] = self.ready[order[1]].task.task_id
        self.result.decisions_log.append(entry)

    def _start(self, st: SimTask, core: Core, now: float):
        # a task can be on only one core; a running/finished task is never re-dispatched
        assert st.core is None and st.state == "READY", f"task {st.task.task_id} not dispatchable"
        assert core.task is None, f"core {core.core_id} busy"
        self.ready.remove(st)
        if core.idle_since is not None:
            if now > core.idle_since + EPS:
                self.result.idle.append(IdleInterval(core.core_id, core.idle_since, now))
            self._log(now, None, core, "IDLE_END", "IDLE", "BUSY")
            core.idle_since = None
        same = core.last_task_id == st.task.task_id
        switch = self.cs if (self.cs > 0 and not same) else 0.0
        if not same:
            self.result.context_switches += 1
        core.task, st.core = st, core
        core.seg_start = now + switch
        core.switch_time += switch
        q = self.sched.quantum
        core.slice_end = core.seg_start + q if q else INF
        st.state = "RUNNING"
        st.dispatches += 1
        self._log(now, st, core, "DISPATCH", "READY", "RUNNING")

    def _stop(self, core: Core, now: float, event: str):
        st = core.task
        tid = st.task.task_id
        ran = min(max(0.0, now - core.seg_start), self._rem[tid])
        if ran > 0:
            if st.first_start is None:
                st.first_start = core.seg_start
            self.result.segments.append(Segment(core.core_id, tid, st.task.category,
                                                core.seg_start, core.seg_start + ran))
        st.executed += ran
        self._rem[tid] -= ran
        core.busy_time += ran
        core.last_task_id = tid
        core.task, st.core = None, None
        if self._rem[tid] <= DONE_TOL:
            event = "COMPLETE"
        if event == "COMPLETE":
            self._rem[tid] = 0.0
            st.completion = now
            st.state = "TERMINATED"
            self._done += 1
            self._log(now, st, core, "COMPLETE", "RUNNING", "TERMINATED")
        else:
            if event == "PREEMPT":
                st.preemptions += 1
            self._log(now, st, core, event, "RUNNING", "READY")
            self._enqueue(st)

    # ----------------------------------------------------- decision point
    def _schedule(self, now: float):
        swaps_left = 4 * (self.M + len(self.ready))   # guard against preemption cycles
        while self.ready:
            idle = [c for c in self.cores if c.task is None]
            running = [c.task for c in self.cores if c.task is not None]
            if idle:
                keys = self._keys(self.ready, now)
                best = self.ready[min(range(len(self.ready)), key=keys.__getitem__)]
                core = next((c for c in idle if c.last_task_id == best.task.task_id), idle[0])
                self._record_decision(now, core, best, keys)
                self._start(best, core, now)
                continue
            if not self.sched.preemptive:
                return
            pool = self.ready + running
            keys = self._keys(pool, now)
            nr = len(self.ready)
            bi = min(range(nr), key=keys.__getitem__)
            wi = max(range(nr, len(pool)), key=keys.__getitem__)
            if keys[bi] < keys[wi] and swaps_left > 0:      # strictly better -> preempt
                swaps_left -= 1
                best, victim = self.ready[bi], pool[wi]
                core = victim.core
                self._stop(core, now, "PREEMPT")
                self._start(best, core, now)
            else:
                return

    # --------------------------------------------------------------- run
    def run(self, workload: Sequence[Task]) -> SimResult:
        canon = {t.task_id: t for t in workload}
        tasks = [SimTask(t.info()) for t in workload]          # fresh run-time state
        self._rem = {t.task_id: t.burst for t in workload}     # private: actual work left
        pending = sorted(tasks, key=lambda s: (s.task.arrival, s.task.task_id))
        n = len(pending)
        self.cores = [Core(i) for i in range(self.M)]
        self.ready: List[SimTask] = []
        self.ctx = SchedContext(self.M, self.sched.quantum)
        self.result = SimResult(self.sched.name, self.M, tasks, canon)
        self._seq = self._done = 0
        est_sum = 0.0
        ai = 0
        now = pending[0].task.arrival if n else 0.0
        self.result.makespan_start = now
        for c in self.cores:
            c.idle_since = now

        while self._done < n:
            while ai < n and pending[ai].task.arrival <= now + EPS:      # 1
                st = pending[ai]
                ai += 1
                self.ctx.arrived += 1
                est_sum += st.task.est_burst
                self.ctx.mean_est_burst = est_sum / self.ctx.arrived
                self._log(st.task.arrival, st, None, "ARRIVE", "NEW", "READY")
                self._enqueue(st)
            self._schedule(now)                                           # 2
            for c in self.cores:                                          # 3
                if c.task is None and c.idle_since is None:
                    c.idle_since = now
                    self._log(now, None, c, "IDLE_START", "BUSY", "IDLE")
            nxt = pending[ai].task.arrival if ai < n else INF             # 4
            for c in self.cores:
                nxt = min(nxt, self._next_stop(c))
            if nxt == INF:
                raise RuntimeError("simulation deadlock: no runnable task and no future arrival")
            now = max(now, nxt)
            for c in self.cores:                                          # 5
                if c.task is not None and self._next_stop(c) <= now + EPS:
                    left = self._rem[c.task.task.task_id] - (now - c.seg_start)
                    self._stop(c, now, "COMPLETE" if left <= DONE_TOL else "QUANTUM_EXPIRE")

        end = max(t.completion for t in tasks) if n else 0.0
        self.result.makespan_end = end
        for c in self.cores:                     # close idle intervals at the end
            if c.idle_since is not None and end > c.idle_since + EPS:
                self.result.idle.append(IdleInterval(c.core_id, c.idle_since, end))
        self.result.cores = self.cores
        self.result.sched_stats = dict(self.ctx.stats)
        return self.result


def simulate(workload: Sequence[Task], scheduler: Scheduler, num_cores: int = 1,
             context_switch: float = 0.0, log_events: bool = True,
             log_decisions: bool = False) -> SimResult:
    return Simulator(scheduler, num_cores, context_switch, log_events, log_decisions).run(workload)
