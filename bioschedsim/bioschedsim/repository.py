"""Module 3 - Task repository and workload manager.

WorkloadRepository
    Stores the canonical workload as an immutable tuple of frozen Task
    records, validates it, and fingerprints it (SHA-256 of a canonical JSON
    encoding). The fingerprint proves that every algorithm received exactly
    the same task inputs.

ExperimentConfig
    Number of cores, time quantum, context-switch cost, seeds and other
    simulation parameters, kept in one frozen object.

WorkloadManager
    Creates a *fresh* simulator instance (and fresh run-time task state) for
    each algorithm, checks the fingerprint before and after each run, and
    returns the results keyed by algorithm name.

Immutable input information (Task) is kept separate from mutable run-time
state (simulator.SimTask), which is created anew inside every run.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from .workload import PRIORITY_LEVELS, Task, load_workload, save_workload


class WorkloadValidationError(ValueError):
    pass


def validate_workload(tasks: Sequence[Task]) -> None:
    """Reject inconsistent workloads before any algorithm sees them."""
    if not tasks:
        raise WorkloadValidationError("workload is empty")
    ids = [t.task_id for t in tasks]
    if len(set(ids)) != len(ids):
        raise WorkloadValidationError("duplicate task IDs")
    for t in tasks:
        problems = []
        if t.arrival < 0:
            problems.append("negative arrival")
        if not t.burst > 0:
            problems.append("actual burst must be > 0")
        if not t.est_burst > 0:
            problems.append("estimated burst must be > 0")
        if t.priority not in PRIORITY_LEVELS:
            problems.append(f"priority {t.priority} not in {PRIORITY_LEVELS}")
        if t.deadline is not None and t.deadline < t.arrival:
            problems.append("deadline before arrival")
        if problems:
            raise WorkloadValidationError(f"task {t.task_id}: " + ", ".join(problems))


def fingerprint(tasks: Iterable[Task]) -> str:
    canon = json.dumps([asdict(t) for t in sorted(tasks, key=lambda t: t.task_id)],
                       sort_keys=True, separators=(",", ":"), default=list)
    return hashlib.sha256(canon.encode()).hexdigest()


class WorkloadRepository:
    def __init__(self, tasks: Sequence[Task], name: str = "workload", meta: Optional[dict] = None):
        validate_workload(tasks)
        self._tasks: Tuple[Task, ...] = tuple(sorted(tasks, key=lambda t: t.task_id))
        self.name = name
        self.meta = dict(meta or {})
        self.fingerprint = fingerprint(self._tasks)

    @property
    def tasks(self) -> Tuple[Task, ...]:
        return self._tasks

    def __len__(self):
        return len(self._tasks)

    def verify(self, tasks: Optional[Iterable[Task]] = None) -> bool:
        return fingerprint(self._tasks if tasks is None else tasks) == self.fingerprint

    def save(self, path: str) -> None:
        save_workload(self._tasks, path)

    @classmethod
    def load(cls, path: str, name: Optional[str] = None) -> "WorkloadRepository":
        return cls(load_workload(path), name or path)


@dataclass(frozen=True)
class ExperimentConfig:
    num_cores: int = 4
    quantum: float = 10.0
    context_switch: float = 0.05
    seeds: Tuple[int, ...] = tuple(range(1000, 1010))
    starvation_factor: float = 10.0
    log_events: bool = False
    log_decisions: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class RunRecord:
    algorithm: str
    result: object                    # simulator.SimResult
    fingerprint: str
    config: dict = field(default_factory=dict)


class WorkloadManager:
    """Runs several algorithms on one repository under one configuration."""

    def __init__(self, repo: WorkloadRepository, config: ExperimentConfig = ExperimentConfig()):
        self.repo = repo
        self.config = config

    def run(self, schedulers: Sequence, check: bool = True) -> Dict[str, RunRecord]:
        from .metrics import check_invariants
        from .simulator import Simulator
        out: Dict[str, RunRecord] = {}
        for sched in schedulers:
            if not self.repo.verify():
                raise RuntimeError("canonical workload changed between runs")
            sim = Simulator(sched, self.config.num_cores, self.config.context_switch,
                            log_events=self.config.log_events, log_decisions=self.config.log_decisions)
            res = sim.run(self.repo.tasks)        # simulator builds its own run-time state
            if check:
                check_invariants(res)
            res.workload_fingerprint = self.repo.fingerprint
            out[sched.name] = RunRecord(sched.name, res, self.repo.fingerprint, self.config.to_dict())
        if not self.repo.verify():
            raise RuntimeError("canonical workload was modified by a run")
        fps = {r.fingerprint for r in out.values()}
        assert len(fps) == 1, "algorithms received different inputs"
        return out


def run_seeds(make_workload: Callable[[int], List[Task]], schedulers_factory: Callable[[], list],
              config: ExperimentConfig) -> Dict[int, Dict[str, RunRecord]]:
    """Repeat a comparison over the configured seeds (fresh schedulers per seed)."""
    return {s: WorkloadManager(WorkloadRepository(make_workload(s), f"seed{s}"), config)
            .run(schedulers_factory()) for s in config.seeds}
