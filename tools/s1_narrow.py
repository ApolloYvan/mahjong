# -*- coding: utf-8 -*-
"""S1 收窄：在"S1 会触发"的 hu_or_not 行里，按 (有效财神数:1/2+) x (副露数:0/1/2+)
分格，找出高手弃胡率>=40% 且 Δ(y_score) 房间聚类 CI 下界>0 的格子——只有
这些格子保留为新触发条件，其余格子改成直接胡。

数据范围：top cohort（10 人）+ 嘎达嘎达（uid=u_45062bea0121，100 场对局）
单独跑一遍对照，两者独立出结论。

有效财神数 = jokers_before（decisions.csv 的 jokers 字段，摸牌前手上财神数）
+ (1 if 本次摸到白板 else 0)——即"决策那一刻手里能用的财神数"，与生产代码
_decline_small_hu_tile 里 hu_result["counts"][JOKER_IDX]（evaluate() 摸牌后
的 counts）语义一致（摸到财神时 evaluate 会把这张也计进 counts）。

用法：
    python3 tools/s1_narrow.py
"""
import csv
import json
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.hypotheses import load_rows  # noqa: E402
from tools.mining_common import cluster_bootstrap_ci, within_person_delta  # noqa: E402

GADA_UID = "u_45062bea0121"


def load_reach():
    reach = {}
    with open("data/analysis/s1_recheck_baotou.csv", encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            key = (r["room_id"], r["game_id"], int(r["round_no"]), int(r["seq"]), int(r["seat"]))
            reach[key] = int(r["baotou_reachable_after_decline"])
    return reach


def effective_jokers_bucket(r):
    eff = (r["jokers"] or 0) + (1 if r["act_tile"] == "白" else 0)
    return "1" if eff == 1 else "2+"


def melds_bucket(r):
    m = r["melds"] or 0
    return "0" if m == 0 else ("1" if m == 1 else "2+")


def s1_triggers(r):
    return (r["dtype"] == "hu_or_not" and r["is_baotou"] == 0
           and ((r["jokers"] or 0) >= 1 or r["act_tile"] == "白")
           and r.get("_reach") == 1
           and (r["wall_left"] or 0) >= 28)


def analyze_cohort(rows, label):
    pop = [r for r in rows if s1_triggers(r)]
    print("\n=== %s：S1 会触发的 hu_or_not 行数 = %d ===" % (label, len(pop)))
    from collections import defaultdict
    buckets = defaultdict(list)
    for r in pop:
        buckets[(effective_jokers_bucket(r), melds_bucket(r))].append(r)

    results = {}
    for key in sorted(buckets):
        brows = buckets[key]
        n = len(brows)
        n_decline = sum(1 for r in brows if r["act_type"] == "decline_hu")
        decline_rate = n_decline / n if n else None

        def group_of(r):
            if r["act_type"] == "decline_hu":
                return "decline"
            if r["act_type"] == "hu":
                return "hu"
            return None

        delta, per_uid = within_person_delta(
            brows, lambda r: r["uid"], group_of, lambda r: r["y_score"] or 0, "decline", "hu")

        def stat_fn(sample_rows, key=key):
            sub = [r for r in sample_rows if s1_triggers(r) and
                  (effective_jokers_bucket(r), melds_bucket(r)) == key]
            d, _ = within_person_delta(sub, lambda r: r["uid"], group_of,
                                       lambda r: r["y_score"] or 0, "decline", "hu")
            return d

        point, lo, hi = cluster_bootstrap_ci(rows, lambda r: r["room_id"], stat_fn, n_boot=1000)
        keep = bool(decline_rate is not None and decline_rate >= 0.40 and lo is not None and lo > 0)
        results[key] = {
            "n": n, "n_decline": n_decline, "decline_rate": decline_rate,
            "delta": delta, "ci_lo": lo, "ci_hi": hi, "n_people": len(per_uid), "keep": keep,
        }
        print("jokers=%-3s melds=%-3s  n=%-5d decline_rate=%s  Δ=%s CI=[%s,%s] n_people=%d  KEEP=%s" %
             (key[0], key[1], n,
              ("%.3f" % decline_rate) if decline_rate is not None else "NA",
              ("%.2f" % delta) if delta is not None else "NA",
              ("%.2f" % lo) if lo is not None else "NA",
              ("%.2f" % hi) if hi is not None else "NA",
              len(per_uid), keep))
    return results


def main():
    rows = load_rows("data/analysis/decisions.csv")
    reach = load_reach()
    for r in rows:
        if r["dtype"] == "hu_or_not":
            key = (r["room_id"], r["game_id"], r["round_no"], int(r["seq"]), r["seat"])
            r["_reach"] = reach.get(key)

    top_rows = [r for r in rows if r["cohort"] == "top"]
    gada_rows = [r for r in rows if r["uid"] == GADA_UID]
    print("嘎达嘎达 (uid=%s) 总决策行数=%d hu_or_not 行数=%d" %
         (GADA_UID, len(gada_rows), sum(1 for r in gada_rows if r["dtype"] == "hu_or_not")))

    top_results = analyze_cohort(top_rows, "top cohort (10人)")
    gada_results = analyze_cohort(gada_rows, "嘎达嘎达 (单人, 100场)")

    out = {"top": {str(k): v for k, v in top_results.items()},
          "gada": {str(k): v for k, v in gada_results.items()}}
    with open("data/analysis/s1_narrow.json", "w", encoding="utf-8") as fh:
        json.dump(out, fh, ensure_ascii=False, indent=2, default=str)

    print("\n=== 最终保留的触发格子（两个 cohort 的并集/交集需要人工决定，先看各自结果）===")
    print("top KEEP:", [k for k, v in top_results.items() if v["keep"]])
    print("gada KEEP:", [k for k, v in gada_results.items() if v["keep"]])


if __name__ == "__main__":
    main()
