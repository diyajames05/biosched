"""Module 7 - Comparative experiments (Phase 7).

All algorithms run on *identical* workloads through the WorkloadManager
(same fingerprint per seed), repeated over 10 seeds. Tables give mean and
95 % confidence interval; Adaptive is compared with each baseline using
paired differences, relative change (absolute difference when the baseline
is ~0) and Wilcoxon signed-rank p-values.

    E1  baseline comparison            N=300, M=4, load 0.9, medium mix
    E2  load sweep                     congestion / starvation as arrivals grow
    E3  heterogeneity sweep            do improvements persist?
    E4  fairness to long tasks         per category and per size class
    E5  estimation-error sweep         robustness to wrong burst estimates
    E6  scalability                    task count & core count, scheduler overhead
    E7  adaptive sensitivity           weights, aging, guard, deadline term, quantum
    E8  dataset-based workload         validated FASTA, really executed & timed tasks
    E9  sequence-length sweep          all sequence lengths scaled x0.25 .. x4
    E10 hardware validation            real worker processes vs simulation
    +   Gantt charts (combined and per algorithm), event and decision logs

Evaluation seeds (1000+) are disjoint from the seeds used while designing
the adaptive policy (100-109).
"""
from __future__ import annotations

import dataclasses
import json
import os
import random
import time
from typing import Callable, Dict, List

import pandas as pd

from . import analysis as an
from . import viz
from .bio_ops import random_dna, write_fasta
from .metrics import per_task, summarize
from .repository import ExperimentConfig, WorkloadManager, WorkloadRepository, run_seeds
from .schedulers import AdaptiveScheduler, by_name, default_suite
from .simulator import simulate
from .workload import (calibrate, generate_from_dataset, generate_synthetic, load_genome,
                       machine_info, save_workload)

CFG = ExperimentConfig()          # 4 cores, q = 10 ms, cs = 0.05 ms, seeds 1000-1009
CANDIDATE = "Adaptive"

MAIN = [("avg_waiting", "Avg waiting (ms)"), ("avg_response", "Avg response (ms)"),
        ("avg_turnaround", "Avg turnaround (ms)"), ("p95_waiting", "p95 waiting (ms)"),
        ("max_waiting", "Max waiting (ms)"), ("avg_slowdown", "Avg slowdown")]
SYSTEM = [("cpu_utilization", "CPU utilisation"), ("throughput_per_s", "Throughput (tasks/s)"),
          ("idle_fraction", "Idle fraction"), ("context_switches", "Context switches"),
          ("sched_overhead_ms", "Scheduler overhead (ms)")]
FAIR = [("jain_fairness", "Jain fairness"), ("starved_tasks", "Starved tasks"),
        ("max_wait_over_mean_burst", "Max W / mean B"), ("wait_high_priority", "W prio 1-2 (ms)"),
        ("wait_low_priority", "W prio 4-5 (ms)"), ("deadline_miss_rate", "Deadline miss rate")]
PCT = [(f"p{p}_waiting", f"p{p} W") for p in (50, 90, 95, 99)] + \
      [(f"p{p}_response", f"p{p} R") for p in (50, 95)]
COMPARE = [("avg_waiting", "Avg W"), ("avg_response", "Avg R"), ("max_waiting", "Max W"),
           ("jain_fairness", "Jain"), ("deadline_miss_rate", "Miss rate")]
ALL_METRICS = sorted({k for k, _ in MAIN + SYSTEM + FAIR + PCT})


# ------------------------------------------------------------- helpers
def run_frame(make_wl: Callable[[int], list], factory: Callable = default_suite,
              cfg: ExperimentConfig = CFG, keep: bool = False, **extra):
    runs = run_seeds(make_wl, factory, cfg)
    frame = an.runs_to_frame({s: {a: summarize(r.result, cfg.starvation_factor) for a, r in d.items()}
                              for s, d in runs.items()}, **extra)
    return (frame, runs) if keep else frame


def write_csv(path, rows: List[dict]):
    pd.DataFrame(rows).to_csv(path, index=False)


def save(out, name, df: pd.DataFrame):
    df.to_csv(os.path.join(out, name), index=False, float_format="%.6g")


def sweep(out, name, param, values, make: Callable, cfg_for: Callable = lambda v: CFG,
          metrics=("avg_waiting", "max_waiting", "starved_tasks", "deadline_miss_rate",
                   "avg_response", "cpu_utilization", "sched_overhead_ms")):
    frames = []
    for v in values:
        cfg = cfg_for(v)
        frames.append(run_frame(lambda s: make(v, s), cfg=cfg, **{param: v}))
    df = pd.concat(frames, ignore_index=True)
    save(out, f"{name}_runs.csv", df)
    summ = an.summarize_runs(df, list(metrics), by=(param, "algorithm"))
    save(out, f"{name}_summary.csv", summ)
    return df, summ


def series(summ, param, metric):
    out = {}
    for algo in dict.fromkeys(summ["algorithm"]):
        sub = summ[(summ.algorithm == algo) & (summ.metric == metric)]
        out[algo] = list(sub.sort_values(param)["mean"])
    return out


def sweep_table(summ, param, metric, values, nd=1):
    s = series(summ, param, metric)
    t = f"| {param} | " + " | ".join(s) + " |\n|---|" + "---|" * len(s) + "\n"
    for i, v in enumerate(values):
        t += f"| {v} | " + " | ".join(f"{s[a][i]:.{nd}f}" for a in s) + " |\n"
    return t


# ---------------------------------------------------------- experiments
def e1_baseline(out, report):
    cfg = dataclasses.replace(CFG, log_events=False)
    df, runs = run_frame(lambda s: generate_synthetic(300, 4, 0.9, "medium", s), cfg=cfg, keep=True)
    save(out, "e1_runs.csv", df)
    groups = [c for c in df.columns if c.startswith(("wait_", "slowdown_"))]
    summ = an.summarize_runs(df, ALL_METRICS + groups)
    save(out, "e1_summary.csv", summ)
    comp = an.compare_to_baselines(df, CANDIDATE, [k for k, _ in COMPARE] + ["avg_turnaround", "p95_waiting"])
    save(out, "e1_adaptive_vs_baselines.csv", comp)

    # per-task records of every seed -> waiting-time distributions
    pt = pd.DataFrame([r for d in runs.values() for rec in d.values() for r in per_task(rec.result)])
    save(out, "e1_per_task.csv", pt)
    viz.wait_distribution({a: list(g["waiting"]) for a, g in pt.groupby("algorithm", sort=False)},
                          os.path.join(out, "e1_wait_distribution.png"),
                          "E1 - Waiting-time distribution (all tasks, 10 seeds)")
    viz.ci_bars(summ, MAIN, os.path.join(out, "e1_main.png"),
                "E1 - Task-level metrics (mean, 95 % CI; 300 tasks, 4 cores, load 0.9)")
    viz.ci_bars(summ, SYSTEM[:3] + FAIR[:3], os.path.join(out, "e1_system_fairness.png"),
                "E1 - Utilisation, throughput, fairness and starvation (mean, 95 % CI)")
    fps = {rec.fingerprint for d in runs.values() for rec in d.values()}
    report.append(
        "## E1 - Baseline comparison\n\n300 tasks, 4 cores, offered load 0.9, medium mix, 30 % of tasks "
        f"with soft deadlines, context switch {CFG.context_switch} ms, {len(CFG.seeds)} seeds. "
        "Cells: mean [95 % CI]. "
        f"Each seed's workload was fingerprinted and verified identical for all algorithms "
        f"({len(fps)} distinct workloads, one per seed).\n\n"
        "### Task-level metrics\n\n" + an.md_ci_table(summ, MAIN) +
        "\n\n### Percentiles\n\n" + an.md_ci_table(summ, PCT) +
        "\n\n### System metrics\n\n" + an.md_ci_table(summ, SYSTEM) +
        "\n\n### Fairness and starvation summary\n\n" + an.md_ci_table(summ, FAIR, nd=3) +
        "\n\n### Adaptive vs each baseline (paired over seeds)\n\nPositive % = Adaptive better. "
        "Δ = Adaptive − baseline with 95 % CI; p = Wilcoxon signed-rank.\n\n" +
        an.md_comparison(comp, COMPARE) +
        "\n\n![](e1_main.png)\n![](e1_system_fairness.png)\n![](e1_wait_distribution.png)\n")
    return summ


def e2_load(out, report):
    loads = [0.5, 0.6, 0.7, 0.8, 0.9, 1.0, 1.1, 1.2]
    _, s = sweep(out, "e2_load", "load", loads, lambda v, seed: generate_synthetic(300, 4, v, "medium", seed))
    viz.line_sweep(loads, series(s, "load", "avg_waiting"), os.path.join(out, "e2_load_avg_wait.png"),
                   "E2 - Average waiting vs offered load", "offered load ρ", "avg waiting (ms)", logy=True)
    viz.line_sweep(loads, series(s, "load", "max_waiting"), os.path.join(out, "e2_load_max_wait.png"),
                   "E2 - Worst-case waiting vs offered load", "offered load ρ", "max waiting (ms)", logy=True)
    viz.line_sweep(loads, series(s, "load", "starved_tasks"), os.path.join(out, "e2_load_starved.png"),
                   "E2 - Starved tasks vs offered load", "offered load ρ", "tasks with W > 10·mean(B)")
    viz.line_sweep(loads, series(s, "load", "deadline_miss_rate"), os.path.join(out, "e2_load_misses.png"),
                   "E2 - Deadline miss rate vs offered load", "offered load ρ", "share of deadlines missed")
    report.append("## E2 - Load sweep (congestion and starvation)\n\n### Average waiting (ms)\n\n" +
                  sweep_table(s, "load", "avg_waiting", loads) + "\n### Maximum waiting (ms)\n\n" +
                  sweep_table(s, "load", "max_waiting", loads, 0) + "\n### Deadline miss rate\n\n" +
                  sweep_table(s, "load", "deadline_miss_rate", loads, 3) +
                  "\n![](e2_load_avg_wait.png)\n![](e2_load_max_wait.png)\n![](e2_load_starved.png)\n"
                  "![](e2_load_misses.png)\n")


def e3_heterogeneity(out, report):
    mixes = ["low", "medium", "high"]
    _, s = sweep(out, "e3_heterogeneity", "mix", mixes,
                 lambda v, seed: generate_synthetic(300, 4, 0.9, v, seed))
    ser = {a: [s[(s.algorithm == a) & (s.metric == "avg_waiting") & (s.mix == m)]["mean"].iloc[0]
               for m in mixes] for a in dict.fromkeys(s["algorithm"])}
    viz.grouped_bars(mixes, ser, os.path.join(out, "e3_heterogeneity.png"),
                     "E3 - Average waiting by workload heterogeneity", "avg waiting (ms)")
    t = "| mix | " + " | ".join(ser) + " |\n|---|" + "---|" * len(ser) + "\n"
    for i, m in enumerate(mixes):
        t += f"| {m} | " + " | ".join(f"{ser[a][i]:.2f}" for a in ser) + " |\n"
    report.append("## E3 - Heterogeneity\n\nlow = GC + k-mer only; medium = all four categories; "
                  "high = 30 % alignment. Avg waiting (ms).\n\n" + t + "\n![](e3_heterogeneity.png)\n")


def e4_categories(out, summ, report):
    algos = list(dict.fromkeys(summ["algorithm"]))
    get = lambda a, k: summ[(summ.algorithm == a) & (summ.metric == k)]["mean"]  # noqa: E731
    blocks = []
    for groups, label, fname in [(["gc_content", "kmer_count", "motif_search", "alignment"], "category", "e4_category"),
                                 (["short", "medium", "long"], "size class", "e4_size_class")]:
        wait = {a: [float(get(a, f"wait_{g}").iloc[0]) if len(get(a, f"wait_{g}")) else 0 for g in groups] for a in algos}
        sd = {a: [float(get(a, f"slowdown_{g}").iloc[0]) if len(get(a, f"slowdown_{g}")) else 0 for g in groups] for a in algos}
        viz.grouped_bars(groups, wait, os.path.join(out, f"{fname}_wait.png"),
                         f"E4 - Average waiting per {label} (E1 setting)", "avg waiting (ms)")
        viz.grouped_bars(groups, sd, os.path.join(out, f"{fname}_slowdown.png"),
                         f"E4 - Average slowdown per {label} (E1 setting)", "slowdown T/B")
        t = f"| Algorithm | " + " | ".join(f"{g} W / SD" for g in groups) + " |\n|---|" + "---|" * len(groups) + "\n"
        for a in algos:
            t += f"| {a} | " + " | ".join(f"{wait[a][i]:.1f} / {sd[a][i]:.2f}" for i in range(len(groups))) + " |\n"
        blocks.append(f"### Per {label}\n\n" + t + f"\n![]({fname}_wait.png)\n![]({fname}_slowdown.png)\n")
    report.append("## E4 - Fairness to long alignment tasks\n\nAverage waiting (ms) / average slowdown. "
                  "Size classes on the estimated burst: short < 5 ms ≤ medium < 50 ms ≤ long.\n\n" +
                  "\n".join(blocks))


def e5_estimation(out, report):
    errs = [0.0, 0.25, 0.5, 1.0]
    _, s = sweep(out, "e5_estimation_error", "estimate_error", errs,
                 lambda v, seed: generate_synthetic(300, 4, 0.9, "medium", seed, estimate_error=v))
    viz.line_sweep(errs, series(s, "estimate_error", "avg_waiting"), os.path.join(out, "e5_estimation_error.png"),
                   "E5 - Avg waiting vs burst-estimation error", "log-normal σ of estimate error", "avg waiting (ms)")
    report.append("## E5 - Robustness to burst-estimation error\n\nSchedulers only ever see the estimate. "
                  "Avg waiting (ms).\n\n" + sweep_table(s, "estimate_error", "avg_waiting", errs, 2) +
                  "\n![](e5_estimation_error.png)\n")


def e6_scalability(out, report):
    ns, ms = [100, 250, 500, 1000, 2000], [1, 2, 4, 8, 16]
    few = dataclasses.replace(CFG, seeds=CFG.seeds[:4])
    _, sn = sweep(out, "e6_tasks", "tasks", ns, lambda v, seed: generate_synthetic(v, 4, 0.9, "medium", seed),
                  cfg_for=lambda v: few)
    _, sm = sweep(out, "e6_cores", "cores", ms, lambda v, seed: generate_synthetic(500, v, 0.9, "medium", seed),
                  cfg_for=lambda v: dataclasses.replace(few, num_cores=v))
    viz.line_sweep(ns, series(sn, "tasks", "sched_overhead_ms"), os.path.join(out, "e6_overhead_vs_tasks.png"),
                   "E6 - Scheduler overhead vs number of tasks (4 cores)", "tasks",
                   "wall-clock time inside policy (ms)", logy=True, xlog=True)
    viz.line_sweep(ns, series(sn, "tasks", "avg_waiting"), os.path.join(out, "e6_wait_vs_tasks.png"),
                   "E6 - Avg waiting vs number of tasks (4 cores, load 0.9)", "tasks", "avg waiting (ms)", xlog=True)
    viz.line_sweep(ms, series(sm, "cores", "avg_waiting"), os.path.join(out, "e6_wait_vs_cores.png"),
                   "E6 - Avg waiting vs number of cores (500 tasks, load 0.9)", "cores",
                   "avg waiting (ms)", logy=True, xlog=True)
    viz.line_sweep(ms, series(sm, "cores", "cpu_utilization"), os.path.join(out, "e6_util_vs_cores.png"),
                   "E6 - CPU utilisation vs number of cores", "cores", "utilisation (M·T normalised)", xlog=True)
    report.append("## E6 - Scalability and scheduling overhead\n\nWall-clock ms spent inside the policy "
                  "for a whole run (4 seeds).\n\n" + sweep_table(sn, "tasks", "sched_overhead_ms", ns) +
                  "\nAverage waiting (ms) vs cores (500 tasks):\n\n" + sweep_table(sm, "cores", "avg_waiting", ms, 2) +
                  "\n![](e6_overhead_vs_tasks.png)\n![](e6_wait_vs_tasks.png)\n![](e6_wait_vs_cores.png)\n"
                  "![](e6_util_vs_cores.png)\n")


def e7_sensitivity(out, report):
    configs = {"default": {}}
    configs.update({f"c={c}": dict(aging_c=c) for c in (2, 4, 16, 32)})
    configs.update({"w=.6/.2/.1/.1": dict(w_short=.6, w_wait=.2, w_prio=.1, w_deadline=.1),
                    "w=.35/.4/.15/.1": dict(w_short=.35, w_wait=.4, w_prio=.15, w_deadline=.1),
                    "no deadline term": dict(w_deadline=0.0),
                    "no priority term": dict(w_prio=0.0),
                    "no starvation guard": dict(starvation_threshold=None),
                    "guard θ=10": dict(starvation_threshold=10.0),
                    "q=5": dict(quantum=5.0), "q=20": dict(quantum=20.0),
                    "no quantum": dict(quantum=None)})
    suite = lambda: [AdaptiveScheduler(name=k, **v) for k, v in configs.items()]  # noqa: E731
    df = run_frame(lambda s: generate_synthetic(300, 4, 0.9, "medium", s), suite)
    save(out, "e7_runs.csv", df)
    mets = [("avg_waiting", "Avg W"), ("max_waiting", "Max W"), ("avg_slowdown", "Avg SD"),
            ("wait_high_priority", "W prio 1-2"), ("deadline_miss_rate", "Miss rate"),
            ("starvation_overrides", "Guard overrides")]
    s = an.summarize_runs(df, [k for k, _ in mets])
    save(out, "e7_summary.csv", s)
    report.append("## E7 - Adaptive scheduler sensitivity\n\nDefault: weights 0.45 / 0.30 / 0.15 / 0.10 "
                  "(burst / aging / priority / deadline), aging c = 8, starvation guard θ = 20, q = 10 ms. "
                  "Each row changes one setting. Mean [95 % CI].\n\n" + an.md_ci_table(s, mets) + "\n")


def e9_sequence_length(out, report):
    scales = [0.25, 0.5, 1.0, 2.0, 4.0]
    _, s = sweep(out, "e9_seq_length", "length_scale", scales,
                 lambda v, seed: generate_synthetic(300, 4, 0.9, "medium", seed, size_scale=v))
    viz.line_sweep(scales, series(s, "length_scale", "avg_waiting"), os.path.join(out, "e9_seq_length.png"),
                   "E9 - Avg waiting vs sequence length (load held at 0.9)", "sequence-length scale factor",
                   "avg waiting (ms)", logy=True, xlog=True)
    report.append("## E9 - Sequence length\n\nAll sequence lengths multiplied by the factor (alignment by its "
                  "square root per side, so work scales by the factor); arrival rate adjusted to keep load 0.9. "
                  "Avg waiting (ms).\n\n" + sweep_table(s, "length_scale", "avg_waiting", scales, 2) +
                  "\n![](e9_seq_length.png)\n")


def _local_fasta(out):
    path = os.path.join(out, "sample_genome.fa")
    rng = random.Random(7)
    write_fasta(path, [(f"synthetic_chr{i} locally generated", random_dna(400_000, rng, 0.42 + 0.04 * i))
                       for i in range(3)])
    return path


def e8_dataset(out, report, n=80, fasta=None):
    fasta = fasta or _local_fasta(out)
    print("  calibrating cost model on this machine ...")
    cm = calibrate(fasta, seed=1, points=8, size_scale=0.3)
    cm.save(os.path.join(out, "e8_cost_model.json"))
    print(f"  profiling {n} real tasks ...")
    prof: dict = {}
    wl = generate_from_dataset(n, fasta, num_cores=4, load=0.9, seed=2, size_scale=0.3,
                               cost_model=cm, profile=prof)
    repo = WorkloadRepository(wl, "e8_dataset", prof)
    repo.save(os.path.join(out, "e8_dataset_workload.csv"))
    recs = WorkloadManager(repo, CFG).run(default_suite())
    df = an.runs_to_frame({0: {a: summarize(r.result) for a, r in recs.items()}})
    save(out, "e8_runs.csv", df)
    s = an.summarize_runs(df, [k for k, _ in MAIN])
    prof["fingerprint"] = repo.fingerprint
    with open(os.path.join(out, "e8_profile.json"), "w") as fh:
        json.dump(prof, fh, indent=2, default=str)
    err = sum(abs(t.est_burst - t.burst) / t.burst for t in wl) / len(wl)
    inp = prof["input"]
    report.append("## E8 - Dataset-based workload (measured runtimes)\n\n"
                  f"Input `{os.path.basename(fasta)}` parsed with {inp.get('parser')}: "
                  f"{inp.get('records_valid')}/{inp.get('records_read')} records valid, "
                  f"{inp.get('total_bases'):,} bases, GC {inp.get('gc_content', 0):.1%}. "
                  f"{n} tasks executed on `{prof['machine']['platform']}` (Python {prof['machine']['python']}); "
                  "burst = median of 3 timed runs; estimate = calibrated cost model (R² "
                  + ", ".join(f"{k} {v:.2f}" for k, v in cm.r2.items()) +
                  f"; mean relative estimate error {err:.0%}). Hardware monitor during profiling: "
                  f"{prof['monitor']}. Single workload (no CI); timings vary slightly between runs.\n\n"
                  + an.md_ci_table(s, MAIN) + "\n")
    return fasta


def e10_hardware(out, report, fasta, n=60, reps=5, workers=None):
    from .hardware import execute_on_hardware
    # leave one core for the dispatcher process, otherwise it competes with the workers
    workers = workers or max(1, min(4, (os.cpu_count() or 2) - 1))
    genome = load_genome(fasta, random.Random(0))
    wl = generate_from_dataset(n, genome=genome, num_cores=workers, load=0.9, seed=11, size_scale=1.0)
    save_workload(wl, os.path.join(out, "e10_hardware_workload.csv"))
    names = ["fcfs", "sjf", "priority", "adaptive-np"]
    rows = []
    for rep in range(reps):
        for nm in names[rep % 4:] + names[:rep % 4]:          # rotate order (warm-cache bias)
            r = execute_on_hardware(wl, genome, by_name(nm), workers)
            h = summarize(r)
            replay = [dataclasses.replace(t, burst=r.workload[t.task_id].burst) for t in wl]
            sim_replay = summarize(simulate(replay, by_name(nm), workers, 0.0, log_events=False))
            sim_prof = summarize(simulate(wl, by_name(nm), workers, 0.0, log_events=False))
            for kind, m in (("hardware", h), ("simulation (replayed bursts)", sim_replay),
                            ("simulation (profiled bursts)", sim_prof)):
                rows.append({"rep": rep, "algorithm": m["algorithm"], "evidence": kind,
                             **{k: m[k] for k in ("avg_waiting", "avg_response", "avg_turnaround",
                                                  "max_waiting", "cpu_utilization")}})
    df = pd.DataFrame(rows)
    save(out, "e10_hardware_runs.csv", df)
    s = an.summarize_runs(df.rename(columns={"rep": "seed"}), ["avg_waiting", "avg_response", "max_waiting"],
                          by=("evidence", "algorithm"))
    save(out, "e10_hardware_summary.csv", s)
    t = "| Algorithm | " + " | ".join(dict.fromkeys(s["evidence"])) + " |\n|---|---|---|---|\n"
    for algo in dict.fromkeys(s["algorithm"]):
        cells = []
        for ev in dict.fromkeys(s["evidence"]):
            r = s[(s.algorithm == algo) & (s.evidence == ev) & (s.metric == "avg_waiting")].iloc[0]
            cells.append(f"{r['mean']:.2f} [{r['ci_low']:.2f}, {r['ci_high']:.2f}]")
        t += f"| {algo} | " + " | ".join(cells) + " |\n"
    report.append(
        "## E10 - Hardware validation (real execution)\n\n"
        f"{n} dataset tasks really executed on {workers} worker process(es) (one core left for the dispatcher) by a user-space dispatcher that "
        f"uses the same policy code; {reps} repetitions with rotated algorithm order. Non-preemptive policies "
        "only (Adaptive-NP = adaptive scoring without quantum). Average waiting (ms), mean [95 % CI]:\n\n" + t +
        "\n*Hardware* waiting includes inter-process communication latency; *replayed* simulation uses the "
        "bursts measured in that hardware run, so the gap between the two is the dispatcher overhead the "
        "simulator does not model. These are user-space measurements, not an OS-kernel scheduler.\n")


def gantt_demo(out, report):
    wl = generate_synthetic(16, 2, 1.5, "medium", seed=CFG.seeds[0] + 77)
    repo = WorkloadRepository(wl, "gantt")
    cfg = dataclasses.replace(CFG, num_cores=2, log_events=True, log_decisions=True)
    recs = WorkloadManager(repo, cfg).run(default_suite())
    results = [r.result for r in recs.values()]
    end = max(r.makespan_end for r in results)
    viz.gantt(results, os.path.join(out, "gantt_all_16tasks_2cores.png"), max_time=end)
    os.makedirs(os.path.join(out, "gantt"), exist_ok=True)
    links = []
    for r in results:
        fn = "gantt_" + "".join(ch if ch.isalnum() else "_" for ch in r.algorithm).strip("_").lower() + ".png"
        viz.gantt_single(r, os.path.join(out, "gantt", fn), max_time=end)
        links.append(f"[{r.algorithm}](gantt/{fn})")
        pd.DataFrame([e.__dict__ for e in r.events]).to_csv(
            os.path.join(out, "gantt", fn.replace(".png", "_events.csv").replace("gantt_", "")), index=False)
    repo.save(os.path.join(out, "gantt_workload.csv"))
    ada = recs[CANDIDATE].result
    pd.DataFrame(ada.decisions_log).to_csv(os.path.join(out, "decision_log_adaptive.csv"), index=False)
    pd.DataFrame(per_task(ada)).to_csv(os.path.join(out, "task_log_adaptive.csv"), index=False)
    report.append("## Gantt charts, event and decision logs (16 tasks, 2 cores, load 1.5)\n\n"
                  "![](gantt_all_16tasks_2cores.png)\n\nPer-algorithm charts with idle intervals: "
                  + ", ".join(links) + ". Event logs (ARRIVE, DISPATCH, PREEMPT, QUANTUM_EXPIRE, COMPLETE, "
                  "IDLE_START, IDLE_END) are in `gantt/*_events.csv`; `decision_log_adaptive.csv` gives the "
                  "score breakdown of every Adaptive dispatch decision; `task_log_adaptive.csv` the task-level log.\n")


def run_all(out="results", seeds=10, quick=False, fasta=None, hardware=True):
    global CFG
    os.makedirs(out, exist_ok=True)
    if quick:
        seeds = 3
    CFG = dataclasses.replace(CFG, seeds=tuple(range(1000, 1000 + seeds)))
    report = ["# Experimental results\n",
              f"Generated {time.strftime('%Y-%m-%d %H:%M')} on `{machine_info()['platform']}`. "
              f"{seeds} seeds per setting unless stated. Time unit: ms. Unless marked *hardware*, "
              "all results are **simulation** evidence.\n"]
    t0 = time.time()
    print("E1 baseline ...")
    summ = e1_baseline(out, report)
    for name, fn in [("E2 load", lambda: e2_load(out, report)),
                     ("E3 heterogeneity", lambda: e3_heterogeneity(out, report)),
                     ("E4 categories", lambda: e4_categories(out, summ, report)),
                     ("E5 estimation error", lambda: e5_estimation(out, report)),
                     ("E6 scalability", lambda: e6_scalability(out, report)),
                     ("E7 sensitivity", lambda: e7_sensitivity(out, report)),
                     ("E9 sequence length", lambda: e9_sequence_length(out, report))]:
        print(f"{name} ...")
        fn()
    print("E8 dataset ...")
    fasta = e8_dataset(out, report, 40 if quick else 80, fasta)
    if hardware:
        print("E10 hardware validation ...")
        e10_hardware(out, report, fasta, n=30 if quick else 60, reps=2 if quick else 5)
    print("Gantt / logs ...")
    gantt_demo(out, report)
    with open(os.path.join(out, "RESULTS.md"), "w") as fh:
        fh.write("\n".join(report))
    print(f"done in {time.time() - t0:.0f}s -> {out}/RESULTS.md")
