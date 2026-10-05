# -*- coding: utf-8 -*-
"""收敛任务 S1-S5：五个决策点的高手 vs 我们对照分析。

复用 data/analysis/decisions.csv（discard/hu_or_not，上一轮全量重放产物）与
本轮新增的 data/analysis/claims.csv / data/analysis/s3_baotou.csv（见
tools/mine_claims_baotou.py）。统一走 tools/mining_common.py 的统计原语
（cluster_bootstrap_ci、within_person_delta、two_proportion_z_test、bh_reject），
不重新发明统计口径。

每条结论按任务要求的统一格式输出（见 docs/experiments/OFFLINE_REPORT.md
「五问结论」一节，本文件只负责算数，落地判断和开关代码在别处）。

用法：
    python3 tools/five_questions.py > data/analysis/five_questions_dump.txt
"""
import csv
import json
import os
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.mining_common import (  # noqa: E402
    OUR_UID, bh_reject, cluster_bootstrap_ci, mean, two_proportion_z_test, within_person_delta,
)
from tools.hypotheses import load_rows, coverage, register_p, rate_comparison, outcome_delta, PVALUE_REGISTRY  # noqa: E402

DECISIONS_CSV = "data/analysis/decisions.csv"
CLAIMS_CSV = "data/analysis/claims.csv"
BAOTOU_CSV = "data/analysis/s3_baotou.csv"


def load_claims(path=CLAIMS_CSV):
    numeric_int = {"round_no", "seq", "seat", "is_dealer", "turn_no", "wall_left",
                   "melds_before", "jokers_before", "n_chi_options", "can_peng", "can_gang",
                   "catch_play", "before_shanten", "before_ukeire", "best_after_shanten",
                   "best_after_ukeire", "best_after_baotou", "y_hu"}
    rows = []
    with open(path, encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            for k in numeric_int:
                v = row.get(k)
                row[k] = int(v) if v not in (None, "") else None
            v = row.get("y_score")
            row["y_score"] = int(v) if v not in (None, "") else None
            v = row.get("gate_allowed")
            row["gate_allowed"] = (v == "True") if v in ("True", "False") else None
            rows.append(row)
    return rows


def load_baotou(path=BAOTOU_CSV):
    numeric_int = {"round_no", "seq", "seat", "is_dealer", "turn_no", "wall_left", "melds",
                   "jokers_before", "shanten_before", "baotou_reachable", "took_baotou",
                   "forwent_baotou", "y_hu"}
    rows = []
    with open(path, encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            for k in numeric_int:
                v = row.get(k)
                row[k] = int(v) if v not in (None, "") else None
            v = row.get("y_score")
            row["y_score"] = int(v) if v not in (None, "") else None
            rows.append(row)
    return rows


# ---------------------------------------------------------------------------
# S1: 胡还是不胡（非爆头能胡时）
# ---------------------------------------------------------------------------

def s1_join_next_discard(rows):
    """给每条 hu_or_not 行挂上「同座位同局下一行」（应为其后立即的 discard 行）。
    P3 安全：只用同一局内已经发生的事件顺序，不引入未来信息之外的东西——
    这条"下一行"本身就是该决策之后紧接着发生的真实动作，不是特征泄漏。"""
    by_group = defaultdict(list)
    for i, r in enumerate(rows):
        by_group[(r["room_id"], r["game_id"], r["round_no"], r["seat"])].append(i)
    next_of = {}
    for key, idxs in by_group.items():
        idxs.sort(key=lambda i: int(rows[i]["seq"]))
        for pos, i in enumerate(idxs):
            if pos + 1 < len(idxs):
                nxt = rows[idxs[pos + 1]]
                if nxt["dtype"] == "discard":
                    next_of[i] = idxs[pos + 1]
    return next_of


def s1_wall_bucket(wall_left):
    wall_left = wall_left or 0
    if wall_left > 40:
        return ">40"
    if wall_left > 20:
        return "21-40"
    return "<=20"


def s1_analysis(rows):
    idx_of_row = {id(r): i for i, r in enumerate(rows)}
    next_of = s1_join_next_discard(rows)

    def pop(r):
        return r["dtype"] == "hu_or_not" and r["is_baotou"] == 0 and r["auto"] == 0

    population = [r for r in rows if pop(r)]
    by_idx = {i: r for i, r in enumerate(rows)}

    # -- 分层：drawn 白 x jokers 分档 x 是否庄 x wall 段 --
    def joker_bucket(j):
        j = j or 0
        return "0" if j == 0 else ("1" if j == 1 else "2+")

    strata = defaultdict(lambda: defaultdict(int))
    for i, r in enumerate(rows):
        if not pop(r):
            continue
        key = (r["cohort"], r["act_tile"] == "白", joker_bucket(r["jokers"]),
               bool(int(r["is_dealer"])), s1_wall_bucket(r["wall_left"]))
        strata[key]["n"] += 1
        if r["act_type"] == "decline_hu":
            strata[key]["decline"] += 1

    strata_table = []
    for key, d in strata.items():
        cohort, drew_joker, jbucket, is_dealer, wall_b = key
        strata_table.append({
            "cohort": cohort, "drew_joker": drew_joker, "jokers": jbucket,
            "is_dealer": is_dealer, "wall": wall_b, "n": d["n"], "decline": d["decline"],
            "decline_rate": d["decline"] / d["n"] if d["n"] else None,
        })

    # -- decline_hu 按后续弃牌分类 a/b/c --
    def classify(i):
        r = rows[i]
        j = next_of.get(i)
        if j is None:
            return None
        nxt = rows[j]
        if nxt["act_tile"] == "白":
            return "a_joker"
        if nxt["act_tile"] == r["act_tile"]:
            return "b_tsumogiri_nonjoker"
        return "c_other"

    class_stats = defaultdict(lambda: {"n": 0, "post_baotou": 0, "y_hu": [], "y_fan": [], "y_score": []})
    unlinked = 0
    for i, r in enumerate(rows):
        if not pop(r) or r["act_type"] != "decline_hu":
            continue
        cls = classify(i)
        if cls is None:
            unlinked += 1
            continue
        j = next_of[i]
        nxt = rows[j]
        cs = class_stats[cls]
        cs["n"] += 1
        cs["post_baotou"] += int(nxt["is_baotou"])
        cs["y_hu"].append(r["y_hu"])
        cs["y_fan"].append(r["y_fan"])
        cs["y_score"].append(r["y_score"])

    class_table = {}
    for cls, cs in class_stats.items():
        n = cs["n"]
        class_table[cls] = {
            "n": n, "post_baotou_rate": cs["post_baotou"] / n if n else None,
            "mean_y_hu": mean(cs["y_hu"]), "mean_y_fan": mean(cs["y_fan"]), "mean_y_score": mean(cs["y_score"]),
        }

    # -- H5 85/95: 找 outcome_delta 用的 population，识别参与 within_person 的 uid --
    def pop_h5(r):
        return r["dtype"] == "hu_or_not" and r["is_baotou"] == 0

    h5_pop = [r for r in rows if pop_h5(r)]
    delta, per_uid = within_person_delta(
        h5_pop, lambda r: r["uid"], lambda r: r["act_type"], lambda r: r["y_fan"] or 0,
        "decline_hu", "hu")
    uid_cohort = {}
    for r in rows:
        uid_cohort.setdefault(r["uid"], r["cohort"])
    cohort_counts = defaultdict(int)
    positive_by_cohort = defaultdict(int)
    top_uid_directions = []
    for uid, d, na, nb in per_uid:
        c = uid_cohort.get(uid, "other")
        cohort_counts[c] += 1
        if d > 0:
            positive_by_cohort[c] += 1
        if c == "top":
            top_uid_directions.append((uid, d, na, nb))

    # -- 粗分层（去掉 dealer/wall，只按 drew_joker x jokers 桶，样本更大）--
    coarse = defaultdict(lambda: defaultdict(int))
    for r in rows:
        if not pop(r):
            continue
        key = (r["cohort"], r["act_tile"] == "白", joker_bucket(r["jokers"]))
        coarse[key]["n"] += 1
        if r["act_type"] == "decline_hu":
            coarse[key]["decline"] += 1
    coarse_table = []
    for key, d in coarse.items():
        cohort, drew_joker, jbucket = key
        coarse_table.append({
            "cohort": cohort, "drew_joker": drew_joker, "jokers": jbucket,
            "n": d["n"], "decline": d["decline"],
            "decline_rate": d["decline"] / d["n"] if d["n"] else None,
        })

    # -- 候选规则条件的 outcome_delta（y_score，同人内部）+ 70/30 房间留出复现 --
    def rule_a(r):   # 现行开关口径
        return (r["jokers"] or 0) >= 2

    def rule_b(r):   # 广口径：pre-existing>=2，或本次摸到白
        return (r["jokers"] or 0) >= 2 or r["act_tile"] == "白"

    def rule_c(r):   # 更广：pre-existing>=1，或本次摸到白
        return (r["jokers"] or 0) >= 1 or r["act_tile"] == "白"

    def build_room_split(all_rows, frac=0.7, seed=20260924):
        import random
        rooms = sorted(set(r["room_id"] for r in all_rows))
        rng = random.Random(seed)
        rng.shuffle(rooms)
        cut = int(len(rooms) * frac)
        train_rooms = set(rooms[:cut])
        return train_rooms

    train_rooms = build_room_split(rows)

    rule_results = {}
    for name, cond in (("A_jokers>=2", rule_a), ("B_jokers>=2_or_drew_white", rule_b),
                       ("C_jokers>=1_or_drew_white", rule_c)):
        def pop_rule(r, cond=cond):
            return pop(r) and cond(r)

        overall = outcome_delta(rows, pop_rule, lambda r: r["act_type"] == "decline_hu",
                                lambda r: r["act_type"] == "hu", "y_score", "S1_%s_score" % name)
        train_rows = [r for r in rows if r["room_id"] in train_rooms]
        test_rows = [r for r in rows if r["room_id"] not in train_rooms]
        train_d = outcome_delta(train_rows, pop_rule, lambda r: r["act_type"] == "decline_hu",
                                lambda r: r["act_type"] == "hu", "y_score", "S1_%s_train" % name)
        test_d = outcome_delta(test_rows, pop_rule, lambda r: r["act_type"] == "decline_hu",
                               lambda r: r["act_type"] == "hu", "y_score", "S1_%s_test" % name)
        cov = coverage(rows, pop_rule, cohort="ours")
        top_rate = rate_comparison(rows, pop_rule, lambda r: r["act_type"] == "decline_hu",
                                   "top", "ours", "S1_%s_rate" % name)
        bottom_rate = rate_comparison(rows, pop_rule, lambda r: r["act_type"] == "decline_hu",
                                      "top", "bottom", "S1_%s_rate_vs_bottom" % name)
        rule_results[name] = {
            "overall_delta": overall, "train_delta": train_d, "test_delta": test_d,
            "coverage_ours": cov, "rate_top_vs_ours": top_rate, "rate_top_vs_bottom": bottom_rate,
        }

    return {
        "population_n": len(population),
        "strata_table": strata_table,
        "coarse_table": coarse_table,
        "class_table": class_table,
        "unlinked_decline_rows": unlinked,
        "rule_results": rule_results,
        "h5_person_pool": {
            "n_people": len(per_uid),
            "n_positive_total": sum(1 for _u, d, _na, _nb in per_uid if d > 0),
            "cohort_counts": dict(cohort_counts), "positive_by_cohort": dict(positive_by_cohort),
            "top_cohort_people": top_uid_directions,
            "top_cohort_n": len(top_uid_directions),
            "top_cohort_n_positive": sum(1 for _u, d, _na, _nb in top_uid_directions if d > 0),
        },
    }


# ---------------------------------------------------------------------------
# S4: 爆头听牌摸到白板：胡还是飘（全体玩家）
# ---------------------------------------------------------------------------

def s4_analysis(rows):
    def pop(r):
        return r["dtype"] == "hu_or_not" and r["act_tile"] == "白" and r["is_baotou"] == 1 and r["auto"] == 0

    population = [r for r in rows if pop(r)]

    def piao_count_bucket(r):
        # 用 piao_out（已飘出白板数）近似"已飘次数"
        p = r["piao_out"] or 0
        return "0" if p == 0 else ("1" if p == 1 else "2+")

    def wall_bucket(r):
        return s1_wall_bucket(r["wall_left"])

    layers = defaultdict(list)
    for r in population:
        key = (piao_count_bucket(r), wall_bucket(r), bool(int(r["is_dealer"])))
        layers[key].append(r)

    layer_table = []
    for key, lrows in layers.items():
        piao_b, wall_b, is_dealer = key
        n = len(lrows)
        piao_rate = mean([1 if r["act_type"] == "decline_hu" else 0 for r in lrows])
        d = outcome_delta(lrows, lambda r: True,
                          lambda r: r["act_type"] == "decline_hu",
                          lambda r: r["act_type"] == "hu",
                          "y_score", "S4_layer_%s_%s_%s" % key, weighting="within_person")
        layer_table.append({
            "piao_out": piao_b, "wall": wall_b, "is_dealer": is_dealer, "n": n,
            "piao_rate": piao_rate, "delta_y_score": d["delta"], "ci_lo": d["ci_lo"], "ci_hi": d["ci_hi"],
            "n_people": d["n_people"],
        })

    return {"population_n": len(population), "layer_table": layer_table}


def main():
    rows = load_rows(DECISIONS_CSV)
    print("decisions.csv 行数：%d" % len(rows))
    s1 = s1_analysis(rows)
    s4 = s4_analysis(rows)

    out = {"s1": s1, "s4": s4}
    with open("data/analysis/five_questions_s1_s4.json", "w", encoding="utf-8") as fh:
        json.dump(out, fh, ensure_ascii=False, indent=2, default=str)

    print("\n=== S1 分层表（节选，仅 cohort in top/ours）===")
    for row in s1["strata_table"]:
        if row["cohort"] in ("top", "ours") and row["n"] >= 15:
            print(row)

    print("\n=== S1 粗分层（drew_joker x jokers，跨庄闲/牌墙）===")
    for row in s1["coarse_table"]:
        if row["cohort"] in ("top", "ours", "bottom") and row["n"] >= 10:
            print(row)

    print("\n=== S1 候选规则对比 ===")
    print(json.dumps(s1["rule_results"], ensure_ascii=False, indent=2, default=str))

    print("\n=== S1 decline_hu 后续弃牌分类 ===")
    for cls, d in s1["class_table"].items():
        print(cls, d)
    print("unlinked:", s1["unlinked_decline_rows"])

    print("\n=== H5 85/95 身份 ===")
    print(s1["h5_person_pool"])

    print("\n=== S4 分层表 ===")
    for row in s4["layer_table"]:
        print(row)


if __name__ == "__main__":
    main()
