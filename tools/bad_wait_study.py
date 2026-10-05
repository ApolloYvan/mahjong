"""烂听口要不要退听：无财神手、能听牌但最好的听口只有 1 种真牌时，高手退一向听的比例和结果。

    python3 tools/bad_wait_study.py

规则是只能自摸，听口宽度 = 胡率。我们的 choose_discard 先按向听砍层，结构上
永远不会从听牌退回一向听；policy_diff 显示高手在听牌点的分歧里有一部分正是退听。
同一局面（能听牌、听口种数、第几手）下比较高手和我们的最终胡率，就是这条规则的价值。
听口种数不含白（摸到财神在任何听牌形都能胡）。
"""
import argparse
import os
import sys
import json
from collections import Counter, defaultdict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))

from mining_common import discover_files, merge_rounds  # noqa: E402
from mining_common import seat_names  # noqa: E402
from mj.shanten import combined_route, route_shanten  # noqa: E402
from mj.tiles import JOKER_IDX, to_counts  # noqa: E402

OUR = "重生之我是雀神"
JOKER = "白"
MASTERS = ["⭐꧁༺🀆🀆🀆🀆༻꧂⭐", "Deepseek胡", "爆头研究所", "Astra-0", "晴总总，该请桂语山房了",
           "glm-flash", "铳一色14"]


def best_tenpai(hand, groups):
    """能听牌时返回 (最宽真听口种数, 该听口的真牌张数)；不能听牌返回 None。"""
    best = None
    for cand in set(hand):
        rest = list(hand)
        rest.remove(cand)
        counts = to_counts(rest)
        if route_shanten(counts, groups) != 0:
            continue
        waits = [i for i in combined_route(counts, groups)[1] if i != JOKER_IDX]
        live = sum(4 - counts[i] for i in waits)
        key = (len(waits), live)
        if best is None or key > best:
            best = key
    return best


def scan(path, masters, out):
    try:
        with open(path, encoding="utf-8") as source:
            game = json.load(source)
    except (OSError, ValueError):
        return
    names = seat_names(game)
    if len(names) != 4 or not any(n == OUR or n in masters for n in names):
        return
    info = {r.get("round_no"): r for r in game.get("rounds") or []}
    for rnd in merge_rounds(game):
        meta = info.get(rnd["round_no"])
        if not meta or rnd.get("truncated") or not rnd.get("start_hands"):
            continue
        hands = [list(h) for h in rnd["start_hands"]]
        melds = [0] * 4
        turns = [0] * 4
        points = []
        try:
            for ev in rnd["events"]:
                kind, seat, tile = ev["type"], ev.get("seat"), ev.get("tile")
                data = ev.get("data") or {}
                if kind == "tile_drawn":
                    hands[seat].append(tile)
                elif kind == "tile_discarded":
                    hand = hands[seat]
                    turns[seat] += 1
                    name = names[seat]
                    if ((name == OUR or name in masters) and JOKER not in hand
                            and len(hand) + 3 * melds[seat] == 14 and not data.get("catch_play")):
                        bt = best_tenpai(hand, melds[seat])
                        if bt is not None:
                            rest = list(hand)
                            rest.remove(tile)
                            broke = route_shanten(to_counts(rest), melds[seat]) > 0
                            points.append((seat, bt, broke, turns[seat]))
                    hand.remove(tile)
                elif kind == "chi":
                    used = list(data.get("tiles") or [])
                    used.remove(tile)
                    for t in used:
                        hands[seat].remove(t)
                    melds[seat] += 1
                elif kind == "peng":
                    for _ in range(2):
                        hands[seat].remove(tile)
                    melds[seat] += 1
                elif kind == "gang":
                    take = {"an": 4, "ming": 3, "bu": 1}[data.get("kind")]
                    for _ in range(take):
                        hands[seat].remove(tile)
                    if data.get("kind") != "bu":
                        melds[seat] += 1
        except (ValueError, KeyError, IndexError, TypeError):
            continue
        winner = None if meta.get("is_draw") else meta.get("winner")
        scores = meta.get("scores") or [0] * 4
        first = {}
        for seat, bt, broke, turn in points:
            if seat not in first:            # 每局每人只取第一次「能听牌」的点，避免重复计数
                first[seat] = (bt, broke, turn)
        for seat, (bt, broke, turn) in first.items():
            group = "我们" if names[seat] == OUR else "高手"
            wait = "1种" if bt[0] <= 1 else ("2种" if bt[0] == 2 else "3种+")
            if bt[0] == 0:
                wait = "0种(只听白)"
            phase = "早(≤6手)" if turn <= 6 else ("中(7-11)" if turn <= 11 else "晚(12+)")
            c = out[(group, wait, phase)]
            c["n"] += 1
            c["broke"] += broke
            c["win"] += winner == seat
            c["score"] += scores[seat]
            c["win_b" if broke else "win_k"] += winner == seat
            c["n_b" if broke else "n_k"] += 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--players", nargs="*", default=MASTERS)
    args = ap.parse_args()
    masters = set(args.players)
    out = defaultdict(Counter)
    files = discover_files()
    for i, path in enumerate(files, 1):
        scan(path, masters, out)
        if i % 250 == 0:
            print("  已扫 %d / %d" % (i, len(files)), flush=True)

    print("\n=== 无财神、第一次能听牌时：最宽听口种数 × 第几手 → 退听率与最终结果 ===")
    print("（退听 = 没选听牌，打成一向听；胡率/分为这一局最终结果；样本 <15 不显示比例）")
    print("%-12s %-9s | %-44s | %s" % ("听口", "时机", "高手", "我们"))
    for wait in ("0种(只听白)", "1种", "2种", "3种+"):
        for phase in ("早(≤6手)", "中(7-11)", "晚(12+)"):
            cells = []
            for group in ("高手", "我们"):
                c = out[(group, wait, phase)]
                if c["n"] < 15:
                    cells.append("n=%-5d%38s" % (c["n"], ""))
                    continue
                kb = ""
                if c["n_b"] >= 10 and c["n_k"] >= 10:
                    kb = " 退听胡%.0f%%/不退胡%.0f%%" % (100 * c["win_b"] / c["n_b"], 100 * c["win_k"] / c["n_k"])
                cells.append("n=%-5d 退听%3.0f%% 胡%3.0f%% 分%+5.1f%s" % (
                    c["n"], 100 * c["broke"] / c["n"], 100 * c["win"] / c["n"], c["score"] / c["n"], kb))
            print("%-12s %-9s | %s | %s" % (wait, phase, cells[0], cells[1]))


if __name__ == "__main__":
    main()
