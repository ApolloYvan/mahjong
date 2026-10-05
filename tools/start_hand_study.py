"""起手一样，谁胡得更多：按「起手财神数 × 起手向听」对齐，比较高手和我们的胜率/得分/吃碰数。

    python3 tools/start_hand_study.py
    python3 tools/start_hand_study.py --recent-logs "logs/2026-09-2[45].jsonl"

只看闲家（13 张起手），庄家另算会混入连庄。起手是发牌决定的，不受打法影响，
所以同一格里的胜率差 = 纯打法差距，没有"还门清"这类选择偏差。
「我们(近期)」= 在 --recent-logs 里出现过的房（即当前版本 + 开关）。
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
MASTERS = ["⭐꧁༺🀆🀆🀆🀆༻꧂⭐", "Deepseek胡", "爆头研究所", "Astra-0", "晴总总，该请桂语山房了",
           "glm-flash", "铳一色14"]


def recent_rooms(pattern):
    rooms = set()
    rx = re.compile(r'"game_id": "(a_[0-9a-f]+)_')
    for path in glob.glob(pattern):
        with open(path, encoding="utf-8", errors="replace") as source:
            for line in source:
                m = rx.search(line)
                if m:
                    rooms.add(m.group(1))
    return rooms


def scan(path, masters, recent, out, per_master):
    try:
        with open(path, encoding="utf-8") as source:
            game = json.load(source)
    except (OSError, ValueError):
        return
    names = seat_names(game)
    if len(names) != 4 or not any(n == OUR or n in masters for n in names):
        return
    room = game.get("room_id") or ""
    info = {r.get("round_no"): r for r in game.get("rounds") or []}
    for rnd in merge_rounds(game):
        meta = info.get(rnd["round_no"])
        hands = rnd.get("start_hands")
        if not meta or rnd.get("truncated") or not hands or len(hands) != 4:
            continue
        dealer = meta.get("dealer", rnd.get("dealer"))
        winner = None if meta.get("is_draw") else meta.get("winner")
        scores = meta.get("scores") or [0] * 4
        calls, discards, win_turn = Counter(), Counter(), None
        fan = 0
        for ev in rnd["events"]:
            kind, seat = ev["type"], ev.get("seat")
            if kind in ("chi", "peng") and seat is not None:
                calls[seat] += 1
            elif kind == "tile_discarded" and seat is not None:
                discards[seat] += 1
            elif kind == "round_ended":
                fan = (ev.get("data") or {}).get("fan") or 0
        if winner is not None:
            win_turn = discards[winner]
        for seat, name in enumerate(names):
            if seat == dealer or len(hands[seat]) != 13:
                continue
            groups = []
            if name == OUR:
                groups.append("我们(全部)")
                if room in recent:
                    groups.append("我们(近期)")
            elif name in masters:
                groups.append("高手合计")
            else:
                continue
            counts = tuple(to_counts(hands[seat]))
            key = (min(hands[seat].count(JOKER), 2), min(shanten(counts, 0), 5))
            won = winner == seat
            for g in groups:
                c = out[g][key]
                c["n"] += 1
                c["win"] += won
                c["score"] += scores[seat]
                c["calls"] += calls[seat]
                if won:
                    c["turn"] += win_turn
                    c["fan"] += fan
            if name in masters:
                pm = per_master[name][key[0]]
                pm["n"] += 1
                pm["win"] += won
                pm["score"] += scores[seat]


def cell(c):
    if c["n"] < 15:
        return "n=%-4d%28s" % (c["n"], "")
    w = c["win"] or 1
    return "n=%-4d 胡%3.0f%% 分%+5.1f 吃碰%.2f 胡巡%4.1f" % (
        c["n"], 100 * c["win"] / c["n"], c["score"] / c["n"], c["calls"] / c["n"], c["turn"] / w)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--players", nargs="*", default=MASTERS)
    ap.add_argument("--recent-logs", default="logs/2026-09-25.jsonl")
    args = ap.parse_args()
    masters = set(args.players)
    recent = recent_rooms(args.recent_logs)
    print("近期房间数（来自 %s）：%d" % (args.recent_logs, len(recent)))

    out = defaultdict(lambda: defaultdict(Counter))
    per_master = defaultdict(lambda: defaultdict(Counter))
    files = discover_files()
    for i, path in enumerate(files, 1):
        scan(path, masters, recent, out, per_master)
        if i % 500 == 0:
            print("  已扫 %d / %d" % (i, len(files)), flush=True)

    groups = ["高手合计", "我们(全部)", "我们(近期)"]
    print("\n=== 闲家起手：财神数 × 向听 → 胡率 / 分/局 / 每局吃碰次数 / 胡牌时是第几手 ===")
    print("（向听 5 表示 5+；样本 <15 不显示）")
    for j in range(3):
        for s in range(0, 6):
            key = (j, s)
            if all(out[g][key]["n"] == 0 for g in groups):
                continue
            label = "%s白 %d向听" % ("2+" if j == 2 else j, s)
            print("%-10s | %s" % (label, " | ".join("%s %s" % (g[:5], cell(out[g][key])) for g in groups)))
        print()

    print("=== 按财神数汇总（不分向听） ===")
    for j in range(3):
        parts = []
        for g in groups:
            tot = Counter()
            for (jj, _), c in out[g].items():
                if jj == j:
                    tot.update(c)
            parts.append("%s %s" % (g[:5], cell(tot)))
        print("%-4s白 | %s" % ("2+" if j == 2 else j, " | ".join(parts)))

    print("\n=== 每位高手：闲家起手按财神数的胜率 / 分局 ===")
    for name in args.players:
        row = []
        for j in range(3):
            c = per_master[name][j]
            row.append("%s白 n=%d 胡%.0f%% 分%+.1f" % ("2+" if j == 2 else j, c["n"],
                                                   100 * c["win"] / max(c["n"], 1), c["score"] / max(c["n"], 1)))
        print("%-24s %s" % (name[:24], " | ".join(row)))


if __name__ == "__main__":
    main()
