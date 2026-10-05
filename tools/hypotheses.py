# -*- coding: utf-8 -*-
"""对 docs/STRATEGY_MINING.md 第四节 H1-H7、以及追加任务的局面特征假设
H-S1-4，逐条给出「通过 / 否决 / 样本不足」结论。

范围与诚实标注：本文件直接读 data/analysis/decisions.csv 做「分层比较 +
房间聚类自助 CI + BH 校正」，不经过 tools/divergence_mine.py 的通用情境桶
挖掘框架（该文件本轮未建成，见 docs/experiments/OFFLINE_REPORT.md）。每条
假设的分层维度按 docs/STRATEGY_MINING.md 第四节原文的判据直接取，不是自动
分桶降级。这意味着覆盖面比完整管线窄，但每条判据都可审计、可复现。

用法：
    python3 tools/hypotheses.py --csv data/analysis/decisions.csv \
        --out data/analysis/hypotheses.json
"""
import argparse
import json
import os
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.mining_common import (  # noqa: E402
    OUR_UID, bh_reject, cluster_bootstrap_ci, mean, two_proportion_z_test, within_person_delta,
)

NUMERIC_INT = {
    "round_no", "seq", "seat", "is_dealer", "turn_no", "wall_left", "to_dead",
    "shanten_std", "shanten_route", "ukeire", "ukeire_live", "wait_cover",
    "jokers", "pairs", "triplets", "lone", "melds", "chi_used", "chain", "piao_out",
    "joker_total", "opp_melds_max", "opp_melds_sum", "dealer_melds", "catch_play",
    "i_am_exempt", "visible_joker", "sess_score", "sess_rank", "gap_to_1st", "gap_to_4th",
    "rounds_left", "round_idx", "wall_remaining", "gap_to_next_up", "gap_to_next_down",
    "y_hu", "y_fan", "y_score", "y_bt", "y_tenpai_3", "y_opp_hu_4", "auto", "is_baotou",
    "tier_gap",
}
NUMERIC_FLOAT = {"shanten_7p", "to_baotou", "fan_from_score"}


def load_rows(path):
    import csv
    rows = []
    with open(path, encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            for k in NUMERIC_INT:
                v = row.get(k)
                row[k] = int(v) if v not in (None, "") else None
            for k in NUMERIC_FLOAT:
                v = row.get(k)
                row[k] = float(v) if v not in (None, "") else None
            rows.append(row)
    return rows


def coverage(rows, pop_filter, cohort="ours"):
    base = [r for r in rows if r["cohort"] == cohort]
    if not base:
        return None
    hit = sum(1 for r in base if pop_filter(r))
    return hit / len(base)


PVALUE_REGISTRY = []  # [(label, pvalue)] 供最后统一 BH 校正


def register_p(label, p):
    if p is not None:
        PVALUE_REGISTRY.append((label, p))
    return p


def rate_comparison(rows, pop_filter, action_filter, cohort_a, cohort_b, label):
    """两个 cohort 在同一 population 里，action_filter 命中率的两比例 z 检验。"""
    a = [r for r in rows if r["cohort"] == cohort_a and pop_filter(r)]
    b = [r for r in rows if r["cohort"] == cohort_b and pop_filter(r)]
    na, nb = len(a), len(b)
    xa = sum(1 for r in a if action_filter(r))
    xb = sum(1 for r in b if action_filter(r))
    z, p = two_proportion_z_test(xa, na, xb, nb)
    register_p(label, p)
    return {
        "label": label, "n_a": na, "x_a": xa, "rate_a": (xa / na if na else None),
        "n_b": nb, "x_b": xb, "rate_b": (xb / nb if nb else None), "z": z, "p": p,
    }


def outcome_delta(rows, pop_filter, group_a_filter, group_b_filter, value_key, label,
                  weighting="within_person"):
    pop = [r for r in rows if pop_filter(r)]

    def group_of(r):
        if group_a_filter(r):
            return "A"
        if group_b_filter(r):
            return "B"
        return None

    if weighting == "within_person":
        delta, per_uid = within_person_delta(
            pop, lambda r: r["uid"], group_of, lambda r: r[value_key] or 0, "A", "B")
    else:
        a_vals = [r[value_key] or 0 for r in pop if group_a_filter(r)]
        b_vals = [r[value_key] or 0 for r in pop if group_b_filter(r)]
        delta = (mean(a_vals) - mean(b_vals)) if a_vals and b_vals else None
        per_uid = []

    def stat_fn(sample_rows):
        sub = [r for r in sample_rows if pop_filter(r)]
        if weighting == "within_person":
            d, _ = within_person_delta(sub, lambda r: r["uid"], group_of, lambda r: r[value_key] or 0, "A", "B")
            return d
        a_vals = [r[value_key] or 0 for r in sub if group_a_filter(r)]
        b_vals = [r[value_key] or 0 for r in sub if group_b_filter(r)]
        return (mean(a_vals) - mean(b_vals)) if a_vals and b_vals else None

    point, lo, hi = cluster_bootstrap_ci(rows, lambda r: r["room_id"], stat_fn, n_boot=1000)
    n_uids_consistent_positive = sum(1 for _uid, d, _na, _nb in per_uid if d > 0)
    n_uids_consistent_negative = sum(1 for _uid, d, _na, _nb in per_uid if d < 0)
    return {
        "label": label, "delta": delta, "ci_lo": lo, "ci_hi": hi,
        "n_people": len(per_uid),
        "n_people_positive": n_uids_consistent_positive,
        "n_people_negative": n_uids_consistent_negative,
        "per_person": per_uid,
    }


# ---------------------------------------------------------------------------
# H1: 打财神控场（非爆头态主动打白）
# ---------------------------------------------------------------------------

def h1(rows):
    def non_bt_white(r):
        return r["dtype"] == "discard" and r["act_tile"] == "白" and r["is_baotou"] == 0

    all_white_discards = [r for r in rows if r["dtype"] == "discard" and r["act_tile"] == "白"]
    n_total_white = len(all_white_discards)
    n_non_bt_white = sum(1 for r in all_white_discards if r["is_baotou"] == 0)
    mechanism_hits = sum(1 for r in all_white_discards if r["is_baotou"] == 0 and r["catch_play"] == 1)

    def pop_opp_pressure(r):
        return r["dtype"] == "discard" and (
            (r["opp_melds_max"] or 0) >= 2 or ((r["dealer_melds"] or 0) >= 2 and r["is_dealer"] == 0))

    rate = rate_comparison(rows, pop_opp_pressure, non_bt_white, "top", "ours", "H1_rate_top_vs_ours")
    outcome = outcome_delta(rows, pop_opp_pressure, non_bt_white,
                            lambda r: not non_bt_white(r), "y_opp_hu_4", "H1_y_opp_hu_4_delta")
    cov = coverage(rows, pop_opp_pressure)
    verdict = "样本不足"
    if rate["n_a"] >= 30 and rate["n_b"] >= 30 and outcome["n_people"] >= 2:
        if rate["p"] is not None and rate["rate_a"] is not None and rate["rate_b"] is not None:
            direction_ok = rate["rate_a"] > rate["rate_b"]
            outcome_ok = outcome["ci_hi"] is not None and outcome["ci_hi"] < 0  # y_opp_hu_4 应更低
            verdict = "通过" if (direction_ok and outcome_ok) else "否决"
    return {
        "id": "H1", "name": "打财神控场（非爆头态主动打白）",
        "n_total_white_discards": n_total_white, "n_non_baotou_white": n_non_bt_white,
        "mechanism_check_catch_play_true": mechanism_hits,
        "mechanism_check_rate": (mechanism_hits / n_non_bt_white) if n_non_bt_white else None,
        "coverage_ours": cov, "rate_comparison": rate, "outcome_delta": outcome,
        "expected_gain": (outcome["delta"] * cov) if (outcome["delta"] is not None and cov) else None,
        "verdict": verdict,
    }


# ---------------------------------------------------------------------------
# H4: 爆头摸白板，胡还是飘（情境化，同人内部 Δ = E[y|飘] - E[y|胡]）
# ---------------------------------------------------------------------------

def h4(rows):
    def pop(r):
        return r["dtype"] == "hu_or_not" and r["act_tile"] == "白" and r["is_baotou"] == 1

    layers = defaultdict(list)
    for r in rows:
        if pop(r):
            wall_seg = "high" if (r["wall_left"] or 0) > 60 else ("mid" if (r["wall_left"] or 0) > 40 else "low")
            layers[(bool(int(r["is_dealer"])), wall_seg)].append(r)

    layer_results = []
    for key, layer_rows in layers.items():
        is_dealer, wall_seg = key
        d = outcome_delta(layer_rows, lambda r: True,
                          lambda r: r["act_type"] == "decline_hu",
                          lambda r: r["act_type"] == "hu",
                          "y_score", "H4_layer_%s_%s" % (is_dealer, wall_seg))
        layer_results.append({"is_dealer": is_dealer, "wall_seg": wall_seg, "n": len(layer_rows), **d})

    n_pop = sum(1 for r in rows if pop(r))
    any_pass = any(l["n"] >= 30 and l["n_people"] >= 2 and l["ci_lo"] is not None and l["ci_lo"] > 0
                   for l in layer_results)
    any_fail_sig = any(l["n"] >= 30 and l["n_people"] >= 2 and l["ci_hi"] is not None and l["ci_hi"] < 0
                       for l in layer_results)
    verdict = "样本不足"
    if n_pop >= 30:
        verdict = "通过" if any_pass else ("否决" if any_fail_sig else "否决")
    return {"id": "H4", "name": "爆头摸白板：胡还是飘（情境化）", "n_population": n_pop,
            "layers": layer_results, "verdict": verdict}


# ---------------------------------------------------------------------------
# H5: 弃胡（非爆头可胡时放弃）
# ---------------------------------------------------------------------------

def h5(rows):
    def pop(r):
        return r["dtype"] == "hu_or_not" and r["is_baotou"] == 0

    top_pop = [r for r in rows if pop(r) and r["cohort"] == "top"]
    n_top = len(top_pop)
    n_decline = sum(1 for r in top_pop if r["act_type"] == "decline_hu")
    decline_rate = (n_decline / n_top) if n_top else None
    # 存在性检验：弃胡率是否显著 >0（对 0 做单侧 z 近似：用 x/n 与 se 比较）
    sig_gt_zero = False
    if n_top >= 30 and n_decline > 0:
        import math
        p = n_decline / n_top
        se = math.sqrt(p * (1 - p) / n_top) if 0 < p < 1 else 0
        sig_gt_zero = se > 0 and (p / se) > 1.645  # 单侧 5%

    outcome = None
    verdict = "样本不足"
    if n_top < 30:
        verdict = "样本不足"
    elif not sig_gt_zero:
        verdict = "否决"
    else:
        outcome = outcome_delta(rows, pop, lambda r: r["act_type"] == "decline_hu",
                                lambda r: r["act_type"] == "hu", "y_fan", "H5_fan_delta")
        verdict = "通过" if (outcome["ci_lo"] is not None and outcome["ci_lo"] > 0) else "否决"
    return {"id": "H5", "name": "弃胡（非爆头可胡时放弃）", "n_top_population": n_top,
            "n_decline": n_decline, "decline_rate": decline_rate,
            "significantly_above_zero": sig_gt_zero, "outcome_delta": outcome, "verdict": verdict}


# ---------------------------------------------------------------------------
# H2/H3/H6/H7：existence-level 描述性检验（未做完整分层，样本/时间限制内的
# 最直接可算版本；结论只在数据支持时给"通过"，否则"样本不足"或"否决"）。
# ---------------------------------------------------------------------------

def h2(rows):
    def by_role(cohort, is_dealer):
        pop = [r for r in rows if r["cohort"] == cohort and r["dtype"] == "discard"
              and bool(int(r["is_dealer"])) == is_dealer]
        fans = [r["y_fan"] for r in pop if r["y_hu"] == 1 and r["y_fan"] is not None]
        return {"n": len(pop), "mean_fan_given_hu": mean(fans), "n_hu": len(fans)}

    top_dealer, top_x = by_role("top", True), by_role("top", False)
    ours_dealer, ours_x = by_role("ours", True), by_role("ours", False)
    verdict = "样本不足"
    if top_dealer["n"] >= 60 and top_x["n"] >= 60 and ours_dealer["n"] >= 60 and ours_x["n"] >= 60:
        top_gap = (top_dealer["mean_fan_given_hu"] or 0) - (top_x["mean_fan_given_hu"] or 0)
        ours_gap = (ours_dealer["mean_fan_given_hu"] or 0) - (ours_x["mean_fan_given_hu"] or 0)
        verdict = "通过（描述性，未做显著性分层）" if abs(top_gap - ours_gap) > 0.2 else "否决"
    return {"id": "H2", "name": "庄闲分离", "top_dealer": top_dealer, "top_idle": top_x,
            "ours_dealer": ours_dealer, "ours_idle": ours_x, "verdict": verdict,
            "note": "轻量版：只比番/胡的庄闲差距，未做速度分层与显著性检验——时间受限的既有局限"}


def h3(rows):
    def pop(cohort, deep):
        return [r for r in rows if r["cohort"] == cohort and r["dtype"] == "discard"
               and ((r["to_dead"] or 99) < 15) == deep]

    segs = {}
    for cohort in ("top", "ours"):
        near = pop(cohort, True)
        far = pop(cohort, False)
        segs[cohort] = {
            "near_dead_n": len(near), "far_n": len(far),
            "near_dead_hu_rate": mean([r["y_hu"] for r in near]) if near else None,
            "far_hu_rate": mean([r["y_hu"] for r in far]) if far else None,
        }
    verdict = "样本不足"
    if segs["top"]["near_dead_n"] >= 30 and segs["ours"]["near_dead_n"] >= 30:
        verdict = "通过（描述性）" if (
            (segs["top"]["near_dead_hu_rate"] or 0) - (segs["top"]["far_hu_rate"] or 0)
            > (segs["ours"]["near_dead_hu_rate"] or 0) - (segs["ours"]["far_hu_rate"] or 0) + 0.02
        ) else "否决"
    return {"id": "H3", "name": "最后 10 墩提前量", "segments": segs, "verdict": verdict,
            "note": "轻量版：只看 to_dead<15 前后胡牌率跳升幅度，未做完整切换点搜索"}


def h6(rows):
    def pop(cohort, behind_late):
        return [r for r in rows if r["cohort"] == cohort and r["dtype"] == "discard"
               and ((r["sess_rank"] or 0) >= 3 and (r["rounds_left"] or 99) <= 2) == behind_late]

    segs = {}
    for cohort in ("top", "ours"):
        behind = pop(cohort, True)
        other = pop(cohort, False)
        segs[cohort] = {
            "behind_late_n": len(behind), "other_n": len(other),
            "behind_late_piao_rate": mean([1 if r["act_tile"] == "白" and r["is_baotou"] == 1 else 0
                                          for r in behind]) if behind else None,
        }
    verdict = "样本不足"
    if segs["top"]["behind_late_n"] >= 20:
        verdict = "样本不足（人工判定阈值过高，未做显著性检验——如实标注）"
    return {"id": "H6", "name": "领先落后切换", "segments": segs, "verdict": verdict,
            "note": "STRATEGY_MINING.md 已标注本假设优先级最低（晋级看总分，方差策略价值小），"
                    "本轮只做存在性统计，未做完整假设检验"}


def h7(rows):
    def first_meld_pop(cohort):
        return [r for r in rows if r["cohort"] == cohort and r["dtype"] == "discard" and r["melds"] == 1]

    top_n = len(first_meld_pop("top"))
    ours_n = len(first_meld_pop("ours"))
    verdict = "样本不足" if min(top_n, ours_n) < 30 else "未实现claim决策点，无法判定"
    return {"id": "H7", "name": "首副露的选择性", "verdict": verdict,
            "note": "H7 依赖 claim（吃碰响应窗口）决策点，本轮 decision_table.py 未实现 "
                    "claim 类型（见 OFFLINE_REPORT.md「范围与局限」），无法计算首副露吃碰率与"
                    "吃碰后 wait_cover 增量，如实标记为未完成而非否决"}


# ---------------------------------------------------------------------------
# 追加任务 H-S1..H-S4：局面特征（分差 x 剩余局数 x 牌墙）
# ---------------------------------------------------------------------------

def _bucket_hs(r):
    rank = r["sess_rank"] or 0
    rank_b = "1" if rank == 1 else ("2-3" if rank in (2, 3) else "4")
    rl = r["rounds_left"] if r["rounds_left"] is not None else 99
    rl_b = "0-1" if rl <= 1 else ("2-3" if rl <= 3 else "4+")
    wall = r["wall_left"] or 0
    wall_b = ">40" if wall > 40 else ("21-40" if wall > 20 else "<=20")
    return (rank_b, rl_b, wall_b)


def h_s1(rows):
    """落后且剩余局数少时，小番自摸接受率是否更低（有胡不胡去追高番）。"""
    def pop(r):
        rank_b, rl_b, _wall_b = _bucket_hs(r)
        return (r["dtype"] == "hu_or_not" and rank_b in ("2-3", "4") and rl_b in ("0-1", "2-3"))

    rate = rate_comparison(rows, pop, lambda r: r["act_type"] == "decline_hu", "top", "ours", "HS1_rate")
    cov = coverage(rows, pop)
    n_min = min(rate["n_a"], rate["n_b"])
    verdict = "样本不足" if n_min < 30 else (
        "通过" if (rate["p"] is not None and rate["p"] < 0.05
                  and (rate["rate_a"] or 0) > (rate["rate_b"] or 0)) else "不显著")
    return {"id": "H-S1", "name": "落后+局少：小番接受率更低",
            "coverage_ours": cov, "rate_comparison": rate, "verdict": verdict}


def h_s2(rows):
    """落后/末局：追爆头/留白/放弃吃碰求番比例更高；领先：更早胡/更多吃碰求速度。"""
    def pop_behind(r):
        rank_b, rl_b, _ = _bucket_hs(r)
        return r["dtype"] == "discard" and rank_b in ("2-3", "4") and rl_b in ("0-1", "2-3")

    def pop_ahead(r):
        rank_b, _rl_b, _ = _bucket_hs(r)
        return r["dtype"] == "discard" and rank_b == "1"

    def piao_or_hold(r):
        return r["act_tile"] == "白"

    behind_rate = rate_comparison(rows, pop_behind, piao_or_hold, "top", "ours", "HS2_behind_rate")
    ahead_rate = rate_comparison(rows, pop_ahead, piao_or_hold, "top", "ours", "HS2_ahead_rate")
    n_min = min(behind_rate["n_a"], behind_rate["n_b"], ahead_rate["n_a"], ahead_rate["n_b"])
    verdict = "样本不足" if n_min < 30 else (
        "通过" if (behind_rate["p"] is not None and behind_rate["p"] < 0.05
                  and (behind_rate["rate_a"] or 0) > (behind_rate["rate_b"] or 0)) else "不显著")
    return {"id": "H-S2", "name": "落后/领先的攻守切换（留白/求速度）",
            "behind_rate": behind_rate, "ahead_rate": ahead_rate, "verdict": verdict}


def h_s3(rows):
    """牌墙余量少时，高手从「番数潜力」切到「听牌速度」的时点与我们不同。"""
    def switch_point(cohort_rows):
        # 用 to_baotou 是否 None（放弃留白倾向的粗代理：留白路线放弃 = 手上无财神
        # 或已不再是"以爆头为目标"）——受限于时间，只做粗粒度分段对比。
        by_wall = defaultdict(list)
        for r in cohort_rows:
            if r["dtype"] != "discard":
                continue
            wall_seg = "high" if (r["wall_left"] or 0) > 40 else ("mid" if (r["wall_left"] or 0) > 20 else "low")
            by_wall[wall_seg].append(r)
        out = {}
        for seg, rs in by_wall.items():
            piao_rate = mean([1 if r["act_tile"] == "白" and r["is_baotou"] == 1 else 0 for r in rs]) if rs else None
            out[seg] = {"n": len(rs), "piao_rate": piao_rate}
        return out

    top_rows = [r for r in rows if r["cohort"] == "top"]
    ours_rows = [r for r in rows if r["cohort"] == "ours"]
    top_seg = switch_point(top_rows)
    ours_seg = switch_point(ours_rows)
    n_min = min((top_seg.get(s, {}).get("n", 0) for s in ("high", "mid", "low")), default=0)
    verdict = "样本不足" if n_min < 30 else "描述性（未做显著性检验，时间受限）"
    return {"id": "H-S3", "name": "牌墙余量与番数/速度切换点", "top": top_seg, "ours": ours_seg,
            "verdict": verdict}


def h_s4(rows):
    """H-S1/H-S2/H-S3 的行为差异是否对应更高 分/局（不能只看"他们这么做"）。"""
    def pop(r):
        rank_b, rl_b, _ = _bucket_hs(r)
        return r["dtype"] == "hu_or_not" and rank_b in ("2-3", "4") and rl_b in ("0-1", "2-3")

    outcome = outcome_delta(rows, pop, lambda r: r["act_type"] == "decline_hu",
                            lambda r: r["act_type"] == "hu", "y_score", "HS4_score_delta")
    verdict = "样本不足"
    if outcome["n_people"] >= 2:
        verdict = "正向且显著" if (outcome["ci_lo"] is not None and outcome["ci_lo"] > 0) else (
            "负向且显著" if (outcome["ci_hi"] is not None and outcome["ci_hi"] < 0) else "不显著")
    return {"id": "H-S4", "name": "落后+局少时「不接受小番」是否真的赚分", "outcome_delta": outcome,
            "verdict": verdict}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", default="data/analysis/decisions.csv")
    ap.add_argument("--out", default="data/analysis/hypotheses.json")
    args = ap.parse_args()

    rows = load_rows(args.csv)
    print("载入决策行数：%d" % len(rows))

    results = {}
    for fn in (h1, h4, h5, h2, h3, h6, h7, h_s1, h_s2, h_s3, h_s4):
        results[fn.__name__] = fn(rows)

    reject = bh_reject([p for _label, p in PVALUE_REGISTRY], alpha=0.05)
    bh_table = [{"label": label, "p": p, "bh_reject": rej}
               for (label, p), rej in zip(PVALUE_REGISTRY, reject)]

    out = {"n_rows": len(rows), "results": results, "bh_table": bh_table}
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(out, fh, ensure_ascii=False, indent=2, default=str)
    print("已写出 %s" % args.out)
    for key, r in results.items():
        print("%-6s %-30s verdict=%s" % (r.get("id"), r.get("name"), r.get("verdict")))


if __name__ == "__main__":
    main()
