"""高手的财神套路反推：同样拿着财神，他们和我们做出什么不一样的牌。

背景：实测我们「财神/胡 1.21、持财神胡牌占比 86.1%」都是全场中上，但
「持财神时爆头率 18.7%」只有高手（30~36%）的一半。同样的原料，产出差一倍。
爆头是 ×2 番，而番/胡 +0.1 值 +0.272 分/局（比胜率 +1pp 还多）。

猜想：财神留着不用才是爆头的来源——留一张没被占用的财神能填任何缺口，
自然摸任意牌都胡；一旦把它并进面子去抢速度，听口就固定了。

本工具在胡牌那一刻拆开看：财神几张、副露几组、是否爆头，按玩家分档。
只读 events，不联网。

    python3 tools/joker_playbook.py            # 全部玩家
    python3 tools/joker_playbook.py --top 8    # 只看前 8 名 + 我们
"""
import argparse
import glob
import json
import os
import sys
from collections import defaultdict
from multiprocessing import Pool

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mj.rules import baotou, seven_pairs, win_standard
from mj.tiles import TILE_INDEX, to_counts

OUR_UID = "u_fd06550b5fb3"
JOKER = "白"


def merge_rounds(game):
    by = {}
    for block in game["blocks"]:
        rno = block["round_no"]
        item = by.setdefault(rno, dict(round_no=rno, start_hands=None, events=[]))
        hands = block.get("start_hands")
        if hands and all(hands):
            item["start_hands"] = hands
        item["events"].extend(block["events"])
    out = []
    for rno in sorted(by):
        by[rno]["events"].sort(key=lambda e: e["seq"])
        out.append(by[rno])
    return out


def scan(path):
    try:
        game = json.load(open(path, encoding="utf-8"))
    except (ValueError, OSError):
        return []
    uids = [s["user_id"] for s in game["seats"]]
    names = [s["name"] for s in game["seats"]]
    results = {r["round_no"]: r for r in game["rounds"]}
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
                kind, seat, tile = event["type"], event.get("seat"), event.get("tile")
                data = event.get("data") or {}
                if kind == "tile_drawn":
                    hands[seat].append(tile)
                    if seat == winner:
                        final = (list(hands[seat]), melds[seat], tile)
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
        hand, groups, drawn = final
        counts = tuple(to_counts(hand))
        if sum(counts) + 3 * groups != 14:
            continue
        if not (win_standard(counts, groups) or (groups == 0 and seven_pairs(counts) is not None)):
            continue
        counts13 = list(counts)
        counts13[TILE_INDEX[drawn]] -= 1
        out.append((uids[winner], names[winner], hand.count(JOKER), groups,
                    baotou(tuple(counts13), groups)))
    return out


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--events", nargs="*",
                        default=["models/events/*.json", "tools/models/events/*.json"])
    parser.add_argument("--top", type=int, default=8)
    parser.add_argument("--jobs", type=int, default=6)
    args = parser.parse_args()

    files, seen = [], set()
    for pattern in args.events:
        for path in sorted(glob.glob(pattern)):
            name = os.path.basename(path)
            if name not in seen:
                seen.add(name)
                files.append(path)

    with Pool(args.jobs) as pool:
        rows = [r for sub in pool.map(scan, files, chunksize=4) for r in sub]

    by_player = defaultdict(lambda: defaultdict(lambda: [0, 0]))   # uid -> (jokers, melds) -> [baotou, n]
    total = defaultdict(lambda: [0, 0, ""])
    for uid, name, jokers, groups, bt in rows:
        total[uid][0] += bt
        total[uid][1] += 1
        total[uid][2] = name
        by_player[uid][(min(jokers, 2), min(groups, 2))][0] += bt
        by_player[uid][(min(jokers, 2), min(groups, 2))][1] += 1

    ranked = sorted([(v[0] / v[1], uid) for uid, v in total.items() if v[1] >= 40], reverse=True)
    focus = [uid for _, uid in ranked[:args.top]]
    if OUR_UID not in focus:
        focus.append(OUR_UID)

    cells = [(j, m) for j in (0, 1, 2) for m in (0, 1, 2)]
    print("胡牌那一刻的爆头率，按 (手上财神数 × 副露组数) 拆分")
    print("列 = 财神0/副露0, 财神0/1, 财神0/2+, 财神1/0, 财神1/1, 财神1/2+, 财神2+/0, 财神2+/1, 财神2+/2+\n")
    print("%-16s %6s %7s  %s" % ("玩家", "胡牌", "总爆头", "  ".join("%d白%d露" % c for c in cells)))
    print("-" * 104)
    for uid in focus:
        if uid not in total or total[uid][1] < 40:
            continue
        bt, n, name = total[uid]
        line = []
        for cell in cells:
            hit, cnt = by_player[uid][cell]
            line.append("%5s" % ("%.0f%%" % (100 * hit / cnt) if cnt >= 8 else "-"))
        mark = "  <== 我们" if uid == OUR_UID else ""
        print("%-16s %6d %6.1f%%  %s%s" % (name[:14], n, 100 * bt / n, " ".join(line), mark))
    print("\n每格样本 <8 显示 '-'。若高手的优势集中在「财神多+副露少」那几格，")
    print("说明套路是「留着财神不用、不靠吃碰抢速度」——那正是 mj/joker_ev.py 管的事。")


if __name__ == "__main__":
    main()
