"""新旧吃碰门禁的对照：直接重放生产日志里的真实响应窗口。

2026-09-23 加入「听牌后吃碰必须做成爆头或加宽听口」的判据后，用这个工具看
它实际拦掉多少、放行多少，以及对照组（听牌后吃碰）我们与对手的执行率差距
（实测 80.7% vs 23.7%，z=+44.9）。

    python3 tools/claim_gate_impact.py                 # 默认抽样 40000 条窗口
    python3 tools/claim_gate_impact.py --sample 0      # 全量
"""
import argparse
import glob
import json
import os
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mj.responses import claim_assessment
from mj.shanten import shanten
from mj.tiles import to_counts

ROLLBACK = {"tenpai_claim_margin": -99}     # 旧行为：听牌分支不设任何听口要求


def windows(log_glob, limit):
    seen = 0
    for path in sorted(glob.glob(log_glob)):
        with open(path, encoding="utf-8", errors="replace") as source:
            for line in source:
                if '"response_' not in line:
                    continue
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                if record.get("kind") != "decision":
                    continue
                payload = record["payload"]
                phase = payload.get("phase")
                if phase not in ("response_chi", "response_peng"):
                    continue
                decision = payload.get("decision") or {}
                tile = decision.get("tile") or payload.get("window_tile")
                hand = payload.get("hand") or []
                if not tile or not hand:
                    continue
                snapshot = {**payload, "my_hand": hand, "window_tile": tile}
                if decision.get("action") == "chi":
                    take = tuple(t for t in (decision.get("tiles") or []) if t != tile)
                elif decision.get("action") == "peng":
                    take = (tile, tile)
                else:
                    continue                      # pass/timeout 无法还原拿哪两张
                if len(take) != 2 or any(hand.count(t) < take.count(t) for t in set(take)):
                    continue
                yield snapshot, take, decision.get("action")
                seen += 1
                if limit and seen >= limit:
                    return


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--logs", default="logs/*.jsonl")
    parser.add_argument("--sample", type=int, default=40000)
    args = parser.parse_args()

    stats = Counter()
    reasons = Counter()
    for snapshot, take, action in windows(args.logs, args.sample):
        seat = snapshot.get("seat", -1)
        melds = snapshot.get("melds") or []
        groups = len(melds[seat]) if isinstance(melds, list) and len(melds) == 4 else 0
        counts = tuple(to_counts(snapshot["my_hand"]))
        if sum(counts) + 3 * groups != 13:
            continue
        tenpai = shanten(counts, groups) == 0
        old = claim_assessment(snapshot, take, weights=ROLLBACK)["allowed"]
        new = claim_assessment(snapshot, take)
        key = "听牌中" if tenpai else "未听牌"
        stats[(key, action, old, new["allowed"])] += 1
        if tenpai and not new["allowed"]:
            reasons[new["reason"]] += 1

    print("历史上我们**实际执行过**的吃碰，在新旧门禁下的判定：")
    print("%-8s %-6s %8s %10s %10s %10s"
          % ("状态", "动作", "样本", "旧:放行", "新:放行", "新增拦截"))
    print("-" * 62)
    for key in ("听牌中", "未听牌"):
        for action in ("chi", "peng"):
            rows = {k: v for k, v in stats.items() if k[0] == key and k[1] == action}
            total = sum(rows.values())
            if not total:
                continue
            old_ok = sum(v for k, v in rows.items() if k[2])
            new_ok = sum(v for k, v in rows.items() if k[3])
            blocked = sum(v for k, v in rows.items() if k[2] and not k[3])
            print("%-8s %-6s %8d %9.1f%% %9.1f%% %9.1f%%"
                  % (key, action, total, 100 * old_ok / total,
                     100 * new_ok / total, 100 * blocked / total))
    if reasons:
        print("\n听牌中被新门禁拒绝的理由：", dict(reasons))
    print("\n对照：实测听牌后吃，我们执行率 80.7%(n=1220)、对手 23.7%(n=5361)。")
    print("新门禁的目标是把我们从 80.7% 往对手那个区间拉，而不是拉到 0。")


if __name__ == "__main__":
    main()
