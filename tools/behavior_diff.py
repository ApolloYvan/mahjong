"""行为差异扫描：在完全相同的局面下，比较我们和对手的行动率。

设计意图（2026-09-23）：本项目历史上的改动几乎都是"调权重"，而按 build_hash
切分真实对局显示，九个代码版本没有一个显著优于其他——调参全在噪声里。唯一
一个被证据钉死的缺陷（摸财神拒胡，243:0）是靠"同一局面下我们做什么、对手做
什么"找到的。本工具把这个方法一次性做完：枚举所有可判定的决策机会，对每一类
输出 我们/对手 的执行率与 z 值，之后所有问题查表，不再为每个假设写一次性脚本。

只读 models/events/*.json，不联网，不写 logs/ 或 models/。

    python3 tools/behavior_diff.py                     # 全量扫描，落盘
    python3 tools/behavior_diff.py --events "models/events/a_xxx_*.json"
    python3 tools/behavior_diff.py --cache /tmp/bd.pkl --report   # 只读缓存出表
"""
import argparse
import glob
import json
import math
import os
import pickle
import sys
from collections import Counter, defaultdict
from multiprocessing import Pool

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mj.rules import baotou, seven_pairs, win_standard
from mj.shanten import shanten
from mj.tiles import JOKER, TILE_INDEX, to_counts

OUR_UID = "u_fd06550b5fb3"
WALL_START = 84          # 局内第 n 次摸牌时 wall_remaining = 84 - n（实测吻合）
WALL_TAIL = 20           # 最后 10 墩保留不摸；wall<=20 禁杠


class ReplayError(Exception):
    pass


def merge_rounds(game):
    """把同一局的分页 block 合并。

    坑：start_hands 缺失时是 [None]*4，在 Python 里是真值——直接
    `if b.get('start_hands')` 会让空块覆盖掉好块，可重放局数会从 1680 掉到 237，
    且掉下来的是系统性偏短的局。必须 all(sh)。"""
    by = {}
    for block in game["blocks"]:
        rno = block["round_no"]
        item = by.setdefault(rno, dict(round_no=rno, dealer=block["dealer"],
                                       start_hands=None, events=[]))
        hands = block.get("start_hands")
        if hands and all(hands):
            item["start_hands"] = hands
        item["events"].extend(block["events"])
    out = []
    for rno in sorted(by):
        by[rno]["events"].sort(key=lambda e: e["seq"])
        out.append(by[rno])
    return out


def _apply(hands, melds, chis, event):
    """把一个事件作用到手牌上。返回 (seat, kind) 或 None。"""
    kind = event["type"]
    seat = event.get("seat")
    tile = event.get("tile")
    data = event.get("data") or {}
    if kind == "tile_drawn":
        hands[seat].append(tile)
    elif kind == "tile_discarded":
        if tile not in hands[seat]:
            raise ReplayError("discard %s missing" % tile)
        hands[seat].remove(tile)
    elif kind == "chi":
        used = list(data.get("tiles") or [])
        if not used:
            raise ReplayError("chi without tiles")
        used.remove(tile)
        for t in used:
            if t not in hands[seat]:
                raise ReplayError("chi tile missing")
            hands[seat].remove(t)
        melds[seat] += 1
        chis[seat] += 1
    elif kind == "peng":
        for _ in range(2):
            if tile not in hands[seat]:
                raise ReplayError("peng tile missing")
            hands[seat].remove(tile)
        melds[seat] += 1
    elif kind == "gang":
        gkind = data.get("kind")
        take = {"an": 4, "ming": 3, "bu": 1}.get(gkind)
        if take is None:
            raise ReplayError("unknown gang kind %s" % gkind)
        for _ in range(take):
            if tile not in hands[seat]:
                raise ReplayError("gang tile missing")
            hands[seat].remove(tile)
        if gkind != "bu":
            melds[seat] += 1
    return seat, kind


def _is_win(hand, groups):
    counts = tuple(to_counts(hand))
    if sum(counts) + 3 * groups != 14:
        return False
    return win_standard(counts, groups) or (groups == 0 and seven_pairs(counts) is not None)


def _tenpai(hand, groups):
    counts = tuple(to_counts(hand))
    if sum(counts) + 3 * groups != 13:
        return False
    return shanten(counts, groups) == 0


def scan_game(path):
    """-> list of (is_me, opportunity_kind, taken, won_round)"""
    try:
        with open(path, encoding="utf-8") as source:
            game = json.load(source)
    except (OSError, ValueError):
        return []   # 采集中断留下的半截文件：跳过，不让整个扫描崩掉
    uids = [s["user_id"] for s in game["seats"]]
    results = {r["round_no"]: r for r in game["rounds"]}
    rows = []
    for rnd in merge_rounds(game):
        info = results.get(rnd["round_no"])
        if not info or not rnd["start_hands"] or info.get("is_draw"):
            continue
        hands = [list(h) for h in rnd["start_hands"]]
        melds = [0] * 4
        chis = [0] * 4
        pengs = [set() for _ in range(4)]
        events = rnd["events"]
        winner = info.get("winner")
        draws = 0
        seen = set()          # 机会去重：同一 (座位, 类型, 牌) 只算第一次

        def record(seat, kind, taken, key=None):
            if key is not None:
                if (seat, kind, key) in seen:
                    return
                seen.add((seat, kind, key))
            rows.append((uids[seat] == OUR_UID, kind, bool(taken), winner == seat))

        try:
            for i, event in enumerate(events):
                etype = event["type"]
                seat = event.get("seat")
                tile = event.get("tile")
                if etype == "peng" and seat is not None:
                    pengs[seat].add(tile)
                if etype == "gang" and seat is not None:
                    pengs[seat].discard(tile)

                if etype == "tile_drawn":
                    draws += 1
                    wall = WALL_START - draws
                    hands[seat].append(tile)
                    hand, groups = hands[seat], melds[seat]
                    # 该座位在本次摸牌后实际做了什么
                    nxt = None
                    for later in events[i + 1:]:
                        if later.get("seat") == seat and later["type"] in (
                                "tile_discarded", "gang"):
                            nxt = later
                            break
                        if later["type"] == "round_ended":
                            break
                    ended_here = (i + 1 < len(events)
                                  and events[i + 1]["type"] == "round_ended"
                                  and winner == seat)

                    if _is_win(hand, groups):
                        counts13 = list(to_counts(hand))
                        counts13[TILE_INDEX[tile]] -= 1
                        bt = baotou(tuple(counts13), groups)
                        if tile == JOKER:
                            record(seat, "摸财神可胡·爆头" if bt else "摸财神可胡·非爆头",
                                   ended_here)
                        else:
                            record(seat, "摸普通牌可胡", ended_here)

                    if wall > WALL_TAIL:
                        gk = (nxt.get("data") or {}).get("kind") if (
                            nxt is not None and nxt["type"] == "gang") else None
                        for t in set(hand):
                            if t == JOKER:
                                continue
                            if hand.count(t) >= 4:
                                record(seat, "暗杠机会", gk == "an" and nxt.get("tile") == t,
                                       key=("an", t))
                            if t in pengs[seat]:
                                record(seat, "补杠机会", gk == "bu" and nxt.get("tile") == t,
                                       key=("bu", t))
                    continue

                if etype == "tile_discarded":
                    # 这张牌打出后，其余三家各有什么机会、做了什么
                    actor = None
                    for later in events[i + 1:]:
                        if later["type"] in ("chi", "peng", "gang") and later.get("tile") == tile:
                            actor = later
                            break
                        if later["type"] in ("tile_drawn", "round_ended"):
                            break
                    for other in range(4):
                        if other == seat or tile == JOKER:
                            continue
                        hand, groups = hands[other], melds[other]
                        was_tenpai = _tenpai(hand, groups)
                        if hand.count(tile) >= 2:
                            took = (actor is not None and actor["type"] == "peng"
                                    and actor.get("seat") == other)
                            record(other, "碰机会·听牌中" if was_tenpai else "碰机会·未听牌", took)
                        if (other - seat) % 4 == 1 and chis[other] < 2 and TILE_INDEX[tile] < 27:
                            idx = TILE_INDEX[tile]
                            ok = False
                            for offs in ((-2, -1), (-1, 1), (1, 2)):
                                need = [idx + o for o in offs]
                                if min(need) < 0 or max(need) >= 27:
                                    continue
                                if any(n // 9 != idx // 9 for n in need):
                                    continue
                                from mj.tiles import INDEX_TILE
                                if all(hand.count(INDEX_TILE[n]) for n in need):
                                    ok = True
                                    break
                            if ok:
                                took = (actor is not None and actor["type"] == "chi"
                                        and actor.get("seat") == other)
                                record(other, "吃机会·听牌中" if was_tenpai else "吃机会·未听牌", took)

                _apply(hands, melds, chis, event) if etype not in ("tile_drawn",) else None
        except (ReplayError, KeyError, ValueError, IndexError):
            continue
    return rows


def report(rows):
    agg = defaultdict(lambda: defaultdict(lambda: [0, 0, 0]))  # kind -> me -> [taken, n, won_taken]
    won = defaultdict(lambda: defaultdict(lambda: [0, 0, 0, 0]))
    for is_me, kind, taken, w in rows:
        cell = agg[kind][is_me]
        cell[0] += taken
        cell[1] += 1
        bucket = won[kind][is_me]
        if taken:
            bucket[0] += w
            bucket[1] += 1
        else:
            bucket[2] += w
            bucket[3] += 1
    print("%-22s %-20s %-20s %-10s" % ("决策机会", "我们 执行率(n)", "对手 执行率(n)", "差 (z)"))
    print("-" * 78)
    for kind in sorted(agg, key=lambda k: -agg[k][True][1]):
        a, b = agg[kind][True], agg[kind][False]
        if a[1] < 10 or b[1] < 10:
            continue
        pa, pb = a[0] / a[1], b[0] / b[1]
        se = math.sqrt(pa * (1 - pa) / a[1] + pb * (1 - pb) / b[1])
        z = (pa - pb) / se if se else 0.0
        flag = "  <<<" if abs(z) >= 3 else ""
        print("%-22s %6.1f%% (%5d)      %6.1f%% (%5d)      %+6.2f%s"
              % (kind, 100 * pa, a[1], 100 * pb, b[1], z, flag))
    print()
    print("%-22s %-28s %-28s" % ("决策机会", "我们 胜率 做了/没做", "对手 胜率 做了/没做"))
    print("-" * 78)
    for kind in sorted(won, key=lambda k: -agg[k][True][1]):
        out = []
        for me in (True, False):
            wt, nt, wn, nn = won[kind][me]
            out.append("%s / %s" % ("%.1f%%(%d)" % (100 * wt / nt, nt) if nt >= 10 else "-",
                                    "%.1f%%(%d)" % (100 * wn / nn, nn) if nn >= 10 else "-"))
        if agg[kind][True][1] < 10:
            continue
        print("%-22s %-28s %-28s" % (kind, out[0], out[1]))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--events", default="models/events/*.json")
    parser.add_argument("--cache", default="/tmp/behavior_diff.pkl")
    parser.add_argument("--report", action="store_true", help="只从缓存出表，不重新扫描")
    parser.add_argument("--jobs", type=int, default=6)
    args = parser.parse_args()
    if args.report and os.path.exists(args.cache):
        rows = pickle.load(open(args.cache, "rb"))
    else:
        files = sorted(glob.glob(args.events))
        with Pool(args.jobs) as pool:
            rows = [r for sub in pool.map(scan_game, files, chunksize=2) for r in sub]
        pickle.dump(rows, open(args.cache, "wb"))
        print("扫描 %d 个对局文件，%d 条决策机会 -> %s\n" % (len(files), len(rows), args.cache))
    report(rows)


if __name__ == "__main__":
    main()
