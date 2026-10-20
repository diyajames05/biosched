"""Command-line interface.

    python -m bioschedsim experiments [--quick] [--seeds 10] [--out results] [--fasta genome.fa] [--no-hardware]
    python -m bioschedsim validate genome.fa
    python -m bioschedsim hardware --tasks 60 --workers 4 [--fasta genome.fa]
    python -m bioschedsim simulate --algo adaptive --tasks 50 --cores 2 [--workload w.csv] [--gantt g.png]
    python -m bioschedsim generate --tasks 200 --cores 4 --load 0.9 --mix medium --out workload.csv
    python -m bioschedsim profile  --tasks 60 [--fasta genome.fa] --out measured.csv
    python -m bioschedsim calibrate [--fasta genome.fa] --out cost_model.json
"""
import argparse
import json

from . import experiments, viz
from .metrics import per_task, summarize
from .schedulers import by_name
from .simulator import simulate
from .workload import (CostModel, calibrate, generate_from_dataset, generate_synthetic,
                       load_workload, save_workload)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="bioschedsim", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    e = sub.add_parser("experiments", help="run the full comparative study (E1-E8)")
    e.add_argument("--out", default="results")
    e.add_argument("--seeds", type=int, default=10)
    e.add_argument("--quick", action="store_true")
    e.add_argument("--fasta")
    e.add_argument("--no-hardware", action="store_true", help="skip E10 real-execution validation")

    v = sub.add_parser("validate", help="read + validate a FASTA file")
    v.add_argument("fasta")

    h = sub.add_parser("hardware", help="execute dataset tasks on real worker processes")
    h.add_argument("--tasks", type=int, default=60)
    h.add_argument("--workers", type=int, default=2)
    h.add_argument("--fasta")
    h.add_argument("--load", type=float, default=0.7)
    h.add_argument("--algo", default="all", help="fcfs | sjf | priority | adaptive-np | all")

    s = sub.add_parser("simulate", help="run one algorithm and print metrics")
    s.add_argument("--algo", default="adaptive",
                   help="fcfs | sjf | srtf | rr | priority | priority-p | adaptive | adaptive-np | all")
    s.add_argument("--workload", help="CSV from `generate`/`profile` (else synthetic)")
    s.add_argument("--tasks", type=int, default=50)
    s.add_argument("--cores", type=int, default=2)
    s.add_argument("--load", type=float, default=0.9)
    s.add_argument("--mix", default="medium")
    s.add_argument("--seed", type=int, default=0)
    s.add_argument("--quantum", type=float, default=10.0)
    s.add_argument("--context-switch", type=float, default=0.05)
    s.add_argument("--gantt", help="write a Gantt chart PNG")
    s.add_argument("--events", help="write the event log CSV")
    s.add_argument("--per-task", help="write per-task metrics CSV")
    s.add_argument("--decisions", help="write the Adaptive decision log CSV")

    g = sub.add_parser("generate", help="synthetic workload -> CSV")
    g.add_argument("--tasks", type=int, default=200)
    g.add_argument("--cores", type=int, default=4)
    g.add_argument("--load", type=float, default=0.9)
    g.add_argument("--mix", default="medium")
    g.add_argument("--seed", type=int, default=0)
    g.add_argument("--estimate-error", type=float, default=0.25)
    g.add_argument("--deadline-fraction", type=float, default=0.3)
    g.add_argument("--scale", type=float, default=1.0, help="sequence-length scale factor")
    g.add_argument("--out", default="workload.csv", help=".csv or .json")

    p = sub.add_parser("profile", help="dataset-based workload: execute & time real tasks -> CSV")
    p.add_argument("--tasks", type=int, default=60)
    p.add_argument("--fasta")
    p.add_argument("--cores", type=int, default=4)
    p.add_argument("--load", type=float, default=0.9)
    p.add_argument("--scale", type=float, default=0.3)
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--cost-model", help="JSON from `calibrate`")
    p.add_argument("--out", default="measured_workload.csv")

    c = sub.add_parser("calibrate", help="fit the burst cost model on this machine")
    c.add_argument("--fasta")
    c.add_argument("--out", default="cost_model.json")

    a = ap.parse_args(argv)
    if a.cmd == "experiments":
        experiments.run_all(a.out, a.seeds, a.quick, a.fasta, hardware=not a.no_hardware)
    elif a.cmd == "validate":
        from .bio_ops import load_sequences
        _, rep = load_sequences(a.fasta)
        print(json.dumps(rep, indent=2))
    elif a.cmd == "hardware":
        import random
        from .hardware import execute_on_hardware
        from .workload import load_genome
        genome = load_genome(a.fasta, random.Random(0))
        wl = generate_from_dataset(a.tasks, genome=genome, num_cores=a.workers, load=a.load,
                                   seed=11, size_scale=1.0)
        algos = ["fcfs", "sjf", "priority", "adaptive-np"] if a.algo == "all" else [a.algo]
        for name in algos:
            m = summarize(execute_on_hardware(wl, genome, by_name(name), a.workers))
            print(f"{m['algorithm']:12s} [hardware] avg W={m['avg_waiting']:.2f} ms  avg R={m['avg_response']:.2f} ms  "
                  f"max W={m['max_waiting']:.2f} ms  util={m['cpu_utilization']:.2f}")
    elif a.cmd == "generate":
        wl = generate_synthetic(a.tasks, a.cores, a.load, a.mix, a.seed, a.estimate_error,
                                size_scale=a.scale, deadline_fraction=a.deadline_fraction)
        save_workload(wl, a.out)
        print(f"wrote {len(wl)} tasks -> {a.out}")
    elif a.cmd == "profile":
        cm = CostModel.load(a.cost_model) if a.cost_model else None
        wl = generate_from_dataset(a.tasks, a.fasta, a.cores, a.load, seed=a.seed,
                                   size_scale=a.scale, cost_model=cm)
        save_workload(wl, a.out)
        print(f"wrote {len(wl)} measured tasks -> {a.out}")
    elif a.cmd == "calibrate":
        cm = calibrate(a.fasta)
        cm.save(a.out)
        print(f"cost model -> {a.out}")
    elif a.cmd == "simulate":
        wl = load_workload(a.workload) if a.workload else generate_synthetic(
            a.tasks, a.cores, a.load, a.mix, a.seed)
        algos = ["fcfs", "sjf", "srtf", "rr", "priority", "adaptive"] if a.algo == "all" else [a.algo]
        results = []
        for name in algos:
            r = simulate(wl, by_name(name, a.quantum), a.cores, a.context_switch,
                         log_decisions=bool(a.decisions))
            results.append(r)
            m = summarize(r)
            print(f"\n== {m['algorithm']} ==")
            for k, v in m.items():
                if k not in ("algorithm", "evidence"):
                    print(f"  {k:22s} {v:.3f}" if isinstance(v, float) else f"  {k:22s} {v}")
        if a.gantt:
            viz.gantt(results, a.gantt)
        if a.events:
            experiments.write_csv(a.events, [e.__dict__ for e in results[-1].events])
        if a.decisions:
            experiments.write_csv(a.decisions, results[-1].decisions_log)
        if a.per_task:
            experiments.write_csv(a.per_task, per_task(results[-1]))


if __name__ == "__main__":
    main()
