# Draft text for Review 2, sections 4–9 (updated implementation)

You have already written Section 3, so this draft covers sections 4 to 9. It matches the
current code, and all numbers come from `results/RESULTS.md`, produced by
`python -m bioschedsim experiments`. Edit the wording as you like. If you rerun the
experiments with a real genome, update E8 and E10.

---

## 4. Workflow diagram for each major module

Insert these images from `docs/diagrams/`. The editable Mermaid source for each is in the matching `.mmd` file.

| Section | Image | Shows |
|---|---|---|
| 4.1 Module 1: Bioinformatics input and workload generator | `m1_input_workload_generator.png` | Input source (FASTA, synthetic or metadata only), then validation with rejected records reported, operation choice, lengths, size class, Poisson arrivals, priority and deadline, and finally the task list (CSV / JSON) |
| 4.2 Module 2: Task profiler and burst-time estimator | `m2_profiler_estimator.png` | Calibration runs and the least-squares cost model. The estimate goes to the scheduler (TaskInfo); the actual burst goes only to the simulator and metrics |
| 4.3 Module 3: Task repository and workload manager | `m3_repository_manager.png` | Validate, freeze, fingerprint, then a fresh simulator for each algorithm, a fingerprint check and identical-input assertion |
| 4.4 Module 4: Conventional scheduling algorithms | `m4_baseline_algorithms.png` | The key each policy uses (FCFS, SJF, SRTF, RR, Priority), dispatch, and completion, quantum expiry or preemption |
| 4.5 Module 5: Adaptive Bioinformatics CPU Scheduler | `m5_adaptive_scheduler.png` | Normalisation, the four scores, weighted score, starvation check, tie-break, dispatch and decision log |
| 4.6 Module 6: Monitoring, metric calculation and visualisation | `m6_monitoring_metrics.png` | Event, segment and idle logging, then per-task and system metrics, CIs, tables, graphs and Gantt charts |
| (optional) Module 7: Analysis | `m7_analysis_reporting.png` | Paired comparison, relative or absolute reporting, and the simulation vs hardware label |

## 5. Dataset details and data collection procedure

**5.1 Synthetic workloads (simulation experiments E1–E7, E9).**
- **Tasks.** Each workload has 300 tasks unless stated otherwise. The operation is drawn from a
  category mix:
  - *low*: GC 50 %, k-mer 50 %
  - *medium*: GC 30 %, k-mer 30 %, motif 25 %, alignment 15 %
  - *high*: GC 45 %, k-mer 15 %, motif 10 %, alignment 30 %
- **Input sizes.** These are drawn log-uniformly:
  - GC content: 50 k–2 M bases
  - k-mer counting: 10 k–200 k bases, k = 4–12
  - motif search: 10 k–150 k bases, motif length 6–16, ≤ 1 mismatch
  - alignment: 150–1200 bp per sequence

  The resulting estimated bursts range from about 0.2 ms to 300 ms. Tasks are labelled
  *short* (< 5 ms), *medium* (5–50 ms) or *long* (≥ 50 ms).
- **Burst times.** The estimated burst comes from the cost model. The actual burst is the
  estimate multiplied by log-normal noise (σ = 0.25 by default; 0 to 1.0 in E5).
- **Arrivals.** Arrivals follow a Poisson process whose rate gives an offered load
  ρ = λ·E[est. burst]/M, from 0.5 to 1.2.
- **Priorities and deadlines.** Priorities are uniform over 1–5. 30 % of tasks get a soft
  deadline: A + est·U(2, 6) + U(0, mean est).
- **Seeds.** Evaluation uses seeds 1000–1009; design and tuning used 100–109.

**5.2 Dataset-based workload (E8, E10).**
1. *Input.* Read a FASTA file with Bio.SeqIO, validate every record (allowed characters
   A/C/G/T/N, minimum length), remove N bases, and log the rejected records. The default input
   is a locally generated genome of 3 records and 1.2 Mbp with GC 46 %. Any public FASTA, such
   as an NCBI *E. coli* K-12 assembly, can be passed with `--fasta`.
2. *Calibration.* Execute 8 tasks per category at different sizes, time each with
   `time.perf_counter` (median of 3 runs), and fit the cost model by least squares. On the test
   machine R² was 0.76–0.99 per category.
3. *Profiling.* Cut 80 random genome windows, execute every task, and record the measured
   median runtime as the actual burst and the cost-model prediction as the estimate. The genome
   coordinates are stored with each task so it can be re-executed identically. The mean
   relative estimation error was 8–17 % across runs.
4. *Recording.* Save the OS, CPU, core count, memory and CPU frequency (psutil), Python version,
   timer, CPU and memory utilisation during profiling, and the workload fingerprint to
   `e8_profile.json`.

## 6. Tools, technologies, algorithms and frameworks

### 6.2 Software environment and development tools

| Component           | Recommended choice                                                                        | Purpose                                                                                                                    |
| ------------------- | ----------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------- |
| Operating system    | Linux (Ubuntu 22.04+) or Windows 10/11 / macOS; development and test runs on Linux x86-64 | Running simulations and timing measurements; recorded in `e8_profile.json`                                                 |
| Code editor         | Visual Studio Code (Python and Jupyter extensions)                                        | Writing, debugging and running the modules                                                                                 |
| Runtime             | CPython 3.10+ (tested on 3.13)                                                            | Executes the simulator, policies, profiler and experiments                                                                 |
| Input format        | FASTA (`.fa` / `.fasta`) for sequences; CSV / JSON for workloads                          | Biological input and the canonical task lists                                                                              |
| Result storage      | CSV (pandas), JSON (cost model, profile), Markdown report                                 | Task-level logs, per-seed runs, summaries, comparison tables                                                               |
| Graph generation    | Matplotlib; Mermaid for workflow diagrams                                                 | Gantt charts, bar / CI charts, distributions, sweep graphs                                                                 |
| Version control     | Git + GitHub                                                                              | History and collaboration between the two authors                                                                          |
| Testing             | pytest (33 tests)                                                                         | State transitions, queue ordering, textbook examples, scoring components, no information leakage, idle logging, statistics |
| Hardware monitoring | psutil (CPU %, memory %, frequency, core count) + `platform`                              | Recording the machine and its load during real-execution measurements                                                      |

### 6.3 Algorithms and computational techniques (as implemented)

| Algorithm or technique | Application | Implementation |
|---|---|---|
| FCFS | Arrival-order scheduling baseline | `schedulers.FCFS` |
| SJF / SRTF | Shortest estimated burst / remaining-time baselines (separate labels) | `SJF`, `SRTF` |
| Round Robin | Time-quantum baseline (q = 10 ms) | `RoundRobin` |
| Priority Scheduling | Application-priority baseline (non-preemptive and preemptive) | `PriorityScheduler` |
| Weighted adaptive scoring | Proposed workload-aware policy | `AdaptiveScheduler` |
| Priority aging + starvation guard | Starvation mitigation | aging term `a_i`, guard `W > θ·b̄` |
| Deadline urgency | Optional soft-deadline awareness | urgency term `u_i` |
| Discrete-event simulation | Arrivals, execution, preemption, idle periods, completion | `simulator.Simulator` |
| Complexity-based estimation | Burst-time estimation by least squares | `workload.CostModel` |
| Descriptive statistics | Means, percentiles, Jain fairness | `metrics.summarize` |
| Confidence intervals | 95 % Student-t CIs; paired differences; Wilcoxon signed-rank | `analysis.py` |

## 7. Experimental plan and evaluation metrics

**7.1 Experiments.**

| Exp. | Question                                                          | Varied                                        | Fixed                               |
| ---- | ----------------------------------------------------------------- | --------------------------------------------- | ----------------------------------- |
| E1   | Does Adaptive reduce waiting and response time?                   | algorithm                                     | N = 300, M = 4, ρ = 0.9, medium mix |
| E2   | Do more arrivals cause starvation or congestion?                  | ρ = 0.5 … 1.2                                 | E1 otherwise                        |
| E3   | Do the gains persist with heterogeneity?                          | mix: low, medium, high                        | ρ = 0.9                             |
| E4   | Are long alignment tasks treated fairly?                          | per category and size class                   | E1                                  |
| E5   | How robust is each policy to estimation error?                    | σ = 0, 0.25, 0.5, 1.0                         | E1                                  |
| E6   | Do scheduling calculations outweigh gains, and how does it scale? | N = 100 … 2000; M = 1 … 16                    | ρ = 0.9                             |
| E7   | How sensitive is Adaptive to its parameters?                      | weights, c, θ, q, deadline and priority terms | E1                                  |
| E8   | Do the results hold on measured tasks?                            | algorithm                                     | 80 executed tasks                   |
| E9   | How does sequence length affect results?                          | length × 0.25 … × 4                           | ρ = 0.9                             |
| E10  | Does simulation agree with real execution?                        | hardware vs simulation                        | 60 tasks, 5 repetitions             |

**7.2 Metrics.**
- *Task-level:* Tᵢ = Cᵢ − Aᵢ, Wᵢ = Tᵢ − Bᵢ, Rᵢ = Sᵢ − Aᵢ, slowdown Tᵢ/Bᵢ, deadline lateness.
- *System-level:*
  - averages and p50 / p90 / p95 / p99 of W, T and R
  - throughput and CPU utilisation normalised by M·T, plus the idle fraction
  - Jain fairness index on 1/slowdown
  - starved tasks (W > 10 × mean B) and max W / mean B
  - waiting time for priority 1–2 and 4–5 tasks
  - deadline miss rate
  - context switches and preemptions
  - scheduler decisions and wall-clock overhead

**7.3 Statistics.** Each setting is run with 10 seeds, and results are given as mean and 95 % CI.
Adaptive is compared with each baseline by paired differences on the same seeds, giving an
absolute Δ with its CI, a relative change, and a Wilcoxon signed-rank p-value. When the baseline
is near zero, only the absolute difference is reported. Every result is labelled *simulation* or
*hardware*.

## 8. Implementation progress

All seven modules are implemented, and 33 automated tests pass. These include the textbook
scheduling examples, a check that schedulers cannot see the actual burst, identical-input
fingerprints, starvation guard behaviour, idle logging, and the statistics.

**E1 results (simulation; 4 cores, ρ = 0.9; mean [95 % CI] over 10 seeds).**

| Algorithm | Avg W (ms) | Avg R (ms) | Max W (ms) | Avg slowdown | Jain | Miss rate |
|---|---|---|---|---|---|---|
| FCFS | 45.1 [20.4, 69.7] | 45.1 | 136 | 15.7 | 0.60 | 0.38 |
| SJF | 15.1 [10.6, 19.5] | 15.1 | 364 | 2.70 | 0.83 | 0.05 |
| SRTF | 9.2 [6.6, 11.7] | 3.3 | 528 | 1.14 | 0.98 | 0.01 |
| RR (q = 10) | 29.7 [15.6, 43.7] | 11.2 | 372 | 5.14 | 0.72 | 0.21 |
| Priority | 47.1 [21.3, 73.0] | 47.1 | 404 | 16.71 | 0.71 | 0.23 |
| **Adaptive** | **16.4 [5.2, 27.6]** | **13.3** | **246** | **2.34** | **0.87** | **0.05** |

Findings:
- **FCFS, RR and Priority.** Adaptive reduces average waiting by 64 %, 45 % and 65 % respectively
  (all p = 0.002). It also improves fairness and cuts the deadline miss rate by 75–86 %.
- **SJF.** Adaptive has lower waiting on 8 of 10 seeds (median 10.2 ms against 13.2 ms). Its mean is
  9 % higher because of one congested seed, so the difference is not significant (p = 0.11).
  Adaptive's worst-case waiting is 33 % lower (246 ms against 364 ms).
- **SRTF.** SRTF remains best for average waiting. Adaptive halves its worst case (246 ms against
  528 ms), and the gap grows with load: at ρ = 1.2 the maximum waiting is 388 ms against 1079 ms (E2).
- **Long tasks (E4).** Long tasks wait 85 ms under Adaptive, compared with 72 ms (SJF), 96 ms (SRTF)
  and 111 ms (RR). Short tasks wait only 3.1 ms.
- **Overload (E2, E5).** Adaptive's average waiting rises faster than SJF's because the aging term
  starts to dominate. This trade-off between mean and worst-case waiting is controlled by c (E7):
  c = 16 gives a mean of 12.9 ms but a maximum of 331 ms.
- **Overhead (E6).** Adaptive costs about 7 ms of policy time per 300-task run and about 75 ms at
  2000 tasks, compared with 10 ms for SRTF. This is small next to the waiting time it saves, but it
  grows faster than linearly.
- **Hardware (E10).** With real execution on worker processes, the ranking agrees with simulation:
  Adaptive-NP ≈ SJF < Priority < FCFS (Adaptive-NP 33.2 ms, SJF 34.9 ms). Hardware waits are 2–4 ms
  above the replayed simulation,
  which is the dispatcher's inter-process communication overhead.

## 9. Challenges encountered and the plan for completing the remaining work

**Challenges**
1. *The range of burst sizes (about 0.2–300 ms).* A linear shortness term let the smallest task
   always win, so the term was replaced with a log-normalised one.
2. *Aging scale.* A fixed constant either starved alignment tasks or degraded towards FCFS.
   Normalising by the running mean burst b̄ helps, but under overload the aging term still
   dominates. This is quantified in E2, E5 and E7.
3. *Information leakage.* Early versions gave schedulers access to the full task record. Now
   schedulers only receive `TaskInfo`, which has no actual burst, and a test enforces this.
4. *Proving identical inputs.* Workloads are frozen and fingerprinted, and each algorithm gets a
   fresh simulator.
5. *Hardware noise.* Running the dispatcher and workers on all cores distorted the measurements,
   so one core is now reserved for the dispatcher, the algorithm order is rotated, and runs are
   repeated 5 times.

**Plan for completion**
- Make the weights adaptive online. For example, lower the aging weight as the ready queue grows,
  to fix the overload weakness while keeping the worst-case guarantee.
- Reduce the per-decision cost of Adaptive by incremental score updates or a heap. E6 shows its
  overhead grows faster than linearly.
- Run E8 and E10 on a real NCBI genome and on a second machine with more cores.
- Add a preemptive real-execution mode, for example by splitting tasks into chunks, so that SRTF,
  RR and quantum-based Adaptive can also be validated on hardware.
- Prepare the final report figures and the discussion.
