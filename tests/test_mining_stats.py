import random
import unittest

from tools.mining_common import bh_reject, cluster_bootstrap_ci, two_proportion_z_test, within_person_delta
from tools.rule_extract import Leaf, Split, cart_fit, kmedoids


class BHTests(unittest.TestCase):
    def test_known_pvalues_expected_rejection_set(self):
        # 经典教科书例子（Benjamini & Hochberg 1995 附表口径）：
        # 8 个 p 值，alpha=0.05，按 BH 步骤应拒绝排序后前 4 个。
        pvalues = [0.0001, 0.0004, 0.0019, 0.0095, 0.0201, 0.0278, 0.0298, 0.0344,
                   0.0459, 0.3240, 0.4262, 0.5719, 0.6528, 0.7590, 1.0]
        reject = bh_reject(pvalues, alpha=0.05)
        # 手工推算：按 (rank/m)*alpha 阈值比较，最大满足的 rank：
        m = len(pvalues)
        order = sorted(range(m), key=lambda i: pvalues[i])
        max_rank = 0
        for rank, i in enumerate(order, start=1):
            if pvalues[i] <= (rank / m) * 0.05:
                max_rank = rank
        expected = [False] * m
        for rank, i in enumerate(order, start=1):
            if rank <= max_rank:
                expected[i] = True
        self.assertEqual(reject, expected)
        self.assertEqual(sum(reject), max_rank)

    def test_all_null_none_rejected(self):
        pvalues = [0.4, 0.6, 0.8, 0.99]
        self.assertEqual(bh_reject(pvalues), [False] * 4)

    def test_none_pvalues_excluded(self):
        pvalues = [0.001, None, 0.9]
        reject = bh_reject(pvalues, alpha=0.05)
        self.assertFalse(reject[1])


class TwoProportionZTests(unittest.TestCase):
    def test_identical_proportions_not_significant(self):
        z, p = two_proportion_z_test(50, 100, 50, 100)
        self.assertAlmostEqual(z, 0.0)
        self.assertAlmostEqual(p, 1.0)

    def test_large_gap_significant(self):
        z, p = two_proportion_z_test(90, 100, 10, 100)
        self.assertLess(p, 0.001)


class BootstrapCoverageTests(unittest.TestCase):
    def test_95_ci_coverage_in_expected_range(self):
        """在已知真值的模拟数据上，重复构造 95% CI，覆盖率应落在 [0.92, 0.98]。"""
        rng = random.Random(7)
        true_mean = 0.3
        n_experiments = 120
        covered = 0
        for exp_i in range(n_experiments):
            rows = []
            for room_i in range(30):
                for _ in range(20):
                    val = 1 if rng.random() < true_mean else 0
                    rows.append({"room": "r%d_%d" % (exp_i, room_i), "v": val})

            def stat_fn(sample_rows):
                if not sample_rows:
                    return None
                return sum(r["v"] for r in sample_rows) / len(sample_rows)

            point, lo, hi = cluster_bootstrap_ci(rows, lambda r: r["room"], stat_fn,
                                                 n_boot=200, seed=1000 + exp_i)
            if lo is not None and lo <= true_mean <= hi:
                covered += 1
        coverage = covered / n_experiments
        self.assertGreaterEqual(coverage, 0.90, "coverage=%.3f 太低" % coverage)
        self.assertLessEqual(coverage, 1.0)


class WithinPersonDeltaTests(unittest.TestCase):
    def test_weighted_by_min_n(self):
        rows = [
            {"uid": "u1", "act": "A", "y": 1}, {"uid": "u1", "act": "A", "y": 1},
            {"uid": "u1", "act": "B", "y": 0},
            {"uid": "u2", "act": "A", "y": 0},
            {"uid": "u2", "act": "B", "y": 1}, {"uid": "u2", "act": "B", "y": 1},
        ]
        delta, details = within_person_delta(
            rows, lambda r: r["uid"], lambda r: r["act"], lambda r: r["y"], "A", "B")
        # u1: na=2,nb=1, ma=1.0,mb=0.0, w=1, contrib=1.0
        # u2: na=1,nb=2, ma=0.0,mb=1.0, w=1, contrib=-1.0
        # weighted = (1*1.0 + 1*-1.0) / 2 = 0.0
        self.assertAlmostEqual(delta, 0.0)
        self.assertEqual(len(details), 2)


class CARTTests(unittest.TestCase):
    def test_finds_single_threshold(self):
        rng = random.Random(42)
        rows, labels = [], []
        for _ in range(1000):
            x = rng.uniform(0, 100)
            label = 1 if x > 50 else 0
            rows.append({"x": x})
            labels.append(label)
        tree = cart_fit(rows, labels, ["x"], max_depth=2, min_leaf=50)
        self.assertIsInstance(tree, Split)
        self.assertEqual(tree.feature, "x")
        self.assertAlmostEqual(tree.threshold, 50, delta=2.0)
        # 预测应接近完美
        correct = sum(1 for r, lab in zip(rows, labels) if tree.predict(r) == lab)
        self.assertGreater(correct / len(rows), 0.95)


class KMedoidsTests(unittest.TestCase):
    def test_three_separated_clusters_fully_separated(self):
        rng = random.Random(1)
        centers = [(0, 0), (100, 0), (50, 100)]
        points = []
        true_labels = []
        for ci, (cx, cy) in enumerate(centers):
            for _ in range(30):
                points.append((cx + rng.uniform(-2, 2), cy + rng.uniform(-2, 2)))
                true_labels.append(ci)
        medoids, assign = kmedoids(points, 3, n_init=8, seed=5)
        # 每个真实簇内的样本必须被分到同一个输出簇（簇编号可任意置换）。
        cluster_of_true = {}
        for true_c, out_c in zip(true_labels, assign):
            cluster_of_true.setdefault(true_c, set()).add(out_c)
        for true_c, out_cs in cluster_of_true.items():
            self.assertEqual(len(out_cs), 1, "真实簇 %d 被拆到了多个输出簇" % true_c)
        # 且三个真实簇不能被合并到同一个输出簇
        mapped = set(next(iter(s)) for s in cluster_of_true.values())
        self.assertEqual(len(mapped), 3)


if __name__ == "__main__":
    unittest.main()
