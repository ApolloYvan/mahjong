"""七对路线：高手在「第 N 手打完时有几个对子 + 几个财神」时，最后有多少比例胡成七对。

    python3 tools/qidui_study.py
    python3 tools/qidui_study.py --turn 4 --players 爆头研究所 Deepseek胡

逐局重放全部事件文件（models/events + tools/models/events），只看门清手。
表格同一行 = 同样的起手对子/财神状态，比较高手和我们谁更多地转成七对、谁总胡率更高。
我们的代码只在 ≥6 对时才走七对（mj/ev.py seven_pairs_route）；如果高手在 4~5 对 + 财神
时就已经大量胡七对且总胡率不降，说明我们的门槛太高。
"""
import argparse
import os
import sys
from collections import Counter, defaultdict
import json

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from mining_common import discover_files, merge_rounds  # noqa: E402
from mining_common import seat_names  # noqa: E402

OUR = "重生之我是雀神"
JOKER = "白"
MASTERS = ["⭐꧁༺🀆🀆🀆🀆༻꧂⭐", "Deepseek胡", "爆头研究所"]


def pairs_of(hand):
    c = Counter(t for t in hand if t != JOKER)
    return sum(n // 2 for n in c.values()), hand.count(JOKER)


def scan(path, tracked, turn, out, totals):
    try:
        with open(path, encoding="utf-8") as source:
            game = json.load(source)
    except (OSError, ValueError):
        return
    names = seat_names(game)
    if len(names) != 4 or not any(n in tracked for n in names):
        return
    info = {r.get("round_no"): r for r in game.get("rounds") or []}
    for rnd in merge_rounds(game):
        meta = info.get(rnd["round_no"])
        if not meta or rnd.get("truncated") or not rnd.get("start_hands"):
            continue
        hands = [list(h) for h in rnd["start_hands"]]
        melded = [False] * 4
        discards = [0] * 4
        state = {}
        detail = []
        try:
            for ev in rnd["events"]:
                kind, seat, tile = ev["type"], ev.get("seat"), ev.get("tile")
                data = ev.get("data") or {}
                if kind == "tile_drawn":
                    hands[seat].append(tile)
                elif kind == "tile_discarded":
                    hands[seat].remove(tile)
                    discards[seat] += 1
                    if discards[seat] == turn and not melded[seat] and len(hands[seat]) == 13:
                        state[seat] = pairs_of(hands[seat])
                elif kind == "chi":
                    used = list(data.get("tiles") or [])
                    used.remove(tile)
                    for t in used:
                        hands[seat].remove(t)
                    melded[seat] = True
                elif kind == "peng":
                    for _ in range(2):
                        hands[seat].remove(tile)
                    melded[seat] = True
                elif kind == "gang":
                    take = {"an": 4, "ming": 3, "bu": 1}[data.get("kind")]
                    for _ in range(take):
                        hands[seat].remove(tile)
                    if data.get("kind") != "an":
                        melded[seat] = True
                elif kind == "round_ended":
                    detail = data.get("detail") or []
        except (ValueError, KeyError, IndexError, TypeError):
            continue
        winner = None if meta.get("is_draw") else meta.get("winner")
        qd = any("七对" in x for x in detail)
        for seat, name in enumerate(names):
            if name not in tracked:
                continue
            won = winner == seat
            if won:
                totals[name]["wins"] += 1
                totals[name]["qd"] += qd
                totals[name]["qd_bt"] += qd and any("爆头" in x for x in detail)
            if seat not in state:
                continue
            p, j = state[seat]
            key = (min(p, 6), min(j, 2))
            cell = out[name][key]
            cell["n"] += 1
            cell["win"] += won
            cell["qd"] += won and qd
            cell["score"] += (meta.get("scores") or [0] * 4)[seat]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--turn", type=int, default=5, help="看第几手打完时的状态（默认 5）")
    ap.add_argument("--players", nargs="*", default=MASTERS)
    args = ap.parse_args()
    tracked = list(dict.fromkeys(args.players + [OUR]))

    out = {n: defaultdict(Counter) for n in tracked}
    totals = {n: Counter() for n in tracked}
    files = discover_files()
    for i, path in enumerate(files, 1):
        scan(path, tracked, args.turn, out, totals)
        if i % 500 == 0:
            print("  已扫 %d / %d" % (i, len(files)), flush=True)

    print("\n=== 全部胡牌里七对占比 ===")
    print("%-24s %6s %8s %10s" % ("玩家", "胡牌", "七对占胡", "七对·爆头"))
    for n in tracked:
        t = totals[n]
        print("%-24s %6d %7.1f%% %10d" % (n[:24], t["wins"], 100 * t["qd"] / max(t["wins"], 1), t["qd_bt"]))

    print("\n=== 第 %d 手打完、仍门清时：对子数 × 财神数 → 最终结果 ===" % args.turn)
    print("（对子=非财神的成对张数；6 表示 6+；财神 2 表示 2+；样本 <10 不显示比例）")
    keys = [(p, j) for p in range(2, 7) for j in range(3)]
    for p, j in keys:
        line = "%d对+%d白" % (p, j) if j < 2 else "%d对+2+白" % p
        cells = []
        for n in tracked:
            c = out[n][(p, j)]
            if c["n"] < 10:
                cells.append("%s: n=%d" % (n[:8], c["n"]))
            else:
                cells.append("%s: n=%d 胡%.0f%% 七对%.0f%% 分%+.1f" % (
                    n[:8], c["n"], 100 * c["win"] / c["n"], 100 * c["qd"] / c["n"], c["score"] / c["n"]))
        print("%-9s | %s" % (line, " | ".join(cells)))


if __name__ == "__main__":
    main()
