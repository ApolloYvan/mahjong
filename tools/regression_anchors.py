# -*- coding: utf-8 -*-
"""第八节回归锚点对账：用 decisions.csv 汇总复现 dealer_edge / score_attribution /
joker_playbook / policy_diff 的关键数字，供 OFFLINE_REPORT.md 填数。

只读 data/analysis/decisions.csv，不重新跑重放（快，几秒钟）。
"""
import os
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.hypotheses import load_rows, OUR_UID  # noqa: E402


def main():
    rows = load_rows("data/analysis/decisions.csv")
    ours = [r for r in rows if r["uid"] == OUR_UID]
    print("我们的决策行数：%d" % len(ours))

    # --- dealer_edge：坐庄/坐闲局数与胡率（按 round 去重，不是按决策点） ---
    seen_rounds = set()
    dealer_rounds = 0
    idle_rounds = 0
    dealer_hu = 0
    idle_hu = 0
    for r in ours:
        key = (r["room_id"], r["game_id"], r["round_no"])
        if key in seen_rounds:
            continue
        seen_rounds.add(key)
        if int(r["is_dealer"]):
            dealer_rounds += 1
            dealer_hu += int(r["y_hu"] or 0)
        else:
            idle_rounds += 1
            idle_hu += int(r["y_hu"] or 0)
    print("dealer_edge 对账：坐庄局数=%d 庄胡率=%.1f%%  坐闲局数=%d 闲胡率=%.1f%%"
          % (dealer_rounds, 100 * dealer_hu / max(1, dealer_rounds),
             idle_rounds, 100 * idle_hu / max(1, idle_rounds)))

    # --- score_attribution：收入/局、支出/局（按 round 去重，用 y_score） ---
    income = outgo = 0.0
    n_rounds = 0
    seen2 = set()
    for r in ours:
        key = (r["room_id"], r["game_id"], r["round_no"])
        if key in seen2:
            continue
        seen2.add(key)
        n_rounds += 1
        sc = r["y_score"] or 0
        if sc > 0:
            income += sc
        else:
            outgo += -sc
    print("score_attribution 对账：收入/局=%.3f 支出/局=%.3f（n_rounds=%d）"
          % (income / max(1, n_rounds), outgo / max(1, n_rounds), n_rounds))

    # --- joker_playbook：胡牌数、总爆头占比 ---
    hu_rows = defaultdict(list)
    for r in ours:
        key = (r["room_id"], r["game_id"], r["round_no"])
        if int(r["y_hu"] or 0):
            hu_rows[key].append(r)
    n_hu = len(hu_rows)
    # 用每局里"最后一条该座位的行"的 is_baotou 近似胡牌那一刻的爆头状态
    # （精确复现 joker_playbook 需要贴到 round_ended 的那一手，此处用 dtype=hu_or_not
    # 里 act_type=='hu' 那一行的 is_baotou，若无则退化为该局同座位最后一条 discard 行）。
    n_bt = 0
    for key, rs in hu_rows.items():
        hu_row = next((r for r in rs if r["dtype"] == "hu_or_not" and r["act_type"] == "hu"), None)
        if hu_row is not None:
            n_bt += int(hu_row["is_baotou"] or 0)
    print("joker_playbook 对账：胡牌数=%d 总爆头占比=%.1f%%（n_bt=%d，只统计 hu_or_not 命中的局）"
          % (n_hu, 100 * n_bt / max(1, n_hu), n_bt))

    # --- policy_diff：与自己历史的一致率（baseline_only 列 vs 真实分流列） ---
    disc = [r for r in ours if r["dtype"] == "discard" and r["our_policy_act_baseline_only"]]
    if disc:
        agree_baseline = sum(1 for r in disc if r["act_tile"] == r["our_policy_act_baseline_only"])
        print("policy_diff 对账（baseline_only）：一致率=%.1f%%（n=%d）"
              % (100 * agree_baseline / len(disc), len(disc)))
    disc_cf = [r for r in ours if r["dtype"] == "discard" and r["our_policy_act"]]
    if disc_cf:
        agree_real = sum(1 for r in disc_cf if r["act_tile"] == r["our_policy_act"])
        print("真实分流（our_policy_act）：一致率=%.1f%%（n=%d）"
              % (100 * agree_real / len(disc_cf), len(disc_cf)))
        if disc:
            print("两者一致率之差：%.1fpp"
                  % (100 * agree_real / len(disc_cf) - 100 * agree_baseline / len(disc)))


if __name__ == "__main__":
    main()
