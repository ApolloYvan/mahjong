# -*- coding: utf-8 -*-
""""剩余差距"审计的统计部分：读 tools/top_policy_replay.py 的产出
（data/analysis/top_policy_hu.csv / top_policy_claims.csv），计算：
  1. 两类决策点上，我们（开关全关 / 两个开关都开）与高手的一致率。
  2. 按局面类型聚类的不一致情形 + 同人内 Δ(y_score) 房间聚类自助 CI。
  3. 按 次数×分差 排序的前 10 类。

用法：
    python3 tools/gap_audit.py
"""
import csv
import json
import os
import sys
from collections import Counter, defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.mining_common import cluster_bootstrap_ci, within_person_delta, mean  # noqa: E402

NUMERIC = {"round_no", "seq", "seat", "is_dealer", "wall_left", "jokers_before",
           "drew_joker", "meld_groups", "shanten_before", "melds_before",
           "before_shanten", "catch_play", "y_hu", "y_score"}


def load(path):
    rows = []
    with open(path, encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            for k in NUMERIC:
                if k in r:
                    v = r[k]
                    r[k] = int(v) if v not in (None, "") else None
            rows.append(r)
    return rows


def norm_hu_act(a):
    return "decline_hu" if a == "discard" else a


def jbucket(j):
    j = j or 0
    return "0" if j == 0 else ("1" if j == 1 else "2+")


def mbucket(m):
    m = m or 0
    return "0" if m == 0 else ("1" if m == 1 else "2+")


def wbucket(w):
    w = w or 0
    return ">40" if w > 40 else ("21-40" if w > 20 else "<=20")


def agreement(rows, real_key, policy_key, norm=None):
    norm = norm or (lambda x: x)
    n = len(rows)
    agree = sum(1 for r in rows if r[real_key] == norm(r[policy_key]))
    return agree, n, (agree / n if n else None)


def bucket_key_hu(r):
    tenpai = (r["shanten_before"] == 0)
    return (tenpai, jbucket(r["jokers_before"]), mbucket(r["meld_groups"]),
           bool(r["is_dealer"]), wbucket(r["wall_left"]))


def bucket_key_claim(r):
    # window_kind 必须进桶键：chi 窗口和 peng/gang 窗口是两种不可比的决策
    # （chi 窗口下 act_type 永远不可能是 peng，反之亦然），混在一个桶里比较
    # "高手选 peng / 我们选 chi" 是无意义的 artifact，不是真实分歧。
    tenpai = (r["before_shanten"] == 0)
    return (r["window_kind"], tenpai, jbucket(r["jokers_before"]), mbucket(r["melds_before"]),
           bool(r["is_dealer"]), wbucket(r["wall_left"]))


def analyze(rows, bucket_key_fn, real_key, policy_key, kind_label, norm=None):
    """返回每个桶的：n, mismatch_n, 高手典型动作, 我们典型动作(policy_key 口径),
    Δ(y_score)(同人内, 房间聚类自助 CI)。"""
    norm = norm or (lambda x: x)
    by_bucket = defaultdict(list)
    for r in rows:
        by_bucket[bucket_key_fn(r)].append(r)

    results = []
    for key, brows in by_bucket.items():
        n = len(brows)
        real_counts = Counter(r[real_key] for r in brows)
        policy_counts = Counter(norm(r[policy_key]) for r in brows)
        hs_action = real_counts.most_common(1)[0][0]
        our_action = policy_counts.most_common(1)[0][0]
        mismatch_n = sum(1 for r in brows if r[real_key] != norm(r[policy_key]))
        if hs_action == our_action:
            # 这一桶高手和我们代码的"典型动作"本来就一致，不是差距来源
            continue

        def group_of(r, hs=hs_action, ours=our_action):
            if r[real_key] == hs:
                return "HS"
            if r[real_key] == ours:
                return "OURS"
            return None

        n_hs = sum(1 for r in brows if group_of(r) == "HS")
        n_ours = sum(1 for r in brows if group_of(r) == "OURS")
        delta, per_uid = within_person_delta(
            brows, lambda r: r["uid"], group_of, lambda r: r["y_score"] or 0, "HS", "OURS")

        def stat_fn(sample_rows, bucket_key_fn=bucket_key_fn, key=key, hs=hs_action, ours=our_action,
                   real_key=real_key, policy_key=policy_key, norm=norm):
            sub = [r for r in sample_rows if bucket_key_fn(r) == key]

            def g(r):
                if r[real_key] == hs:
                    return "HS"
                if r[real_key] == ours:
                    return "OURS"
                return None
            d, _ = within_person_delta(sub, lambda r: r["uid"], g, lambda r: r["y_score"] or 0, "HS", "OURS")
            return d

        point, lo, hi = cluster_bootstrap_ci(rows, lambda r: r["room_id"], stat_fn, n_boot=1000)
        sufficient = n_hs >= 30 and n_ours >= 30
        results.append({
            "kind": kind_label, "bucket": key, "n": n, "mismatch_n": mismatch_n,
            "hs_action": hs_action, "our_action": our_action,
            "n_hs": n_hs, "n_ours": n_ours, "sufficient": sufficient,
            "delta": delta, "ci_lo": lo, "ci_hi": hi,
            "n_people": len(per_uid),
            # 排序权重：次数 x |点估计分差|——不要求两边都 >=30 才进入排序，
            # 只是样本不足的桶在展示时另外标注，不作为可信结论。
            "score": (mismatch_n * abs(delta)) if delta is not None else 0,
        })
    return results


def main():
    hu_rows = load("data/analysis/top_policy_hu.csv")
    claim_rows = load("data/analysis/top_policy_claims.csv")
    print("hu_or_not 行数：%d  claim 行数：%d" % (len(hu_rows), len(claim_rows)))

    print("\n=== 一致率 ===")
    for label, policy_key in (("开关全关", "our_policy_act_off"), ("两个开关都开", "our_policy_act_on")):
        a, n, rate = agreement(hu_rows, "act_type", policy_key, norm_hu_act)
        print("hu_or_not  %-10s  n=%d  一致=%d  一致率=%.4f" % (label, n, a, rate))
    for label, policy_key in (("开关全关", "our_policy_act_off"), ("两个开关都开", "our_policy_act_on")):
        a, n, rate = agreement(claim_rows, "act_type", policy_key)
        print("claim      %-10s  n=%d  一致=%d  一致率=%.4f" % (label, n, a, rate))

    print("\n=== 按局面类型聚类的不一致（用'两个开关都开'的策略）===")
    hu_results = analyze(hu_rows, bucket_key_hu, "act_type", "our_policy_act_on", "hu_or_not", norm_hu_act)
    claim_results = analyze(claim_rows, bucket_key_claim, "act_type", "our_policy_act_on", "claim")
    all_results = hu_results + claim_results
    all_results.sort(key=lambda r: -r["score"])

    out = {"hu_results": hu_results, "claim_results": claim_results}
    with open("data/analysis/gap_audit.json", "w", encoding="utf-8") as fh:
        json.dump(out, fh, ensure_ascii=False, indent=2, default=str)

    print("\n=== 前 10 类（按 次数(mismatch_n) x |点估计分差| 排序；"
         "样本不足（任一边 <30）的桶单独标注，不当作可信结论）===")
    for r in all_results[:10]:
        flag = "" if r["sufficient"] else "  [样本不足，仅供参考]"
        delta_s = ("%.2f" % r["delta"]) if r["delta"] is not None else "NA"
        ci_s = ("[%.2f,%.2f]" % (r["ci_lo"], r["ci_hi"])) if r["ci_lo"] is not None else "NA"
        print("[%s] bucket=%s  mismatch_n=%d  高手=%s 我们=%s  n_hs=%d n_ours=%d "
             "delta=%s CI=%s n_people=%d  score=%.1f%s" %
             (r["kind"], r["bucket"], r["mismatch_n"], r["hs_action"], r["our_action"],
              r["n_hs"], r["n_ours"], delta_s, ci_s, r["n_people"], r["score"], flag))

    print("\n=== 全部桶（含样本不足）按 mismatch_n 降序，用于查看被样本不足排除的桶 ===")
    for r in sorted(all_results, key=lambda r: -r["mismatch_n"])[:20]:
        flag = "" if r["sufficient"] else "  [样本不足]"
        print("[%s] bucket=%s mismatch_n=%d 高手=%s 我们=%s n_hs=%d n_ours=%d%s" %
             (r["kind"], r["bucket"], r["mismatch_n"], r["hs_action"], r["our_action"],
              r["n_hs"], r["n_ours"], flag))


if __name__ == "__main__":
    main()
