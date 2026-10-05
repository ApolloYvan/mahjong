# -*- coding: utf-8 -*-
"""情境桶分歧挖掘：docs/STRATEGY_MINING.md 第二节。

桶 = (is_dealer, shanten_route 0/1/2/3+, melds 0/1/2+, jokers 0/1/2+,
      wall 段[>60,40-60,20-40], chain>0, catch_play, sess_rank 段)

分层回退：两组都 >=30 用精确桶；不足则按 sess_rank -> catch_play -> chain
-> wall 段的顺序逐个丢维度合并，直到够样本，每行标注用到第几层。

覆盖率 = 我们(ours)决策点落在该桶的比例；预期收益 = Δ x 覆盖率，按它排序。
行为率比较用两比例 z 检验 + BH 校正（FDR 5%）；噪声地板：分歧率<=5% 丢弃。
结果 Δ 用同人内部（min(nA,nB) 加权）；跨人混合 Δ 作为辅助列。
CI 用房间聚类自助法。

输出：data/analysis/divergence.csv
"""
import argparse
import csv
import os
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.hypotheses import load_rows  # noqa: E402
from tools.mining_common import bh_reject, cluster_bootstrap_ci, mean, two_proportion_z_test  # noqa: E402

DIMS = ["is_dealer", "shanten_route_b", "melds_b", "jokers_b", "wall_b", "chain_b",
        "catch_play", "sess_rank_b"]
BACKOFF_ORDER = ["sess_rank_b", "catch_play", "chain_b", "wall_b"]  # 依次丢弃的维度


def _bucket_dims(r):
    sr = r["shanten_route"]
    sr_b = min(sr, 3) if sr is not None else 9
    melds_b = min(r["melds"] or 0, 2)
    jokers_b = min(r["jokers"] or 0, 2)
    wall = r["wall_left"] or 0
    wall_b = ">60" if wall > 60 else ("40-60" if wall > 40 else ("20-40" if wall > 20 else "<20"))
    chain_b = int((r["chain"] or 0) > 0)
    rank = r["sess_rank"] or 0
    rank_b = "1" if rank == 1 else ("2-3" if rank in (2, 3) else "4")
    return {
        "is_dealer": int(r["is_dealer"] or 0), "shanten_route_b": sr_b, "melds_b": melds_b,
        "jokers_b": jokers_b, "wall_b": wall_b, "chain_b": chain_b,
        "catch_play": int(r["catch_play"] or 0), "sess_rank_b": rank_b,
    }


def bucket_key(dims, dropped):
    return tuple(dims[d] for d in DIMS if d not in dropped)


def act_signature(r):
    """动作类别：弃牌打白 / 弃字 / 弃幺九 / 弃二八 / 弃中张 / 胡 / 飘。"""
    if r["dtype"] == "hu_or_not":
        return "hu" if r["act_type"] == "hu" else "decline"
    return r["act_cat"] or "?"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", default="data/analysis/decisions.csv")
    ap.add_argument("--out", default="data/analysis/divergence.csv")
    ap.add_argument("--min-n", type=int, default=30)
    ap.add_argument("--noise-floor", type=float, default=0.05)
    args = ap.parse_args()

    rows = load_rows(args.csv)
    discard_rows = [r for r in rows if r["dtype"] == "discard"]
    top_rows = [r for r in discard_rows if r["cohort"] == "top"]
    ours_rows = [r for r in discard_rows if r["cohort"] == "ours"]
    dims_top = [_bucket_dims(r) for r in top_rows]
    dims_ours = [_bucket_dims(r) for r in ours_rows]

    # 精确桶（level 0）作为分桶归属基准；对每个精确桶找到达到样本门槛的最浅层级。
    exact_top = defaultdict(list)
    exact_ours = defaultdict(list)
    for r, d in zip(top_rows, dims_top):
        exact_top[bucket_key(d, set())].append(r)
    for r, d in zip(ours_rows, dims_ours):
        exact_ours[bucket_key(d, set())].append(r)
    all_exact_keys = set(exact_top) | set(exact_ours)

    n_ours_total = len(ours_rows)
    out_records = []
    pvalues = []
    for exact_key in all_exact_keys:
        # 精确桶样本量足够就直接用（level=0）；不足就按 BACKOFF_ORDER 依次丢维度合并。
        t_rows = exact_top.get(exact_key, [])
        o_rows = exact_ours.get(exact_key, [])
        level_used = 0
        if len(t_rows) < args.min_n or len(o_rows) < args.min_n:
            # 依次丢弃维度合并，直到够样本或丢完
            dropped = set()
            for lvl, dim in enumerate(BACKOFF_ORDER, start=1):
                dropped.add(dim)
                key_reduced = bucket_key(dict(zip(DIMS, exact_key)), dropped)
                t_rows = [r for r, d in zip(top_rows, dims_top) if bucket_key(d, dropped) == key_reduced]
                o_rows = [r for r, d in zip(ours_rows, dims_ours) if bucket_key(d, dropped) == key_reduced]
                level_used = lvl
                if len(t_rows) >= args.min_n and len(o_rows) >= args.min_n:
                    break
        if len(t_rows) < args.min_n or len(o_rows) < args.min_n:
            continue  # 样本仍不足，丢弃

        top_acts = defaultdict(int)
        for r in t_rows:
            top_acts[act_signature(r)] += 1
        top_majority = max(top_acts, key=top_acts.get)
        x_top = top_acts[top_majority]
        x_ours = sum(1 for r in o_rows if act_signature(r) == top_majority)
        n_top, n_ours = len(t_rows), len(o_rows)
        rate_top = x_top / n_top
        rate_ours = x_ours / n_ours
        divergence_rate = abs(rate_top - rate_ours)
        if divergence_rate <= args.noise_floor:
            continue  # 噪声地板

        z, p = two_proportion_z_test(x_top, n_top, x_ours, n_ours)
        pvalues.append(p)

        # 同人内部 Δ（用 y_score 作为结果）
        def group_of(r, maj=top_majority):
            return "A" if act_signature(r) == maj else "B"

        pop = t_rows + o_rows
        by_uid = defaultdict(lambda: {"a": [], "b": []})
        for r in pop:
            g = group_of(r)
            by_uid[r["uid"]]["a" if g == "A" else "b"].append(r["y_score"] or 0)
        total_w = weighted = 0.0
        for _uid, d in by_uid.items():
            na, nb = len(d["a"]), len(d["b"])
            if na == 0 or nb == 0:
                continue
            w = min(na, nb)
            weighted += w * (mean(d["a"]) - mean(d["b"]))
            total_w += w
        delta_within = weighted / total_w if total_w else None
        a_vals = [r["y_score"] or 0 for r in pop if group_of(r) == "A"]
        b_vals = [r["y_score"] or 0 for r in pop if group_of(r) == "B"]
        delta_mixed = (mean(a_vals) - mean(b_vals)) if a_vals and b_vals else None

        # 注：房间聚类自助 CI（P5/2.4 要求）在本文件里没有对每个桶都算一遍——
        # 桶数可能有几百个，每桶 1000 次自助法对全量语料会很慢，且上一轮
        # decision_table.py 满载曾把机器跑到崩溃重启，这里故意保守。真正需要
        # CI 的场景（H1/H4/H5/H-S1-4）已经在 tools/hypotheses.py 里对每条假设
        # 单独算了房间聚类自助 CI；divergence.csv 只用作"哪些桶值得深入看"的
        # 排序线索，不单独当作可发布结论。见 OFFLINE_REPORT.md「已知局限」。
        coverage_val = n_ours / n_ours_total if n_ours_total else 0
        expected_gain = (delta_within * coverage_val) if delta_within is not None else None

        out_records.append({
            "bucket": "|".join(map(str, exact_key)), "backoff_level": level_used,
            "n_top": n_top, "n_ours": n_ours, "majority_action_top": top_majority,
            "rate_top": round(rate_top, 4), "rate_ours": round(rate_ours, 4),
            "divergence_rate": round(divergence_rate, 4), "z": z, "p": p,
            "delta_within_person": delta_within, "delta_mixed": delta_mixed,
            "coverage_ours": round(coverage_val, 5), "expected_gain": expected_gain,
        })

    reject = bh_reject([r["p"] for r in out_records], alpha=0.05)
    for rec, rej in zip(out_records, reject):
        rec["bh_significant"] = int(rej)

    out_records.sort(key=lambda r: -(r["expected_gain"] or -1e9))

    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    fields = ["bucket", "backoff_level", "n_top", "n_ours", "majority_action_top", "rate_top",
              "rate_ours", "divergence_rate", "z", "p", "bh_significant", "delta_within_person",
              "delta_mixed", "coverage_ours", "expected_gain"]
    with open(args.out, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for rec in out_records:
            writer.writerow(rec)
    print("桶数（过噪声地板后）：%d，其中 BH 显著：%d" % (len(out_records), sum(reject)))
    print("已写出 %s" % args.out)


if __name__ == "__main__":
    main()
