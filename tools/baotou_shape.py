# -*- coding: utf-8 -*-
"""爆头手长什么样：把高手的爆头手和我们的听牌手摊开，逐个结构特征比。

为什么要这个：番/胡 的差距 100% 来自"胡牌里爆头占比"（我们 9~13%、高手 25%），
而 tools/joker_playbook.py 显示**即使手上财神数和副露组数完全相同**，我们也落后
3~40 倍（1白1露 我们 1% vs 歪比巴卜 16%；2白0露 我们 2% vs 80%）。
既然原料一样、产出差一个数量级，差别只能在**其余那几张牌的形状**里。

本工具在胡牌前一刻（13 张的稳定态）提取结构特征，按
(玩家 × 财神数 × 副露组数 × 是否爆头) 分组求均值。只读 events，不联网。

    python3 tools/baotou_shape.py
    python3 tools/baotou_shape.py --cell 1,1      # 只看 1财神/1副露 那一格
"""
import argparse
import glob
import json
import os
import sys
from collections import defaultdict
from multiprocessing import Pool

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mj.joker_ev import hand_wait_cover
from mj.rules import baotou, seven_pairs, win_standard
from mj.tiles import TILE_INDEX, to_counts

OUR_UID = "u_fd06550b5fb3"
JOKER = "白"
# 本地语料里 分/局 明显为正、且样本 >=300 局的人
TOP = ("腾蛇-0638", "牛来", "面包猪", "歪比巴卜", "Astra-0", "两脚离地",
       "算我求您了", "爆头研究所", "双白平胡", "嘎达嘎达")


def merge_rounds(game):
    by = {}
    for block in game.get("blocks") or []:
        rno = block.get("round_no")
        item = by.setdefault(rno, dict(round_no=rno, start_hands=None, events=[]))
        hands = block.get("start_hands")
        if hands and all(hands):
            item["start_hands"] = hands
        item["events"].extend(block.get("events") or [])
    out = []
    for rno in sorted(by, key=lambda x: (x is None, x)):
        by[rno]["events"].sort(key=lambda e: e.get("seq", 0))
        out.append(by[rno])
    return out


def features(tiles, meld_groups):
    """13 张手牌（含财神）的结构特征。"""
    counts = to_counts(tiles)
    jokers = counts[TILE_INDEX[JOKER]]
    pairs = sum(1 for i in range(34) if counts[i] == 2 and i != TILE_INDEX[JOKER])
    triplets = sum(1 for i in range(34) if counts[i] >= 3 and i != TILE_INDEX[JOKER])
    honors = sum(counts[27:])
    terminals = sum(counts[i] for i in range(27) if i % 9 in (0, 8))
    middles = sum(counts[i] for i in range(27) if 2 <= i % 9 <= 6)
    lone = 0
    for i in range(27):
        if counts[i] != 1:
            continue
        near = 0
        for d in (-2, -1, 1, 2):
            j = i + d
            if 0 <= j < 27 and j // 9 == i // 9:
                near += counts[j]
        if not near:
            lone += 1
    for i in range(27, 34):
        if counts[i] == 1 and i != TILE_INDEX[JOKER]:
            lone += 1
    hit, total = hand_wait_cover(tuple(counts), meld_groups)
    return {
        "听口/34": 34.0 * hit / max(1, total),
        "对子": pairs, "刻子": triplets, "孤张": lone,
        "字牌": honors - jokers, "幺九": terminals, "中张": middles,
    }


ORDER = ("听口/34", "对子", "刻子", "孤张", "字牌", "幺九", "中张")


def scan(path):
    try:
        game = json.load(open(path, encoding="utf-8"))
    except (ValueError, OSError):
        return []
    seats = game.get("seats") or []
    if len(seats) != 4:
        return []
    uids = [s.get("user_id") for s in seats]
    names = [s.get("name") or "?" for s in seats]
    results = {r.get("round_no"): r for r in (game.get("rounds") or [])}
    out = []
    for rnd in merge_rounds(game):
        info = results.get(rnd["round_no"])
        if not info or info.get("is_draw") or not rnd["start_hands"]:
            continue
        winner = info.get("winner")
        if winner is None:
            continue
        hands = [list(h) for h in rnd["start_hands"]]
        melds = [0] * 4
        final = None
        try:
            for event in rnd["events"]:
                kind, seat, tile = event.get("type"), event.get("seat"), event.get("tile")
                data = event.get("data") or {}
                if kind == "tile_drawn":
                    if seat == winner:
                        final = (list(hands[seat]), melds[seat], tile)
                    hands[seat].append(tile)
                elif kind == "tile_discarded":
                    hands[seat].remove(tile)
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
                    for _ in range({"an": 4, "ming": 3, "bu": 1}[data.get("kind")]):
                        hands[seat].remove(tile)
                    if data.get("kind") != "bu":
                        melds[seat] += 1
        except (ValueError, KeyError, TypeError):
            continue
        if not final:
            continue
        hand13, groups, _drawn = final          # 摸牌**前**的 13 张稳定态
        if len(hand13) + 3 * groups != 13:
            continue
        counts13 = tuple(to_counts(hand13))
        bt = baotou(counts13, groups)
        out.append((uids[winner], names[winner], hand13.count(JOKER), groups, bool(bt),
                    features(hand13, groups)))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--events", nargs="*",
                    default=["models/events/*.json", "tools/models/events/*.json"])
    ap.add_argument("--cell", default="1,1", help="只细看「财神数,副露组数」这一格")
    ap.add_argument("--jobs", type=int, default=6)
    a = ap.parse_args()

    files, seen = [], set()
    for pattern in a.events:
        for path in sorted(glob.glob(pattern)):
            name = os.path.basename(path)
            if name not in seen:
                seen.add(name)
                files.append(path)
    with Pool(a.jobs) as pool:
        rows = [r for sub in pool.map(scan, files, chunksize=4) for r in sub]

    def side(uid, name):
        if uid == OUR_UID:
            return "我们"
        return "高手" if any(t in (name or "") for t in TOP) else None

    agg = defaultdict(lambda: [0, defaultdict(float)])
    for uid, name, jk, groups, bt, feat in rows:
        who = side(uid, name)
        if who is None:
            continue
        for key in (("全部", who, bt), ("%d白%d露" % (min(jk, 2), min(groups, 2)), who, bt)):
            box = agg[key]
            box[0] += 1
            for k, v in feat.items():
                box[1][k] += v

    def show(title, keys):
        print("\n=== %s ===" % title)
        print("%-18s %6s  %s" % ("", "手数", "  ".join("%-8s" % k for k in ORDER)))
        for key in keys:
            n, tot = agg[key]
            if n < 12:
                continue
            label = "%s %s %s" % (key[0], key[1], "爆头" if key[2] else "非爆头")
            print("%-18s %6d  %s" % (label, n,
                                     "  ".join("%-8.2f" % (tot[k] / n) for k in ORDER)))

    show("全部胡牌：爆头手 vs 非爆头手",
         [("全部", w, b) for w in ("高手", "我们") for b in (True, False)])
    try:
        jk, gp = [int(x) for x in a.cell.split(",")]
        cell = "%d白%d露" % (jk, gp)
    except ValueError:
        cell = "1白1露"
    show("同一格 %s：原料相同，形状差在哪" % cell,
         [(cell, w, b) for w in ("高手", "我们") for b in (True, False)])

    print("""
读法：同一行内比「高手爆头」和「我们爆头」——形状一样说明我们能做成的时候做得对；
再比「高手爆头」和「我们非爆头」——那才是我们**该做成却没做成**的手，差在哪一列，
就是评分函数缺哪一项。「听口/34」是折算到 34 张的听口宽度，爆头 = 34。""")


if __name__ == "__main__":
    main()
