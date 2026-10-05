"""扣掉牌运之后，我们 vs 高手：同一把尺子。

    python3 tools/luck_vs_masters.py

期望分 = 全语料所有玩家「同样起手（庄/闲 × 财神数 0/1/2+ × 起手向听）」的平均得分（公共基准，不是我们自己的历史）。
实际 − 期望 = 扣掉发牌运气之后的打法水平；±值 = 1.96 × 标准误。
「我们(当前)」= --since 之后开打的房（与 luck_adjust / baotou_funnel 一致）。
"""
import argparse
import json
import math
import os
import sys
from collections import defaultdict
from multiprocessing import Pool

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))

from mining_common import discover_files, merge_rounds  # noqa: E402
from mining_common import seat_names  # noqa: E402
from baotou_funnel import MASTERS, OUR, recent_rooms  # noqa: E402
from mj.shanten import shanten  # noqa: E402
from mj.tiles import to_counts  # noqa: E402

JOKER = "白"


def scan(path):
    try:
        with open(path, encoding="utf-8") as source:
            game = json.load(source)
    except (OSError, ValueError):
        return None, []
    names = seat_names(game)
    if len(names) != 4:
        return None, []
    info = {r.get("round_no"): r for r in game.get("rounds") or []}
    out = []
    for rnd in merge_rounds(game):
        meta = info.get(rnd["round_no"])
        hands = rnd.get("start_hands")
        if not meta or not hands or len(hands) != 4 or rnd.get("truncated"):
            continue
        scores = meta.get("scores") or [0] * 4
        dealer = meta.get("dealer", rnd.get("dealer"))
        winner = None if meta.get("is_draw") else meta.get("winner")
        for s in range(4):
            if not hands[s]:
                continue
            key = (s == dealer, min(hands[s].count(JOKER), 2), min(shanten(tuple(to_counts(hands[s])), 0), 5))
            out.append((names[s], key, scores[s], winner == s, tuple(names[o] for o in range(4) if o != s)))
    return game.get("room_id"), out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default="2026-09-25T15:30")
    ap.add_argument("--exclude", nargs="*", default=[], help="这些房间不计入「我们(当前)」（看结果是否被个别房间撑起来）")
    ap.add_argument("--jobs", type=int, default=6)
    args = ap.parse_args()
    recent = recent_rooms(args.since)
    files = discover_files()
    rows = []
    with Pool(args.jobs) as pool:
        for done, (room, part) in enumerate(pool.imap_unordered(scan, files, chunksize=8), 1):
            if room in args.exclude:
                continue
            for name, key, score, won, opps in part:
                if name == OUR:
                    g = "我们(当前)" if room in recent else "我们(以前)"
                elif name in MASTERS:
                    g = name
                else:
                    g = "其他玩家"
                rows.append((g, key, score, won, name, opps))
            if done % 500 == 0 or done == len(files):
                print("  已扫 %d / %d" % (done, len(files)), flush=True)

    tot, cnt = defaultdict(float), defaultdict(int)
    for _g, key, score, *_ in rows:
        tot[key] += score
        cnt[key] += 1
    base = {k: tot[k] / cnt[k] for k in tot}

    # 对手水平：每个玩家全历史的 实际-期望；零和桌上 自己的分 ≈ 自己水平 − 三个对手水平的平均
    sk_t, sk_n = defaultdict(float), defaultdict(int)
    for _g, key, score, _w, name, _o in rows:
        sk_t[name] += score - base[key]
        sk_n[name] += 1
    skill = {n: sk_t[n] / sk_n[n] for n in sk_t}
    acc = defaultdict(list)
    for g, key, score, won, _name, opps in rows:
        rec = (score, base[key], won, sum(skill.get(o, 0.0) for o in opps) / 3)
        acc[g].append(rec)
        if g in MASTERS:
            acc["高手合计"].append(rec)

    def line(g):
        xs = acc.get(g) or []
        n = len(xs)
        if n < 30:
            return None
        diff = [s - e for s, e, _w, _o in xs]
        mean = sum(diff) / n
        sd = math.sqrt(sum((d - mean) ** 2 for d in diff) / (n - 1))
        opp = sum(o for *_x, o in xs) / n
        return "%-26s %6d  %+7.2f  %+7.2f  %+7.2f ±%.2f  %5.1f%%  %+7.2f  %+9.2f" % (
            g, n, sum(x[0] for x in xs) / n, sum(x[1] for x in xs) / n, mean,
            1.96 * sd / math.sqrt(n), 100.0 * sum(x[2] for x in xs) / n, opp, mean + opp)

    print("\n=== 扣掉牌运后的打法水平（公共基准）===")
    print("%-26s %6s  %7s  %7s  %13s  %6s  %7s  %9s" % ("玩家", "局数", "实际分/局", "起手期望", "实际-期望", "胜率",
                                                        "对手水平", "对手修正后"))
    order = ["我们(当前)", "我们(以前)", "高手合计"] + sorted(MASTERS, key=lambda m: -len(acc.get(m) or [])) + ["其他玩家"]
    for g in order:
        s = line(g)
        if s:
            print(s)
    print("\n读法：「实际-期望」扣掉了发牌运气；「对手修正后」= 实际-期望 + 同桌对手平均水平，再扣掉坐强桌/弱桌的影响，")
    print("  是最终拿来和高手比的数（误差与 实际-期望 的 ± 同量级）。")


if __name__ == "__main__":
    main()
