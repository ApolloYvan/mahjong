# -*- coding: utf-8 -*-
"""S2（吃碰）+ S3（何时转爆头）+ S5 的庄闲拆分，读 claims.csv / s3_baotou.csv。
与 tools/five_questions.py（S1/S4，读 decisions.csv）分开跑，避免同时对同一
大文件做房间聚类自助法时互相抢 CPU、也方便单独重跑。

用法：
    python3 tools/five_questions_s2s3.py
"""
import json
import os
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.mining_common import mean, two_proportion_z_test, cluster_bootstrap_ci, within_person_delta, bh_reject  # noqa: E402
from tools.five_questions import load_claims, load_baotou, s1_wall_bucket  # noqa: E402
from tools.hypotheses import coverage, rate_comparison, outcome_delta, register_p, PVALUE_REGISTRY  # noqa: E402

PVALUE_REGISTRY.clear()  # 本文件自己的 BH 池，不与 hypotheses.py 主池混用（如实标注见报告）


def jbucket(j):
    j = j or 0
    return "0" if j == 0 else ("1" if j == 1 else "2+")


def sbucket(s):
    if s is None:
        return "NA"
    return "0(tenpai)" if s == 0 else ("1" if s == 1 else "2+")


# ---------------------------------------------------------------------------
# S2: 吃碰
# ---------------------------------------------------------------------------

def s2_analysis(rows):
    def accepted(r):
        return r["act_type"] in ("chi", "peng", "gang")

    # -- 总体接受率：cohort x window_kind --
    overall = defaultdict(lambda: defaultdict(int))
    for r in rows:
        key = (r["cohort"], r["window_kind"])
        overall[key]["n"] += 1
        if accepted(r):
            overall[key]["accept"] += 1
    overall_table = [{"cohort": c, "window_kind": wk, "n": d["n"], "accept": d["accept"],
                      "accept_rate": d["accept"] / d["n"] if d["n"] else None}
                     for (c, wk), d in overall.items()]

    # -- 首副露 (melds_before==0) 分层：cohort x window_kind x jokers --
    def first_meld(r):
        return (r["melds_before"] or 0) == 0

    strata = defaultdict(lambda: defaultdict(int))
    for r in rows:
        if not first_meld(r):
            continue
        key = (r["cohort"], r["window_kind"], jbucket(r["jokers_before"]))
        strata[key]["n"] += 1
        if accepted(r):
            strata[key]["accept"] += 1
    first_meld_table = [{"cohort": c, "window_kind": wk, "jokers": jb, "n": d["n"],
                         "accept": d["accept"], "accept_rate": d["accept"] / d["n"] if d["n"] else None}
                        for (c, wk, jb), d in strata.items()]

    # -- 听牌态 (before_shanten==0) 分层 --
    tenpai_strata = defaultdict(lambda: defaultdict(int))
    for r in rows:
        if r["before_shanten"] != 0:
            continue
        key = (r["cohort"], r["window_kind"])
        tenpai_strata[key]["n"] += 1
        if accepted(r):
            tenpai_strata[key]["accept"] += 1
    tenpai_table = [{"cohort": c, "window_kind": wk, "n": d["n"], "accept": d["accept"],
                     "accept_rate": d["accept"] / d["n"] if d["n"] else None}
                    for (c, wk), d in tenpai_strata.items()]

    # -- 抓打圈内窗口数 --
    catch_windows = sum(1 for r in rows if r["catch_play"])
    catch_accept = sum(1 for r in rows if r["catch_play"] and accepted(r))

    # -- 多种吃法时选哪一种 --
    multi_chi = [r for r in rows if r["window_kind"] == "chi" and (r["n_chi_options"] or 0) >= 2]
    combo_accept = defaultdict(lambda: defaultdict(int))
    for r in multi_chi:
        if r["act_type"] == "chi":
            combo_accept[r["cohort"]][r["act_chi_combo"] or "?"] += 1
    combo_recommend_when_pass = defaultdict(lambda: defaultdict(int))
    for r in multi_chi:
        if r["act_type"] == "pass":
            combo_recommend_when_pass[r["cohort"]][r["act_chi_combo"] or "?"] += 1

    # -- 吃碰之后向听是否下降：accept vs pass 各自的 shanten 分布 --
    shanten_move = defaultdict(lambda: defaultdict(list))
    for r in rows:
        if r["before_shanten"] is None or r["best_after_shanten"] is None:
            continue
        grp = "accept" if accepted(r) else "pass"
        shanten_move[r["cohort"]][grp].append(r["before_shanten"] - r["best_after_shanten"])
    shanten_move_table = {c: {g: mean(vs) for g, vs in d.items()} for c, d in shanten_move.items()}

    # -- H7 核心：首副露 accept vs pass 的 y_score 同人内部 Δ + 70/30 --
    def pop_first(r):
        return first_meld(r) and r["window_kind"] in ("chi", "peng_gang")

    import random
    rooms = sorted(set(r["room_id"] for r in rows))
    rng = random.Random(20260924)
    rng.shuffle(rooms)
    train_rooms = set(rooms[:int(len(rooms) * 0.7)])

    outcome = outcome_delta(rows, pop_first, accepted, lambda r: not accepted(r),
                            "y_score", "S2_first_meld_score")
    train_rows = [r for r in rows if r["room_id"] in train_rooms]
    test_rows = [r for r in rows if r["room_id"] not in train_rooms]
    train_d = outcome_delta(train_rows, pop_first, accepted, lambda r: not accepted(r),
                            "y_score", "S2_first_meld_score_train")
    test_d = outcome_delta(test_rows, pop_first, accepted, lambda r: not accepted(r),
                           "y_score", "S2_first_meld_score_test")
    cov = coverage(rows, pop_first, cohort="ours")
    rate_top_ours = rate_comparison(rows, pop_first, accepted, "top", "ours", "S2_first_meld_rate_top_ours")
    rate_top_bottom = rate_comparison(rows, pop_first, accepted, "top", "bottom", "S2_first_meld_rate_top_bottom")

    # -- 针对性检验：只看 peng_gang（chi 没有明显 gap），分别在 tenpai 态、
    # 首副露态两个人群上单独做 outcome_delta，比笼统的 accept-vs-pass 更贴近
    # 实际候选规则（"tenpai 态该不该更愿意碰/杠"）--
    def pop_tenpai_pg(r):
        return r["window_kind"] == "peng_gang" and r["before_shanten"] == 0

    def pop_firstmeld_pg(r):
        return r["window_kind"] == "peng_gang" and first_meld(r)

    tenpai_pg_outcome = outcome_delta(rows, pop_tenpai_pg, accepted, lambda r: not accepted(r),
                                      "y_score", "S2_tenpai_pg_score")
    tenpai_pg_train = outcome_delta(train_rows, pop_tenpai_pg, accepted, lambda r: not accepted(r),
                                    "y_score", "S2_tenpai_pg_score_train")
    tenpai_pg_test = outcome_delta(test_rows, pop_tenpai_pg, accepted, lambda r: not accepted(r),
                                   "y_score", "S2_tenpai_pg_score_test")
    tenpai_pg_cov = coverage(rows, pop_tenpai_pg, cohort="ours")
    tenpai_pg_rate = rate_comparison(rows, pop_tenpai_pg, accepted, "top", "ours", "S2_tenpai_pg_rate")
    tenpai_pg_rate_bottom = rate_comparison(rows, pop_tenpai_pg, accepted, "top", "bottom", "S2_tenpai_pg_rate_bottom")

    firstmeld_pg_outcome = outcome_delta(rows, pop_firstmeld_pg, accepted, lambda r: not accepted(r),
                                         "y_score", "S2_firstmeld_pg_score")
    firstmeld_pg_train = outcome_delta(train_rows, pop_firstmeld_pg, accepted, lambda r: not accepted(r),
                                       "y_score", "S2_firstmeld_pg_score_train")
    firstmeld_pg_test = outcome_delta(test_rows, pop_firstmeld_pg, accepted, lambda r: not accepted(r),
                                      "y_score", "S2_firstmeld_pg_score_test")
    firstmeld_pg_cov = coverage(rows, pop_firstmeld_pg, cohort="ours")
    firstmeld_pg_rate = rate_comparison(rows, pop_firstmeld_pg, accepted, "top", "ours", "S2_firstmeld_pg_rate")
    firstmeld_pg_rate_bottom = rate_comparison(rows, pop_firstmeld_pg, accepted, "top", "bottom",
                                               "S2_firstmeld_pg_rate_bottom")

    return {
        "overall_table": overall_table,
        "first_meld_table": first_meld_table,
        "tenpai_table": tenpai_table,
        "catch_windows": catch_windows, "catch_accept": catch_accept,
        "multi_chi_n": len(multi_chi),
        "combo_accept_when_chi": {c: dict(d) for c, d in combo_accept.items()},
        "combo_recommend_when_pass": {c: dict(d) for c, d in combo_recommend_when_pass.items()},
        "shanten_move_table": shanten_move_table,
        "h7_outcome": outcome, "h7_train": train_d, "h7_test": test_d,
        "h7_coverage_ours": cov, "h7_rate_top_ours": rate_top_ours, "h7_rate_top_bottom": rate_top_bottom,
        "tenpai_pg_outcome": tenpai_pg_outcome, "tenpai_pg_train": tenpai_pg_train,
        "tenpai_pg_test": tenpai_pg_test, "tenpai_pg_coverage_ours": tenpai_pg_cov,
        "tenpai_pg_rate": tenpai_pg_rate, "tenpai_pg_rate_bottom": tenpai_pg_rate_bottom,
        "firstmeld_pg_outcome": firstmeld_pg_outcome, "firstmeld_pg_train": firstmeld_pg_train,
        "firstmeld_pg_test": firstmeld_pg_test, "firstmeld_pg_coverage_ours": firstmeld_pg_cov,
        "firstmeld_pg_rate": firstmeld_pg_rate, "firstmeld_pg_rate_bottom": firstmeld_pg_rate_bottom,
    }


# ---------------------------------------------------------------------------
# S3: 什么时候转做爆头
# ---------------------------------------------------------------------------

def s3_analysis(rows):
    def pop(r):
        return r["baotou_reachable"] == 1

    population = [r for r in rows if pop(r)]

    strata = defaultdict(lambda: defaultdict(int))
    for r in population:
        key = (r["cohort"], jbucket(r["jokers_before"]), sbucket(r["shanten_before"]),
               bool(int(r["is_dealer"])))
        strata[key]["n"] += 1
        if r["took_baotou"]:
            strata[key]["took"] += 1
    strata_table = [{"cohort": c, "jokers": jb, "shanten": sb, "is_dealer": isd,
                     "n": d["n"], "took": d["took"], "took_rate": d["took"] / d["n"] if d["n"] else None}
                    for (c, jb, sb, isd), d in strata.items()]

    def acc_a(r):
        return r["took_baotou"] == 1

    def acc_b(r):
        return r["took_baotou"] == 0

    outcome = outcome_delta(population, lambda r: True, acc_a, acc_b, "y_score", "S3_take_baotou_score")

    import random
    rooms = sorted(set(r["room_id"] for r in population))
    rng = random.Random(20260924)
    rng.shuffle(rooms)
    train_rooms = set(rooms[:int(len(rooms) * 0.7)])
    train_rows = [r for r in population if r["room_id"] in train_rooms]
    test_rows = [r for r in population if r["room_id"] not in train_rooms]
    train_d = outcome_delta(train_rows, lambda r: True, acc_a, acc_b, "y_score", "S3_take_baotou_score_train")
    test_d = outcome_delta(test_rows, lambda r: True, acc_a, acc_b, "y_score", "S3_take_baotou_score_test")

    rate_top_ours = rate_comparison(rows, pop, acc_a, "top", "ours", "S3_take_rate_top_ours")
    rate_top_bottom = rate_comparison(rows, pop, acc_a, "top", "bottom", "S3_take_rate_top_bottom")
    cov = coverage(rows, pop, cohort="ours")

    return {
        "population_n": len(population), "strata_table": strata_table,
        "outcome": outcome, "train": train_d, "test": test_d,
        "rate_top_ours": rate_top_ours, "rate_top_bottom": rate_top_bottom, "coverage_ours": cov,
    }


def main():
    claims = load_claims()
    baotou = load_baotou()
    print("claims 行数：%d  baotou 行数：%d" % (len(claims), len(baotou)))

    s2 = s2_analysis(claims)
    s3 = s3_analysis(baotou)

    reject = bh_reject([p for _l, p in PVALUE_REGISTRY])
    bh_table = [{"label": l, "p": p, "reject": r} for (l, p), r in zip(PVALUE_REGISTRY, reject)]

    out = {"s2": s2, "s3": s3, "bh_table": bh_table}
    with open("data/analysis/five_questions_s2_s3.json", "w", encoding="utf-8") as fh:
        json.dump(out, fh, ensure_ascii=False, indent=2, default=str)

    print("\n=== S2 总体接受率 ===")
    for r in s2["overall_table"]:
        print(r)
    print("\n=== S2 首副露分层 ===")
    for r in s2["first_meld_table"]:
        if r["n"] >= 10:
            print(r)
    print("\n=== S2 听牌态分层 ===")
    for r in s2["tenpai_table"]:
        print(r)
    print("\n抓打圈窗口数:", s2["catch_windows"], "接受数:", s2["catch_accept"])
    print("\n多吃法样本数:", s2["multi_chi_n"])
    print("接受时选择的组合:", s2["combo_accept_when_chi"])
    print("放弃时模型推荐的组合:", s2["combo_recommend_when_pass"])
    print("\n向听变化(accept vs pass):", s2["shanten_move_table"])
    print("\nH7 outcome:", s2["h7_outcome"])
    print("H7 train:", s2["h7_train"])
    print("H7 test:", s2["h7_test"])
    print("H7 coverage_ours:", s2["h7_coverage_ours"])
    print("H7 rate top/ours:", s2["h7_rate_top_ours"])
    print("H7 rate top/bottom:", s2["h7_rate_top_bottom"])

    print("\n=== S3 population_n ===", s3["population_n"])
    print("=== S3 分层（n>=10）===")
    for r in s3["strata_table"]:
        if r["n"] >= 10:
            print(r)
    print("\nS3 outcome:", s3["outcome"])
    print("S3 train:", s3["train"])
    print("S3 test:", s3["test"])
    print("S3 rate top/ours:", s3["rate_top_ours"])
    print("S3 rate top/bottom:", s3["rate_top_bottom"])
    print("S3 coverage_ours:", s3["coverage_ours"])

    print("\n=== BH 表 ===")
    for r in bh_table:
        print(r)


if __name__ == "__main__":
    main()
