# BioSchedSim: an adaptive CPU scheduler for bioinformatics workloads

This is the implementation for *Review 2: Methodology and Partial Implementation*.
Diya Sojan James (24BCB0109) and Gayathri S. Nair (24BCB0125).

The project is a discrete-event simulator, plus a real-execution validator, that compares
FCFS, SJF, SRTF, Round Robin and Priority scheduling with the proposed **Adaptive
Bioinformatics CPU Scheduler**. The workloads are built from four DNA-processing operations.

## Quick start

```bash
pip install -r requirements.txt        # numpy, pandas, matplotlib, scipy, pytest (+ optional biopython, psutil)
pytest -q                              # 33 tests
pyt hon -m bioschedsim experiments     # E1-E10 -> results/RESULTS.md, CSVs, plots, logs (about 1 min)
```

Other commands:

```bash
python -m bioschedsim validate genome.fa                       # read and validate a FASTA file (Module 1)
python -m bioschedsim generate --tasks 200 --load 0.9 --mix high --out wl.json   # CSV or JSON
python -m bioschedsim simulate --workload wl.json --cores 4 --algo all \
       --gantt g.png --events ev.csv --decisions dec.csv --per-task tasks.csv
python -m bioschedsim calibrate --fasta genome.fa --out cost_model.json        # fit the burst estimator
python -m bioschedsim profile --fasta genome.fa --cost-model cost_model.json --out measured.csv
python -m bioschedsim hardware --tasks 60 --workers 3 --fasta genome.fa        # real execution
python -m bioschedsim experiments --fasta genome.fa            # E8 and E10 use your FASTA
```

Any FASTA file works, for example an *E. coli* K-12 assembly from NCBI. Without `--fasta`, the
experiments use a locally generated three-chromosome genome.

## How the code maps to Section 3 of the report

| Module (Section 3)                       | Requirement                                                                                                                                                                                                                                   | Where                                                                                                                              |
| ---------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------- |
| **1 Input and workload generator**       | Read and validate FASTA (Bio.SeqIO when installed, otherwise the built-in parser); reject invalid records and report them                                                                                                                     | `bio_ops.load_sequences`, `validate_sequence`                                                                                      |
|                                          | Choose the operation, vary sequence lengths, mix short / medium / long tasks                                                                                                                                                                  | `workload.generate_synthetic`, `MIXES`, `size_class`                                                                               |
|                                          | Arrival times (Poisson, target load ρ), priorities 1-5, optional soft deadlines                                                                                                                                                               | `workload._finalise`                                                                                                               |
|                                          | Output as CSV, JSON or in-memory list (ID, type, input size, arrival, estimated burst, actual burst, priority, deadline)                                                                                                                      | `save_workload` / `load_workload`                                                                                                  |
| **2 Profiler and burst estimator**       | Complexity-based cost model fitted by least squares on measured runtimes                                                                                                                                                                      | `workload.CostModel`, `calibrate`                                                                                                  |
|                                          | No leakage of the actual burst: schedulers receive `TaskInfo`, which has no `burst` field, and the simulator keeps the actual remaining work in a private table                                                                               | `TaskInfo`, `Simulator._rem`, test `test_scheduler_cannot_see_actual_burst`                                                        |
|                                          | Estimation error studied separately from scheduling quality                                                                                                                                                                                   | `estimate_error` parameter, experiment E5                                                                                          |
|                                          | Dataset tasks are really executed; hardware, OS, timer and CPU/memory monitoring are recorded                                                                                                                                                 | `generate_from_dataset`, `machine_info`, `HardwareMonitor`                                                                         |
| **3 Repository and workload manager**    | Canonical immutable workload (frozen records), validation, SHA-256 fingerprint                                                                                                                                                                | `repository.WorkloadRepository`, `validate_workload`                                                                               |
|                                          | Fresh simulator and run-time state per algorithm; proof that all algorithms got identical input                                                                                                                                               | `WorkloadManager.run`                                                                                                              |
|                                          | Cores, quantum, context switch, seeds and logging set in one place                                                                                                                                                                            | `ExperimentConfig`                                                                                                                 |
|                                          | Immutable input (`Task`) kept separate from mutable run-time state (`SimTask`)                                                                                                                                                                | `workload.Task` vs `simulator.SimTask`                                                                                             |
| **4 Conventional algorithms**            | FCFS, SJF, SRTF (separate label), RR(q), Priority, Priority-P behind one interface                                                                                                                                                            | `schedulers.py`                                                                                                                    |
| **5 Adaptive scheduler**                 | Feature normalisation, burst score, aging, priority score, optional deadline urgency, weighted score, tie-breaking, starvation detection and prevention, dispatch decision with explanation                                                   | `AdaptiveScheduler.features / burst_score / aging_score / priority_score / urgency_score / combine / is_starving / keys / explain` |
|                                          | Running and completed tasks are never selected; no task is ever on two cores                                                                                                                                                                  | assertions in `Simulator._start`, `check_invariants`                                                                               |
| **6 Monitoring, metrics, visualisation** | Logs arrival, dispatch, preemption, quantum expiry, **idle intervals** and completion                                                                                                                                                         | `simulator.Event`, `IdleInterval`                                                                                                  |
|                                          | Task-level log, Gantt chart per algorithm, average and percentile tables (p50/90/95/99), utilisation and throughput, waiting-time distributions, fairness and starvation summaries, graphs against task count, sequence length and core count | `metrics.py`, `viz.py`, `experiments.py`                                                                                           |
| **7 Analysis and reporting**             | Aggregated runs with 95 % confidence intervals; absolute and relative change (absolute only when the baseline is near zero); paired Wilcoxon tests                                                                                            | `analysis.py`                                                                                                                      |
|                                          | Simulation results kept apart from hardware results (`evidence` column); real execution on worker processes                                                                                                                                   | `hardware.py`, experiment E10                                                                                                      |

The workflow diagrams for Section 4 are in `docs/diagrams/` as `.png` files, with their Mermaid source in `.mmd` files.

## The adaptive scheduler

```
S_i = 0.45·s_i + 0.30·a_i + 0.15·p_i + 0.10·u_i

s_i = 1 − ln(r_i/r_min) / ln(r_max/r_min)     burst score (estimated remaining work, log scale over the ready set)
a_i = W_i / (c·b̄),  c = 8                     aging (b̄ = running mean of estimated bursts of arrived tasks)
p_i = (5 − priority_i) / 4                     application priority (1 = highest)
u_i = 0 if no deadline; 1 if slack ≤ 0; otherwise b̄ / (b̄ + slack), where slack = deadline − now − r_i
```

- **Starvation guard.** A task whose wait exceeds θ·b̄ (θ = 20) is served before every task that is not starving.
- **Ties.** Broken by earlier arrival, then by lower ID.
- **Re-ranking.** Tasks are re-ranked every quantum (q = 10 ms). `Adaptive-NP` is the same policy without a quantum.

## Validity notes

* Evaluation seeds (1000-1009) are separate from the seeds used during design (100-109).
* E1-E7 and E9 are deterministic. E8 and E10 use measured wall-clock times, so they vary slightly from run to run.
* E10 runs a user-space dispatcher on worker processes. It is evidence about real execution of the same policy code, not about an OS-kernel scheduler.
