"""分数下滑是打法变差还是牌运差：按时间顺序把我们打过的房分批，每批拆成「起手运气」和「打法」。

    python3 tools/luck_adjust.py              # 每 10 房一批
    python3 tools/luck_adjust.py --chunk 5

期望分 = 我们全历史里「同样的起手（庄/闲 × 财神数 × 向听）」的平均得分。
实际 − 期望 = 扣掉发牌运气之后的打法表现；这一列随时间上升才说明优化有效。
房间时间取自 logs/*.jsonl 中该房第一条记录。
"""
import argparse
import glob
import json
import os
import re
import sys
from collections import Counter, defaultdict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))

from mining_common import discover_files, merge_rounds  # noqa: E402
from mining_common import seat_names  # noqa: E402
from mj.shanten import shanten  # noqa: E402
from mj.tiles import to_counts  # noqa: E402

OUR = "重生之我是雀神"
JOKER = "白"


def room_times(pattern):
    first = {}
    rx = re.compile(r'^\{"time": "([^"]+)".*?"game_id": "(a_[0-9a-f]+)_')
    for path in sorted(glob.glob(pattern)):
        with open(path, encoding="utf-8", errors="replace") as source:
            for line in source:
                m = rx.search(line)
                if m and m.group(2) not in first:
                    first[m.group(2)] = m.group(1)
    return first


def our_rounds(path):
    try:
        with open(path, encoding="utf-8") as source:
            game = json.load(source)
    except (OSError, ValueError):
        return None, []
    names = seat_names(game)
    if OUR not in names:
        return None, []
    seat = names.index(OUR)
    info = {r.get("round_no"): r for r in game.get("rounds") or []}
    out = []
    for rnd in merge_rounds(game):
        meta = info.get(rnd["round_no"])
        hands = rnd.get("start_hands")
        if not meta or not hands or len(hands) != 4 or not hands[seat]:
            continue
        hand = hands[seat]
        dealer = meta.get("dealer", rnd.get("dealer")) == seat
        key = (dealer, min(hand.count(JOKER), 2), min(shanten(tuple(to_counts(hand)), 0), 5))
        won = meta.get("winner") == seat and not meta.get("is_draw")
        out.append((key, (meta.get("scores") or [0] * 4)[seat], won, hand.count(JOKER)))
    return game.get("room_id"), out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--chunk", type=int, default=10)
    ap.add_argument("--logs", default="logs/*.jsonl")
    args = ap.parse_args()

    times = room_times(args.logs)
    by_room = defaultdict(list)
    base = defaultdict(lambda: [0.0, 0])
    files = discover_files()
    for i, path in enumerate(files, 1):
        room, rows = our_rounds(path)
        for key, score, won, jokers in rows:
            base[key][0] += score
            base[key][1] += 1
            by_room[room].append((key, score, won, jokers))
        if i % 500 == 0:
            print("  已扫 %d / %d" % (i, len(files)), flush=True)

    rooms = sorted((t, r) for r, t in times.items() if r in by_room)
    print("\n有时间戳的房：%d（没有日志的更早的房不计入）" % len(rooms))
    print("\n%-3s %-16s %-16s %5s %6s %8s %8s %9s %7s %10s %10s %7s %7s" % (
        "批", "开始", "结束", "房数", "局数", "实际分/局", "起手期望", "实际-期望", "平均财神",
        "庄:实-期", "闲:实-期", "庄胡率", "闲胡率"))
    print("-" * 140)
    for n, start in enumerate(range(0, len(rooms), args.chunk), 1):
        part = rooms[start:start + args.chunk]
        rows = [x for _, r in part for x in by_room[r]]
        if not rows:
            continue
        actual = sum(x[1] for x in rows) / len(rows)
        expect = sum(base[x[0]][0] / base[x[0]][1] for x in rows) / len(rows)
        jok = sum(x[3] for x in rows) / len(rows)
        side = {}
        for flag in (True, False):
            sub_rows = [x for x in rows if x[0][0] == flag]
            m = max(len(sub_rows), 1)
            side[flag] = (sum(x[1] - base[x[0]][0] / base[x[0]][1] for x in sub_rows) / m,
                          sum(x[2] for x in sub_rows) / m)
        print("%-3d %-16s %-16s %5d %6d %+9.2f %+8.2f %+9.2f %7.2f %+10.2f %+10.2f %6.1f%% %6.1f%%" % (
            n, part[0][0][5:16], part[-1][0][5:16], len(part), len(rows), actual, expect,
            actual - expect, jok, side[True][0], side[False][0], 100 * side[True][1], 100 * side[False][1]))
    print("\n庄/闲两列：分开计算的「实际 − 期望」（庄家局约占 1/4，单批误差约 ±1.5）。")
    print("无财神拟合约 09-25 午后生效、持财神拟合约 09-25 15:30 生效（批 22 后半到批 23 起）；庄家弃牌也改走拟合打分，看庄那一列是否在那之后变差。")
    print("\n读法：「起手期望」为负 = 这批发到的牌比我们平时差（运气）；"
          "「实际-期望」= 扣掉运气后的打法，单批 80 局×10 房的误差约 ±0.5。")


if __name__ == "__main__":
    main()
