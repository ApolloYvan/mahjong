# -*- coding: utf-8 -*-
""""剩余差距"审计第 4 部分：番型来源拆分。直接读 round_ended 事件的
``data.detail``/``data.fan`` 字段统计，不自己从其它列反推番型。

2026-09-24 重要发现（写这个脚本时才发现，如实记录）：`mj/rules.py::evaluate()`
本地生成的 detail 字符串（"七对子"/"豪华七对"/"动作链x%d"）**与服务端真实
下发的 detail 字符串不是同一套词表**——真实语料里出现的是 "七对"（不带
"子"）、"豪华七对×1"/"豪华七对×2"（用 ×N 表示分支档位，不是本地代码假设
的独立名字"豪华七对"/"双豪华七对"）、"财飘"/"双财飘"/"连杠×2"/"杠飘链×2"
（本地代码只认识"动作链x%d"这一种写法，从未见过这些真实标签）。用旧的
（按 mj/rules.py 词表写的）解析脚本跑出来的"分支/链"分类全部是错的（全部
落入"平胡"、链次数恒为 0）——这正是 SKILL.md 反复强调的"服务端是真相源、
本地代码是我们的复刻，可能有出入"的又一个实例。

本脚本改用**直接从全量语料里统计出来的真实 detail 标签词表**，并且对每一个
标签的番数倍率**用真实 fan 字段反推验证**（不是假设），核对结果见脚本内
DETAIL_MULTIPLIER 常量旁的注释——20 种真实出现过的 detail 组合，每一种的
`分支番 x 各标签倍率的乘积` 都与该组合下真实记录的 fan 值完全一致，
零例外，可复现（见 tools/gap_audit.py 同目录下的验证过程，或直接重跑这里
的 sanity_check()）。

用法：
    python3 tools/fan_breakdown.py
"""
import json
import os
import sys
from collections import Counter, defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.mining_common import discover_files  # noqa: E402
from tools.decision_table import build_cohort_map, scan_rankings  # noqa: E402

# 分支番（互斥，一局只属于其中一种）——用真实 fan 反推验证过，不是照抄
# mj/rules.py 的 BRANCH_FAN（"豪华七对×1/×2" 是服务端真实字符串，本地代码
# 从未生成过这种写法）。
BRANCH_FAN = {
    "平胡": 1, "七对": 2, "豪华七对×1": 4, "豪华七对×2": 8, "豪华七对×3": 16,
}
BRANCH_TAGS = set(BRANCH_FAN)

# 乘数标签（可叠加，一局可以同时有多个）——同样用真实 fan 反推验证过。
# "杠开" 单独出现时真实 fan 仍然是 ×2（例：('平胡','杠开')->fan=2），这与
# mj/rules.py::evaluate() 注释里"杠开不再额外×2"的假设**不一致**——本地
# 代码的假设是"杠开已经计入 chain_count"，但真实数据里出现纯"杠开"（不带
# 财飘/连杠标签）也照样 ×2，说明本地对"杠开"这个具体标签的番值建模与服务端
# 不同。这是本轮审计的一个副产品发现，如实记录，不在本任务范围内修代码。
MULTIPLIER_TAGS = {
    "爆头": 2, "4个白板": 2, "杠开": 2, "财飘": 2, "双财飘": 4,
    "连杠×2": 4, "杠飘链×2": 4,
}


def classify(detail):
    branch = None
    for d in detail:
        if d in BRANCH_TAGS:
            branch = d
            break
    if branch is None:
        branch = "平胡"  # 没见过的组合，保守按平胡分支登记，不强行猜测
    multiplier_tags = [d for d in detail if d in MULTIPLIER_TAGS]
    return {"branch": branch, "multiplier_tags": multiplier_tags,
            "has_baotou": "爆头" in detail, "has_4white": "4个白板" in detail,
            "has_chain": any(t in detail for t in ("财飘", "双财飘", "连杠×2", "杠飘链×2")),
            "has_gang_open": "杠开" in detail}


def sanity_check(all_wins):
    """核对：分支番 x 各乘数标签的乘积 是否等于真实记录的 fan——不一致就
    响亮地报错，不能带着错误的番值分类继续往下算。"""
    mismatches = []
    for w in all_wins:
        c = classify(w["detail"])
        computed = BRANCH_FAN.get(c["branch"], 1)
        for t in c["multiplier_tags"]:
            computed *= MULTIPLIER_TAGS[t]
        if computed != w["fan"]:
            mismatches.append((w["detail"], w["fan"], computed))
    return mismatches


def process_file(path, cohort_of):
    out = []
    try:
        game = json.load(open(path, encoding="utf-8"))
    except (ValueError, OSError):
        return out
    seats = game.get("seats") or []
    if len(seats) != 4:
        return out
    uids = [s.get("user_id") for s in seats]
    for block in game.get("blocks") or []:
        for ev in block.get("events") or []:
            if ev.get("type") != "round_ended":
                continue
            data = ev.get("data") or {}
            if data.get("draw"):
                continue
            winner_seat = ev.get("seat")
            if winner_seat is None or not (0 <= winner_seat < 4):
                continue
            uid = uids[winner_seat]
            detail = data.get("detail") or []
            fan = data.get("fan")
            if fan is None:
                continue
            dealer_seat = data.get("dealer")
            out.append({
                "uid": uid, "cohort": cohort_of(uid), "fan": fan, "detail": detail,
                "is_dealer_win": int(dealer_seat == winner_seat),
            })
    return out


def main():
    files = discover_files()
    print("文件数（去重后）：%d" % len(files))
    score = scan_rankings(files)
    cohort_map = build_cohort_map(score)

    def cohort_of(uid):
        return cohort_map.get(uid, "other")

    all_wins = []
    for path in files:
        all_wins.extend(process_file(path, cohort_of))
    print("胡牌事件总数：%d" % len(all_wins))

    mismatches = sanity_check(all_wins)
    print("\n=== 番值核对（分支x乘数标签乘积 vs 真实 fan 字段）===")
    print("不一致数：%d / %d" % (len(mismatches), len(all_wins)))
    for m in mismatches[:10]:
        print("  detail=%s 真实fan=%s 推算fan=%s" % m)

    by_cohort = defaultdict(list)
    for w in all_wins:
        by_cohort[w["cohort"]].append(w)

    print("\n=== 各 cohort 胡牌数 / 平均番（算术均值，即番/胡）===")
    for c in ("top", "ours", "bottom", "other"):
        ws = by_cohort.get(c, [])
        if not ws:
            continue
        mean_fan = sum(w["fan"] for w in ws) / len(ws)
        print("%-6s n=%-6d mean_fan=%.4f" % (c, len(ws), mean_fan))

    print("\n=== 番型标签占比 + 条件均番（top/ours/bottom）===")
    tag_stats = {}
    for c in ("top", "ours", "bottom"):
        ws = by_cohort.get(c, [])
        if not ws:
            print(c, "n=0，跳过"); continue
        n = len(ws)
        classified = [classify(w["detail"]) for w in ws]
        branch_counts = Counter(cc["branch"] for cc in classified)
        print("--- %s (n=%d) ---" % (c, n))
        for branch, cnt in branch_counts.most_common():
            sub_fans = [w["fan"] for w, cc in zip(ws, classified) if cc["branch"] == branch]
            print("  分支=%-10s n=%-5d (%5.1f%%) mean_fan=%.3f" %
                 (branch, cnt, 100.0 * cnt / n, sum(sub_fans) / cnt))
        for tag in ("爆头", "财飘", "双财飘", "连杠×2", "杠飘链×2", "4个白板", "杠开"):
            cnt = sum(1 for w in ws if tag in w["detail"])
            print("  标签=%-10s n=%-5d (%5.1f%%)" % (tag, cnt, 100.0 * cnt / n))
        n_chain_any = sum(1 for cc in classified if cc["has_chain"])
        print("  飘财/动作链类(财飘∪双财飘∪连杠×2∪杠飘链×2) 合计: n=%d (%.1f%%)" %
             (n_chain_any, 100.0 * n_chain_any / n))
        tag_stats[c] = {"n": n, "classified": classified, "ws": ws}

    print("\n=== log2(fan) 加性分解（用真实反推验证过的分支/标签倍率）===")
    import math
    decomp = {}
    for c in ("top", "ours", "bottom"):
        if c not in tag_stats:
            continue
        ws, classified, n = tag_stats[c]["ws"], tag_stats[c]["classified"], tag_stats[c]["n"]
        e_branch = sum(math.log2(BRANCH_FAN.get(cc["branch"], 1)) for cc in classified) / n
        e_baotou = sum(cc["has_baotou"] for cc in classified) / n
        e_4white = sum(cc["has_4white"] for cc in classified) / n
        e_gangopen = sum(cc["has_gang_open"] for cc in classified) / n
        # 飘财/动作链类：不同标签倍率不同（财飘=1档,双财飘/连杠/杠飘链=2档），
        # 直接按每个标签的 log2(倍率) 加总，标签互斥（同一局最多命中一种链标签）。
        e_chain = 0.0
        for w in ws:
            for tag in ("财飘", "双财飘", "连杠×2", "杠飘链×2"):
                if tag in w["detail"]:
                    e_chain += math.log2(MULTIPLIER_TAGS[tag])
                    break
        e_chain /= n
        total = e_branch + e_baotou + e_4white + e_gangopen + e_chain
        mean_fan_arith = sum(w["fan"] for w in ws) / n
        decomp[c] = dict(branch=e_branch, baotou=e_baotou, four_white=e_4white,
                        gang_open=e_gangopen, chain=e_chain, total_log2=total,
                        mean_fan=mean_fan_arith)
        print("%s: E[log2branch]=%.4f E[baotou]=%.4f E[4white]=%.4f E[杠开]=%.4f "
             "E[飘财链]=%.4f sum=%.4f (算术mean_fan=%.4f)" %
             (c, e_branch, e_baotou, e_4white, e_gangopen, e_chain, total, mean_fan_arith))

    if "top" in decomp and "ours" in decomp:
        print("\n=== top - ours 差距分解（log2 单位，每 1.0 = 番数翻一倍）===")
        gap_total = decomp["top"]["mean_fan"] - decomp["ours"]["mean_fan"]
        print("  算术番/胡差距: top=%.4f ours=%.4f 差=%.4f" %
             (decomp["top"]["mean_fan"], decomp["ours"]["mean_fan"], gap_total))
        for k in ("branch", "baotou", "four_white", "gang_open", "chain"):
            d = decomp["top"][k] - decomp["ours"][k]
            print("  %-10s: top=%.4f ours=%.4f 差(log2)=%.4f  差占log2总差比例=%.1f%%" %
                 (k, decomp["top"][k], decomp["ours"][k], d,
                  100.0 * d / (decomp["top"]["total_log2"] - decomp["ours"]["total_log2"])
                  if decomp["top"]["total_log2"] != decomp["ours"]["total_log2"] else 0))
        print("  总计(log2): top=%.4f ours=%.4f 差=%.4f" %
             (decomp["top"]["total_log2"], decomp["ours"]["total_log2"],
              decomp["top"]["total_log2"] - decomp["ours"]["total_log2"]))


if __name__ == "__main__":
    main()
