"""杠的决策：高手只杠 53%~75%，我们几乎逢杠必杠——高手在什么情况下不杠、不杠的结果如何。

    python3 tools/gang_study.py

每个能杠的点算两种手的向听（都折算成 13 张口径、杠之前还没补牌）：
    不杠：暗杠/补杠 = 14 张里打掉最好的一张后的向听；明杠 = 现有手牌的向听
    杠后：拿掉杠出去的牌、副露 +1（补杠副露数不变）后的向听
    伤型 = 杠后向听 − 不杠向听（>0 表示杠了要拆牌型；杠后还有一次补牌，所以 0 = 白赚一摸）
按 类型 × 是否伤型 × 是否已听牌 分格：杠的比例，以及 杠/不杠 两种选择本局的胜率、平均得分（观察值，非因果）。
"""
import argparse
import json
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
from mj.shanten import route_shanten  # noqa: E402
from mj.tiles import to_counts  # noqa: E402

JOKER = "白"
_RECENT = set()


def _init(recent):
    _RECENT.update(recent)


def _sh(tiles, melds):
    return route_shanten(tuple(to_counts(tiles)), melds)


def _best14(tiles, melds):
    best = 9
    for t in set(tiles):
        rest = list(tiles)
        rest.remove(t)
        best = min(best, _sh(rest, melds))
    return best


def _features(kind, hand, tile, melds):
    """hand：暗/补杠为摸牌后 14 张口径；明杠为 13 张口径。"""
    rest = [x for x in hand if x != tile]
    if kind == "暗杠":
        nog = _best14(hand, melds)
        g = _sh(rest, melds + 1)
    elif kind == "补杠":
        nog = _best14(hand, melds)
        left = list(hand)
        left.remove(tile)
        g = _sh(left, melds)
    else:
        nog = _sh(hand, melds)
        g = _sh(rest, melds + 1)
    return min(max(g - nog, 0), 1), nog == 0


def scan(path):
    out = []
    try:
        with open(path, encoding="utf-8") as source:
            game = json.load(source)
    except (OSError, ValueError):
        return out
    names = seat_names(game)
    if len(names) != 4:
        return out
    room = game.get("room_id") or ""
    groups = {}
    for s, n in enumerate(names):
        if n in MASTERS:
            groups[s] = "高手"
        elif n == OUR:
            groups[s] = "我们(当前)" if room in _RECENT else "我们(以前)"
    if not groups:
        return out
    info = {r.get("round_no"): r for r in game.get("rounds") or []}
    for rnd in merge_rounds(game):
        meta = info.get(rnd["round_no"])
        if not meta or rnd.get("truncated") or not rnd.get("start_hands"):
            continue
        hands = [list(h) for h in rnd["start_hands"]]
        if not all(hands):
            continue
        wall = 136 - sum(len(h) for h in hands)
        pengs = [set() for _ in range(4)]
        melds = [0] * 4
        pend = {}
        recs = []

        def close(s, taken):
            k, t, feat = pend.pop(s)
            recs.append((s, k, feat, taken))

        try:
            for ev in rnd["events"]:
                kind, seat, tile = ev["type"], ev.get("seat"), ev.get("tile")
                data = ev.get("data") or {}
                if kind == "tile_drawn":
                    for s in [x for x in pend if pend[x][0] == "明杠"]:
                        close(s, False)
                    hands[seat].append(tile)
                    wall -= 1
                    if seat in groups and wall > 20:
                        if tile != JOKER and hands[seat].count(tile) == 4:
                            pend[seat] = ("暗杠", tile, _features("暗杠", hands[seat], tile, melds[seat]))
                        elif tile in pengs[seat]:
                            pend[seat] = ("补杠", tile, _features("补杠", hands[seat], tile, melds[seat]))
                elif kind == "tile_discarded":
                    if seat in pend:
                        close(seat, False)
                    hands[seat].remove(tile)
                    if wall > 20 and tile != JOKER:
                        for s in groups:
                            if s != seat and hands[s].count(tile) == 3:
                                pend[s] = ("明杠", tile, _features("明杠", hands[s], tile, melds[s]))
                elif kind in ("chi", "peng", "gang"):
                    if kind == "gang" and seat in pend:
                        close(seat, True)
                    for s in [x for x in pend if pend[x][0] == "明杠"]:
                        close(s, False)
                    if kind == "chi":
                        used = list(data.get("tiles") or [])
                        used.remove(tile)
                        for x in used:
                            hands[seat].remove(x)
                        melds[seat] += 1
                    elif kind == "peng":
                        for _ in range(2):
                            hands[seat].remove(tile)
                        pengs[seat].add(tile)
                        melds[seat] += 1
                    else:
                        gk = data.get("kind")
                        for _ in range({"an": 4, "ming": 3, "bu": 1}[gk]):
                            hands[seat].remove(tile)
                        if gk == "bu":
                            pengs[seat].discard(tile)
                        else:
                            melds[seat] += 1
        except (ValueError, KeyError, IndexError, TypeError):
            continue
        winner = None if meta.get("is_draw") else meta.get("winner")
        scores = meta.get("scores") or [0] * 4
        for s, k, (hurt, tenpai), taken in recs:
            out.append((groups[s], k, hurt, tenpai, taken, winner == s, scores[s]))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default="2026-09-25T15:30")
    ap.add_argument("--jobs", type=int, default=6)
    args = ap.parse_args()
    recent = recent_rooms(args.since)
    files = discover_files()
    rows = []
    with Pool(args.jobs, initializer=_init, initargs=(recent,)) as pool:
        for done, part in enumerate(pool.imap_unordered(scan, files, chunksize=8), 1):
            rows += part
            if done % 500 == 0 or done == len(files):
                print("  已扫 %d / %d" % (done, len(files)), flush=True)

    st = defaultdict(lambda: defaultdict(float))
    for g, k, hurt, tenpai, taken, won, score in rows:
        for key in ((g, k, hurt, tenpai), (g, k, "全部", "全部")):
            x = st[key]
            x["n"] += 1
            x["t"] += taken
            side = "g" if taken else "p"
            x[side + "n"] += 1
            x[side + "w"] += won
            x[side + "s"] += score

    def side(x, p):
        n = x[p + "n"]
        return "%4d %5.1f%% %+6.2f" % (n, 100 * x[p + "w"] / n, x[p + "s"] / n) if n >= 5 else "%18s" % "-"

    print("\n=== 能杠时：杠的比例；杠 / 不杠 两种选择本局的 次数 胜率 平均得分 ===")
    print("%-5s %-6s %-5s %-10s %5s %7s | %18s | %18s" % ("类型", "伤型", "已听", "组", "点数", "杠比例", "杠了", "没杠"))
    for k in ("暗杠", "补杠", "明杠"):
        for hurt, tenpai in ((0, False), (0, True), (1, False), (1, True), ("全部", "全部")):
            for g in ("高手", "我们(当前)", "我们(以前)"):
                x = st.get((g, k, hurt, tenpai))
                if not x or x["n"] < 5:
                    continue
                print("%-5s %-6s %-5s %-10s %5d %6.1f%% | %s | %s" % (
                    k, {0: "不伤", 1: "伤型"}.get(hurt, hurt), {False: "否", True: "是"}.get(tenpai, tenpai),
                    g, x["n"], 100 * x["t"] / x["n"], side(x, "g"), side(x, "p")))
        print()
    print("读法：高手在哪一格明显不杠（杠比例低）而我们照杠，且那一格高手「没杠」的得分不比「杠了」差，就是该改的门禁。")


if __name__ == "__main__":
    main()
