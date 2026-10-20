"""pytest tests for Modules 1-7 (run:  pytest -q)."""
import dataclasses
import os
import random
import sys

import pandas as pd
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from bioschedsim import analysis as an
from bioschedsim import bio_ops
from bioschedsim.metrics import check_invariants, summarize
from bioschedsim.repository import (ExperimentConfig, WorkloadManager, WorkloadRepository,
                                    WorkloadValidationError, fingerprint)
from bioschedsim.schedulers import (FCFS, SJF, SRTF, AdaptiveScheduler, Scheduler, by_name,
                                    default_suite)
from bioschedsim.simulator import simulate
from bioschedsim.workload import (Task, TaskInfo, generate_from_dataset, generate_synthetic,
                                  load_genome, load_workload, save_workload, size_class)


def mk(spec, deadline=None):
    return [Task(i, "gc_content", a, b, b, p, 1000, deadline, {}) for i, (a, b, p) in enumerate(spec)]


# ------------------------------------------------------------ Module 1
def test_validate_sequence_rejects_bad_input():
    assert bio_ops.validate_sequence("acg t\nn") == "ACGTN"
    with pytest.raises(bio_ops.SequenceValidationError):
        bio_ops.validate_sequence("ACGTXZ")
    with pytest.raises(bio_ops.SequenceValidationError):
        bio_ops.validate_sequence("")


def test_load_sequences_reports_rejected(tmp_path):
    p = tmp_path / "x.fa"
    p.write_text(">good\n" + "ACGT" * 30 + "\n>bad\nACGTQQ\n>short\nACG\n")
    recs, rep = bio_ops.load_sequences(str(p), min_length=50)
    assert [h for h, _ in recs] == ["good"]
    assert rep["records_read"] == 3 and rep["records_valid"] == 1 and len(rep["rejected"]) == 2


def test_workload_mix_has_size_classes_and_deadlines():
    wl = generate_synthetic(300, 4, 0.9, "medium", seed=1)
    classes = {size_class(t.est_burst) for t in wl}
    assert classes == {"short", "medium", "long"}
    dl = [t for t in wl if t.deadline is not None]
    assert 0.2 < len(dl) / len(wl) < 0.4
    assert all(t.deadline >= t.arrival for t in dl)


@pytest.mark.parametrize("ext", ["csv", "json"])
def test_roundtrip(tmp_path, ext):
    genome = load_genome(None, random.Random(0), synth_len=50_000)
    wl = generate_from_dataset(6, genome=genome, seed=1, size_scale=0.05, repeats=1)
    p = str(tmp_path / f"w.{ext}")
    save_workload(wl, p)
    assert fingerprint(load_workload(p)) == fingerprint(wl)


# ------------------------------------------------------------ Module 2
def test_scheduler_cannot_see_actual_burst():
    seen = []

    class Snoop(Scheduler):
        name = "snoop"

        def keys(self, cands, now, ctx):
            for c in cands:
                seen.append(type(c.task))
                assert not hasattr(c.task, "burst")
                assert not hasattr(ctx, "_rem")
            return [(c.task.arrival,) for c in cands]

    simulate(generate_synthetic(30, 2, 0.9, seed=2), Snoop(), 2)
    assert seen and all(t is TaskInfo for t in seen)


def test_estimate_differs_from_actual_but_scheduler_uses_estimate():
    # true burst 1 vs 100, estimates reversed: SJF must follow the estimates
    wl = [Task(0, "gc_content", 0, 100, 1, 3, 1, None, {}), Task(1, "gc_content", 0, 1, 100, 3, 1, None, {}),
          Task(2, "gc_content", 0, 0.5, 5, 3, 1, None, {})]
    r = simulate(wl, SJF(), 1)
    order = [s.task_id for s in sorted(r.segments, key=lambda s: s.start)]
    assert order == [2, 1, 0]


# ------------------------------------------------------------ Module 3
def test_repository_validation():
    with pytest.raises(WorkloadValidationError):
        WorkloadRepository([])
    bad = mk([(0, 5, 1), (0, 5, 1)])
    bad[1] = dataclasses.replace(bad[1], task_id=0)
    with pytest.raises(WorkloadValidationError):
        WorkloadRepository(bad)
    with pytest.raises(WorkloadValidationError):
        WorkloadRepository(mk([(0, 5, 9)]))
    with pytest.raises(WorkloadValidationError):
        WorkloadRepository([dataclasses.replace(mk([(5, 5, 1)])[0], deadline=1.0)])


def test_tasks_are_immutable():
    t = mk([(0, 5, 1)])[0]
    with pytest.raises(dataclasses.FrozenInstanceError):
        t.burst = 1


def test_manager_identical_inputs_and_fresh_state():
    repo = WorkloadRepository(generate_synthetic(80, 2, 0.9, seed=3))
    fp = repo.fingerprint
    recs = WorkloadManager(repo, ExperimentConfig(num_cores=2)).run(default_suite())
    assert len(recs) == 6 and {r.fingerprint for r in recs.values()} == {fp}
    assert repo.verify()
    states = [id(r.result.tasks[0]) for r in recs.values()]
    assert len(set(states)) == len(states)          # every run had its own run-time state


# ------------------------------------------------------------ Module 5
def test_adaptive_components():
    a = AdaptiveScheduler()
    assert a.burst_score(1, 1, 100) == 1.0 and a.burst_score(100, 1, 100) == 0.0
    assert a.priority_score(1) == 1.0 and a.priority_score(5) == 0.0
    assert a.urgency_score(None, 0, 1, 10) == 0.0
    assert a.urgency_score(5, 10, 1, 10) == 1.0                     # already late
    assert 0 < a.urgency_score(100, 0, 10, 10) < a.urgency_score(30, 0, 10, 10) < 1
    assert a.aging_score(80, 10) == pytest.approx(1.0)              # c = 8
    assert a.is_starving(201, 10) and not a.is_starving(199, 10)


def test_adaptive_tie_breaking_is_deterministic():
    wl = mk([(0, 5, 3)] * 4)
    r1 = simulate(wl, AdaptiveScheduler(), 1)
    order = [s.task_id for s in sorted(r1.segments, key=lambda s: s.start)]
    assert order[:4] == [0, 1, 2, 3]


def test_starvation_guard_overrides_score():
    # long low-priority task vs a continuous stream of short high-priority tasks
    spec = [(0, 1, 1), (0.5, 200, 5)] + [(i * 1.0, 1.0, 1) for i in range(1, 800)]
    no_guard = simulate(mk(spec), AdaptiveScheduler(w_wait=0.01, starvation_threshold=None, quantum=5), 1)
    guard = simulate(mk(spec), AdaptiveScheduler(w_wait=0.01, starvation_threshold=20, quantum=5), 1)
    first = lambda r: min(s.start for s in r.segments if s.task_id == 1)  # noqa: E731
    assert first(guard) < first(no_guard)
    assert guard.sched_stats.get("starvation_overrides", 0) > 0


def test_deadline_urgency_reduces_misses():
    wl = generate_synthetic(300, 4, 1.0, seed=5, deadline_fraction=0.5)
    m0 = summarize(simulate(wl, AdaptiveScheduler(w_deadline=0.0), 4, log_events=False))
    m1 = summarize(simulate(wl, AdaptiveScheduler(w_deadline=0.3), 4, log_events=False))
    assert m1["deadline_miss_rate"] <= m0["deadline_miss_rate"]


def test_multicore_never_double_assigns():
    for s in default_suite() + [AdaptiveScheduler(preemptive=True, quantum=None)]:
        check_invariants(simulate(generate_synthetic(150, 8, 1.1, seed=4), s, 8, 0.05))


# ------------------------------------------------------------ Module 6
def test_idle_intervals_and_events():
    r = simulate(mk([(0, 5, 1), (20, 5, 1)]), FCFS(), 2)
    evs = [e.event for e in r.events]
    assert "IDLE_START" in evs and "IDLE_END" in evs
    core0 = [(i.start, i.end) for i in r.idle if i.core_id == 0]
    assert (5.0, 20.0) in core0
    m = summarize(r)
    assert m["cpu_utilization"] + m["idle_fraction"] == pytest.approx(1.0)


def test_decision_log_has_breakdown():
    r = simulate(generate_synthetic(40, 2, 1.0, seed=6), AdaptiveScheduler(), 2, log_decisions=True)
    row = r.decisions_log[5]
    for k in ("burst_score", "aging_score", "priority_score", "urgency_score", "score", "starving"):
        assert k in row


def test_metrics_percentiles_and_deadlines():
    r = simulate(mk([(0, 10, 1), (0, 10, 1)], deadline=15), FCFS(), 1)
    m = summarize(r)
    assert m["p50_waiting"] == pytest.approx(5.0)
    assert m["deadline_miss_rate"] == 0.5 and m["avg_lateness_missed"] == pytest.approx(5.0)


# ------------------------------------------------------------ Module 7
def test_ci_and_relative_vs_absolute():
    df = pd.DataFrame([{"seed": s, "algorithm": a, "evidence": "simulation", "avg_waiting": v, "starved_tasks": z}
                       for s in range(5) for a, v, z in (("Base", 10.0 + s, 0.0), ("Adaptive", 5.0 + s, 1.0))])
    summ = an.summarize_runs(df, ["avg_waiting"])
    row = summ[summ.algorithm == "Base"].iloc[0]
    assert row["ci_low"] < 12 < row["ci_high"]
    comp = an.compare_to_baselines(df, "Adaptive", ["avg_waiting", "starved_tasks"])
    w = comp[comp.metric == "avg_waiting"].iloc[0]
    assert w["rel_change_pct"] == pytest.approx(5 / 12 * 100) and w["candidate_better"]
    z = comp[comp.metric == "starved_tasks"].iloc[0]
    assert z["report_as"] == "absolute" and pd.isna(z["rel_change_pct"])


def test_hardware_execution_small():
    from bioschedsim.hardware import execute_on_hardware
    genome = load_genome(None, random.Random(0), synth_len=100_000)
    wl = generate_from_dataset(8, genome=genome, num_cores=1, seed=3, size_scale=0.05, repeats=1)
    r = execute_on_hardware(wl, genome, by_name("adaptive-np"), workers=1)
    m = summarize(r)
    assert r.evidence == "hardware" and m["tasks"] == 8 and m["avg_waiting"] >= 0
    with pytest.raises(ValueError):
        execute_on_hardware(wl, genome, SRTF(), workers=1)
