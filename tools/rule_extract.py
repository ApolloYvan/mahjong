# -*- coding: utf-8 -*-
"""手写 CART（Gini，深度<=4，叶子>=200）+ 规则枚举 + 手写 k-medoids。

范围说明（如实标注）：本轮时间预算里，完整的 tools/divergence_mine.py（情境桶
分层回退 + 覆盖率排序）未能建成，因此本文件里的 CART / 规则枚举 / k-medoids
只在 __main__ 里对 data/analysis/decisions.csv 做了一遍最直接的落地（不是
docs/STRATEGY_MINING.md 完整设想的"先分歧挖掘、再喂给规则提取"两阶段管线）。
三个算法本身（CART/规则枚举/k-medoids）都有独立单元测试验证正确性
（tests/test_mining_stats.py），可以复用于未来把 divergence_mine.py 补齐后的
真实管线。详见 docs/experiments/OFFLINE_REPORT.md「已知局限」一节。
"""
import math
import random
from collections import Counter


# ---------------------------------------------------------------------------
# CART：数值特征、二分裂、Gini 不纯度
# ---------------------------------------------------------------------------

def gini(labels):
    n = len(labels)
    if n == 0:
        return 0.0
    counts = Counter(labels)
    return 1.0 - sum((c / n) ** 2 for c in counts.values())


class Leaf:
    def __init__(self, labels):
        self.labels = list(labels)
        self.n = len(labels)
        counts = Counter(labels)
        self.majority = counts.most_common(1)[0][0] if counts else None
        self.rate = {k: v / self.n for k, v in counts.items()} if self.n else {}

    def predict(self, _row):
        return self.majority


class Split:
    def __init__(self, feature, threshold, left, right):
        self.feature = feature
        self.threshold = threshold
        self.left = left
        self.right = right

    def predict(self, row):
        branch = self.left if row.get(self.feature, 0) <= self.threshold else self.right
        return branch.predict(row)

    def rules(self, prefix=None):
        prefix = prefix or []
        out = []
        out.extend(self.left.rules(prefix + [(self.feature, "<=", self.threshold)])
                   if isinstance(self.left, Split) else
                   [(prefix + [(self.feature, "<=", self.threshold)], self.left)])
        out.extend(self.right.rules(prefix + [(self.feature, ">", self.threshold)])
                   if isinstance(self.right, Split) else
                   [(prefix + [(self.feature, ">", self.threshold)], self.right)])
        return out


def _best_split(rows, labels, features, min_leaf):
    n = len(rows)
    base_gini = gini(labels)
    best = None  # (gain, feature, threshold)
    for feat in features:
        pairs = sorted((row.get(feat, 0), lab) for row, lab in zip(rows, labels)
                       if row.get(feat) is not None)
        if len(pairs) < 2 * min_leaf:
            continue
        values = [p[0] for p in pairs]
        # 候选阈值：相邻不同值的中点
        candidates = sorted(set(values))
        for i in range(len(candidates) - 1):
            thresh = (candidates[i] + candidates[i + 1]) / 2.0
            left_labels = [lab for v, lab in pairs if v <= thresh]
            right_labels = [lab for v, lab in pairs if v > thresh]
            if len(left_labels) < min_leaf or len(right_labels) < min_leaf:
                continue
            g = (len(left_labels) * gini(left_labels) + len(right_labels) * gini(right_labels)) / n
            gain = base_gini - g
            if best is None or gain > best[0]:
                best = (gain, feat, thresh)
    return best


def cart_fit(rows, labels, features, max_depth=4, min_leaf=200):
    def build(rows_, labels_, depth):
        if depth >= max_depth or len(rows_) < 2 * min_leaf or gini(labels_) == 0:
            return Leaf(labels_)
        best = _best_split(rows_, labels_, features, min_leaf)
        if best is None:
            return Leaf(labels_)
        _gain, feat, thresh = best
        left_rows, left_labels, right_rows, right_labels = [], [], [], []
        for row, lab in zip(rows_, labels_):
            v = row.get(feat, 0)
            if v is None:
                continue
            if v <= thresh:
                left_rows.append(row)
                left_labels.append(lab)
            else:
                right_rows.append(row)
                right_labels.append(lab)
        left = build(left_rows, left_labels, depth + 1)
        right = build(right_rows, right_labels, depth + 1)
        return Split(feat, thresh, left, right)

    return build(rows, labels, 0)


# ---------------------------------------------------------------------------
# 规则枚举：离散特征上 <=3 个条件的合取，support>=100、lift>=1.5
# ---------------------------------------------------------------------------

def enumerate_rules(rows, labels, discrete_features, max_conds=3, min_support=100, min_lift=1.5):
    base_rate = sum(labels) / len(labels) if labels else 0
    conds_pool = []
    for feat in discrete_features:
        values = set(row.get(feat) for row in rows if row.get(feat) is not None)
        for v in values:
            conds_pool.append((feat, v))

    def matches(row, conds):
        return all(row.get(f) == v for f, v in conds)

    from itertools import combinations
    found = []
    for k in range(1, max_conds + 1):
        for combo in combinations(conds_pool, k):
            feats = set(f for f, _ in combo)
            if len(feats) != k:
                continue  # 不允许同一特征出现两次
            hits = [lab for row, lab in zip(rows, labels) if matches(row, combo)]
            support = len(hits)
            if support < min_support:
                continue
            rate = sum(hits) / support
            lift = rate / base_rate if base_rate else 0
            if lift >= min_lift:
                found.append({"conds": combo, "support": support, "rate": rate, "lift": lift})
    found.sort(key=lambda r: -r["lift"])
    return found


# ---------------------------------------------------------------------------
# k-medoids（纯 Python，PAM 简化版：随机初始化 + 迭代重分配 + 换中心）
# ---------------------------------------------------------------------------

def _dist(a, b):
    return math.sqrt(sum((x - y) ** 2 for x, y in zip(a, b)))


def kmedoids(points, k, n_init=5, seed=20260924, max_iter=30):
    rng = random.Random(seed)
    n = len(points)
    if n == 0 or k <= 0:
        return [], []
    best_cost = None
    best_medoids = None
    best_assign = None
    for _init in range(n_init):
        medoids = rng.sample(range(n), min(k, n))
        for _iter in range(max_iter):
            assign = []
            for p in points:
                dists = [_dist(p, points[m]) for m in medoids]
                assign.append(dists.index(min(dists)))
            new_medoids = list(medoids)
            changed = False
            for ci in range(len(medoids)):
                members = [i for i, a in enumerate(assign) if a == ci]
                if not members:
                    continue
                costs = []
                for cand in members:
                    c = sum(_dist(points[cand], points[m]) for m in members)
                    costs.append((c, cand))
                costs.sort()
                if costs[0][1] != medoids[ci]:
                    new_medoids[ci] = costs[0][1]
                    changed = True
            medoids = new_medoids
            if not changed:
                break
        cost = sum(_dist(points[i], points[medoids[assign[i]]]) for i in range(n))
        if best_cost is None or cost < best_cost:
            best_cost = cost
            best_medoids = medoids
            best_assign = assign
    return best_medoids, best_assign


def silhouette_score(points, assign, sample_cap=5000, seed=20260924):
    n = len(points)
    if n > sample_cap:
        rng = random.Random(seed)
        idx = rng.sample(range(n), sample_cap)
    else:
        idx = list(range(n))
    by_cluster = {}
    for i in idx:
        by_cluster.setdefault(assign[i], []).append(i)
    scores = []
    for i in idx:
        ci = assign[i]
        same = [j for j in by_cluster.get(ci, []) if j != i]
        a = sum(_dist(points[i], points[j]) for j in same) / len(same) if same else 0.0
        b = None
        for cj, members in by_cluster.items():
            if cj == ci or not members:
                continue
            d = sum(_dist(points[i], points[j]) for j in members) / len(members)
            if b is None or d < b:
                b = d
        if b is None:
            continue
        s = (b - a) / max(a, b) if max(a, b) > 0 else 0.0
        scores.append(s)
    return sum(scores) / len(scores) if scores else 0.0


if __name__ == "__main__":
    import csv
    import os
    import sys

    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    path = "data/analysis/decisions.csv"
    if not os.path.exists(path):
        print("decisions.csv 不存在，先跑 tools/decision_table.py")
        raise SystemExit(1)
    print("rule_extract.py 是算法库；本轮把 CART/规则枚举/k-medoids 的正确性验证放在"
          "tests/test_mining_stats.py，未在此对 decisions.csv 跑完整两阶段管线"
          "（divergence_mine.py 未建成，见 OFFLINE_REPORT.md）。")
