"""Modules 4 & 5 - Scheduling policies behind one common interface.

Every policy implements ``keys(candidates, now, ctx)`` and returns one sort
key per candidate: the *smallest* key is served first. Candidates are
run-time task states whose ``.task`` is a ``TaskInfo``, which has no actual
burst field, so a policy cannot read future information. ``ctx`` is a
``SchedContext`` holding only information a real scheduler would have.

Attributes read by the simulator:
    preemptive : may a better ready task displace a running one at an event?
    quantum    : time slice (ms) after which a running task returns to the
                 ready queue; None = run to completion.

Baselines (Module 4)     preemptive  quantum
    FCFS                    no         -
    SJF  (non-preemptive)   no         -
    SRTF (preemptive SJF)   yes        -     reported under its own label
    RR                      no         q
    Priority (non-pre.)     no         -
    Priority-P (preempt.)   yes        -
Proposed (Module 5)
    Adaptive                no         q     re-ranked at every quantum boundary
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional


@dataclass
class SchedContext:
    """What a scheduler may know besides the ready queue."""
    num_cores: int
    quantum: Optional[float]
    mean_est_burst: float = 0.0          # running mean over *arrived* tasks
    arrived: int = 0
    stats: Dict[str, int] = field(default_factory=dict)


class Scheduler:
    name = "base"
    preemptive = False
    quantum = None

    def keys(self, cands, now, ctx: SchedContext) -> List:
        raise NotImplementedError

    def __repr__(self):
        return self.name


class FCFS(Scheduler):
    """Ready queue ordered by arrival time."""
    name = "FCFS"

    def keys(self, cands, now, ctx):
        return [(c.task.arrival, c.task.task_id) for c in cands]


class SJF(Scheduler):
    """Non-preemptive Shortest Job First on the *estimated* burst."""
    name = "SJF"

    def keys(self, cands, now, ctx):
        return [(c.task.est_burst, c.task.arrival, c.task.task_id) for c in cands]


class SRTF(Scheduler):
    """Preemptive variant: Shortest (estimated) Remaining Time First."""
    name = "SRTF"
    preemptive = True

    def keys(self, cands, now, ctx):
        return [(c.est_remaining(now), c.task.arrival, c.task.task_id) for c in cands]


class RoundRobin(Scheduler):
    """Fixed quantum q, cyclic order of (re-)entry into the ready queue."""

    def __init__(self, quantum: float = 10.0):
        self.quantum = quantum
        self.name = f"RR(q={quantum:g})"

    def keys(self, cands, now, ctx):
        return [(c.ready_seq,) for c in cands]


class PriorityScheduler(Scheduler):
    """Highest configured priority first (1 = highest). FCFS among equals."""

    def __init__(self, preemptive: bool = False):
        self.preemptive = preemptive
        self.name = "Priority-P" if preemptive else "Priority"

    def keys(self, cands, now, ctx):
        return [(c.task.priority, c.task.arrival, c.task.task_id) for c in cands]


# =====================================================================
#  Module 5 - Adaptive Bioinformatics CPU Scheduler
# =====================================================================
@dataclass
class Evaluation:
    """Score breakdown for one candidate at one decision point."""
    task_id: int
    remaining_est: float
    waited: float
    burst_score: float
    aging_score: float
    priority_score: float
    urgency_score: float
    score: float
    starving: bool


class AdaptiveScheduler(Scheduler):
    """Workload-aware adaptive scheduler.

    Functional components (one method each, in pipeline order):
      1. features()        feature extraction & normalisation context
      2. burst_score()     workload awareness      s_i in [0, 1]
      3. aging_score()     waiting-time awareness  a_i >= 0 (unbounded)
      4. priority_score()  priority awareness      p_i in [0, 1]
      5. urgency_score()   optional deadline urgency u_i in [0, 1]
      6. combine()         S_i = w_s s_i + w_w a_i + w_p p_i + w_d u_i
      7. tie-breaking      higher S, then earlier arrival, then lower ID
      8. is_starving()     W_i > theta * b_bar  -> served before any non-starving task
      9. keys()/explain()  dispatch decision + human-readable breakdown

    Definitions (r_i = estimated remaining work, W_i = accumulated waiting,
    b_bar = running mean of estimated bursts of arrived tasks):
        s_i = 1 - ln(r_i / r_min) / ln(r_max / r_min)   (over the ready set)
        a_i = W_i / (c * b_bar)
        p_i = (5 - priority_i) / 4
        u_i = 0 without deadline; 1 if slack <= 0; b_bar / (b_bar + slack) otherwise,
              slack = deadline - now - r_i

    Running tasks are never candidates (the simulator only passes the ready
    queue), completed tasks never return to it, and on multicore the
    simulator removes each selected task before choosing the next, so no
    task is assigned to two cores.
    """

    def __init__(self, w_short: float = 0.45, w_wait: float = 0.30, w_prio: float = 0.15,
                 w_deadline: float = 0.10, aging_c: float = 8.0, starvation_threshold: float = 20.0,
                 quantum: Optional[float] = 10.0, preemptive: bool = False, name: Optional[str] = None):
        self.w_s, self.w_w, self.w_p, self.w_d = w_short, w_wait, w_prio, w_deadline
        self.c = aging_c
        self.theta = starvation_threshold      # None/inf disables the explicit guard
        self.quantum = quantum
        self.preemptive = preemptive
        self.name = name or "Adaptive"

    # 1 ------------------------------------------------------------------
    def features(self, cands, now, ctx):
        rem = [max(c.est_remaining(now), 1e-6) for c in cands]
        b_bar = max(ctx.mean_est_burst, 1e-6)
        return rem, min(rem), max(rem), b_bar

    # 2 ------------------------------------------------------------------
    @staticmethod
    def burst_score(r, r_min, r_max):
        if r_max <= r_min:
            return 1.0
        return 1.0 - math.log(r / r_min) / math.log(r_max / r_min)

    # 3 ------------------------------------------------------------------
    def aging_score(self, waited, b_bar):
        return waited / (self.c * b_bar)

    # 4 ------------------------------------------------------------------
    @staticmethod
    def priority_score(priority):
        return (5 - priority) / 4.0

    # 5 ------------------------------------------------------------------
    @staticmethod
    def urgency_score(deadline, now, r, b_bar):
        if deadline is None:
            return 0.0
        slack = deadline - now - r
        return 1.0 if slack <= 0 else b_bar / (b_bar + slack)

    # 6 ------------------------------------------------------------------
    def combine(self, s, a, p, u):
        return self.w_s * s + self.w_w * a + self.w_p * p + self.w_d * u

    # 8 ------------------------------------------------------------------
    def is_starving(self, waited, b_bar):
        return self.theta is not None and waited > self.theta * b_bar

    def evaluate(self, cands, now, ctx) -> List[Evaluation]:
        rem, r_min, r_max, b_bar = self.features(cands, now, ctx)
        out = []
        for c, r in zip(cands, rem):
            w = c.waited(now)
            s = self.burst_score(r, r_min, r_max)
            a = self.aging_score(w, b_bar)
            p = self.priority_score(c.task.priority)
            u = self.urgency_score(c.task.deadline, now, r, b_bar)
            out.append(Evaluation(c.task.task_id, r, w, s, a, p, u, self.combine(s, a, p, u),
                                  self.is_starving(w, b_bar)))
        return out

    # 7 + 9 ---------------------------------------------------------------
    def keys(self, cands, now, ctx):
        ev = self.evaluate(cands, now, ctx)
        keys = [((0, -e.waited) if e.starving else (1, -e.score)) + (c.task.arrival, c.task.task_id)
                for e, c in zip(ev, cands)]
        if any(e.starving for e in ev):
            by_score = max(range(len(ev)), key=lambda i: (ev[i].score, -cands[i].task.arrival))
            if not ev[by_score].starving:
                ctx.stats["starvation_overrides"] = ctx.stats.get("starvation_overrides", 0) + 1
        return keys

    def explain(self, cands, now, ctx) -> List[dict]:
        return [e.__dict__ for e in self.evaluate(cands, now, ctx)]


def default_suite(quantum: float = 10.0):
    """Algorithms compared in the experiments (each a fresh instance)."""
    return [FCFS(), SJF(), SRTF(), RoundRobin(quantum), PriorityScheduler(),
            AdaptiveScheduler(quantum=quantum)]


def by_name(name: str, quantum: float = 10.0) -> Scheduler:
    table = {
        "fcfs": FCFS, "sjf": SJF, "srtf": SRTF,
        "rr": lambda: RoundRobin(quantum), "priority": PriorityScheduler,
        "priority-p": lambda: PriorityScheduler(True),
        "adaptive": lambda: AdaptiveScheduler(quantum=quantum),
        "adaptive-np": lambda: AdaptiveScheduler(quantum=None, name="Adaptive-NP"),
    }
    return table[name.lower()]()
