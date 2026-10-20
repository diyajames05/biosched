"""Correctness tests. Run:  python -m unittest discover -s tests   (or pytest)

The single-core cases are the classic textbook examples (Silberschatz,
Operating System Concepts), whose average waiting times are known.
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from bioschedsim import bio_ops
from bioschedsim.metrics import check_invariants, summarize
from bioschedsim.schedulers import (FCFS, SJF, SRTF, AdaptiveScheduler, PriorityScheduler,
                                    RoundRobin, default_suite)
from bioschedsim.simulator import simulate
from bioschedsim.workload import Task, generate_synthetic, load_workload, save_workload


def mk(spec):
    """spec: list of (arrival, burst, priority)"""
    return [Task(i, "gc_content", a, b, b, p, 1000, None, {}) for i, (a, b, p) in enumerate(spec)]


class TextbookExamples(unittest.TestCase):
    def avg_wait(self, spec, sched, cores=1):
        r = simulate(mk(spec), sched, cores)
        check_invariants(r)
        return summarize(r)["avg_waiting"]

    def test_fcfs(self):            # P1=24, P2=3, P3=3 -> (0+24+27)/3 = 17
        self.assertAlmostEqual(self.avg_wait([(0, 24, 1), (0, 3, 1), (0, 3, 1)], FCFS()), 17.0)

    def test_sjf(self):             # 6,8,7,3 all at 0 -> 7
        self.assertAlmostEqual(self.avg_wait([(0, 6, 1), (0, 8, 1), (0, 7, 1), (0, 3, 1)], SJF()), 7.0)

    def test_srtf(self):            # arrivals 0,1,2,3 bursts 8,4,9,5 -> 6.5
        self.assertAlmostEqual(self.avg_wait([(0, 8, 1), (1, 4, 1), (2, 9, 1), (3, 5, 1)], SRTF()), 6.5)

    def test_rr(self):              # q=4, 24,3,3 -> 17/3 = 5.667
        self.assertAlmostEqual(self.avg_wait([(0, 24, 1), (0, 3, 1), (0, 3, 1)], RoundRobin(4)), 17 / 3)

    def test_priority(self):        # bursts 10,1,2,1,5 prio 3,1,4,5,2 -> 8.2
        spec = [(0, 10, 3), (0, 1, 1), (0, 2, 4), (0, 1, 5), (0, 5, 2)]
        self.assertAlmostEqual(self.avg_wait(spec, PriorityScheduler()), 8.2)

    def test_two_cores_fcfs(self):  # 4 tasks of 10 on 2 cores -> waits 0,0,10,10
        self.assertAlmostEqual(self.avg_wait([(0, 10, 1)] * 4, FCFS(), cores=2), 5.0)

    def test_utilization_normalised_by_cores(self):
        r = simulate(mk([(0, 10, 1)] * 4), FCFS(), 2)
        s = summarize(r)
        self.assertAlmostEqual(s["cpu_utilization"], 1.0)
        r = simulate(mk([(0, 10, 1)] * 2), FCFS(), 4)
        self.assertAlmostEqual(summarize(r)["cpu_utilization"], 0.5)

    def test_context_switch_cost(self):
        r = simulate(mk([(0, 5, 1), (0, 5, 1)]), FCFS(), 1, context_switch=1.0)
        self.assertAlmostEqual(r.tasks[1].completion, 12.0)


class RandomWorkloads(unittest.TestCase):
    def test_invariants_all_algorithms(self):
        for cores in (1, 2, 4):
            for seed in range(3):
                wl = generate_synthetic(120, cores, load=0.95, seed=seed)
                for s in default_suite() + [PriorityScheduler(True),
                                            AdaptiveScheduler(preemptive=True, quantum=None)]:
                    r = simulate(wl, s, cores, context_switch=0.05)
                    check_invariants(r)

    def test_input_not_modified(self):
        wl = generate_synthetic(50, 2, seed=1)
        before = [(t.arrival, t.burst) for t in wl]
        simulate(wl, SRTF(), 2)
        self.assertEqual(before, [(t.arrival, t.burst) for t in wl])

    def test_adaptive_no_starvation(self):
        # one long task + an endless stream of short ones: SJF starves it, Adaptive must not
        spec = [(0, 1, 3), (0.5, 100, 3)] + [(i * 1.0, 1.0, 3) for i in range(1, 600)]
        sjf = simulate(mk(spec), SJF(), 1)
        ada = simulate(mk(spec), AdaptiveScheduler(quantum=5), 1)
        long_sjf = next(s for s in sjf.tasks if s.task.task_id == 1).completion
        long_ada = next(s for s in ada.tasks if s.task.task_id == 1).completion
        self.assertGreater(long_sjf, 600)          # starved until the stream ends
        self.assertLess(long_ada, long_sjf * 0.6)  # served much earlier

    def test_csv_roundtrip(self):
        wl = generate_synthetic(20, seed=3)
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "w.csv")
            save_workload(wl, p)
            self.assertEqual([t.__dict__ for t in load_workload(p)], [t.__dict__ for t in wl])


class BioOps(unittest.TestCase):
    def test_ops(self):
        self.assertAlmostEqual(bio_ops.gc_content("GGCCAATT"), 0.5)
        self.assertEqual(bio_ops.kmer_count("AAAA", 2)["AA"], 3)
        self.assertEqual(bio_ops.motif_search("ACGACGT", "ACG"), [0, 3])
        self.assertEqual(bio_ops.motif_search("ACGTTCGT", "ACGT", 1), [0, 4])
        self.assertEqual(bio_ops.needleman_wunsch("GATTACA", "GATTACA"), 7)
        self.assertEqual(bio_ops.needleman_wunsch("A", ""), -2)


if __name__ == "__main__":
    unittest.main()
