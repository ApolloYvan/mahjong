"""财飘 / 杠 / 七对：产生这几种番的决策点上，高手 vs 我们 怎么选、选完结果如何。

    python3 tools/fan_decisions.py

A 财飘：摸牌前手上已是爆头听（摸啥都胡）、且打一张财神后仍爆头 → 飘 / 直接胡 的比例；飘了之后最终胡率、胡时番。
        按「本局已飘次数」× 剩余可摸张数 分格。
B 杠：  能暗杠（摸后手里 4 张同种）/ 补杠（摸到自己碰过的牌）/ 明杠（别家打出、手里有 3 张）时，杠了多少。
C 七对：起手（不含财神）对子数 4 / 5 / 6+ 的局，最终以七对胡的比例、总胜率、平均得分。
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
from mj.rules import baotou  # noqa: E402
from mj.tiles import TILE_INDEX, to_counts  # noqa: E402

JOKER = "白"
J = TILE_INDEX[JOKER]
_RECENT = set()


def _init(recent):
    _RECENT.update(recent)


def _bucket(left):
    return "40+" if left >= 40 else ("20-39" if left >= 20 else ("8-19" if left >= 8 else "<8"))


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
        piaos = [0] * 4
        pend_piao = {}          # seat -> 记录（等本人下一步）
        pend_gang = {}          # seat -> (kind, tile, bucket)
        piao_open = {}          # seat -> 已飘记录（等本局结果）
        detail, winner, fan = [], None, 0
        rows = []
        try:
            for ev in rnd["events"]:
                kind, seat, tile = ev["type"], ev.get("seat"), ev.get("tile")
                data = ev.get("data") or {}
                if kind == "tile_drawn":
                    for s in [x for x in pend_gang if pend_gang[x][0] == "明杠"]:
                        k, t, b = pend_gang.pop(s)
                        rows.append(("gang", groups[s], k, b, False))
                    before = tuple(to_counts(hands[seat]))
                    hands[seat].append(tile)
                    wall -= 1
                    if seat in groups:
                        b = _bucket(wall - 20)
                        if before[J] and baotou(before, melds[seat]):
                            c = list(to_counts(hands[seat]))
                            c[J] -= 1
                            if tile == JOKER or baotou(tuple(c), melds[seat]):
                                pend_piao[seat] = (min(piaos[seat], 2), b)
                        if tile != JOKER and hands[seat].count(tile) == 4:
                            pend_gang[seat] = ("暗杠", tile, b)
                        elif tile in pengs[seat]:
                            pend_gang[seat] = ("补杠", tile, b)
                elif kind == "tile_discarded":
                    if seat in pend_piao:
                        ch, b = pend_piao.pop(seat)
                        if tile == JOKER:
                            piao_open.setdefault(seat, []).append((ch, b))
                            rows.append(("piao_sel", groups[seat], ch, b, True))
                        else:
                            rows.append(("piao_sel", groups[seat], ch, b, None))     # 打了别的（少见）
                    if seat in pend_gang:
                        k, t, b = pend_gang.pop(seat)
                        rows.append(("gang", groups[seat], k, b, False))
                    hands[seat].remove(tile)
                    if tile == JOKER:
                        piaos[seat] += 1
                    for s in groups:
                        if s != seat and tile != JOKER and hands[s].count(tile) == 3:
                            pend_gang[s] = ("明杠", tile, _bucket(wall - 20))
                elif kind in ("chi", "peng", "gang"):
                    if kind == "gang" and seat in pend_gang:
                        k, t, b = pend_gang.pop(seat)
                        rows.append(("gang", groups[seat], k, b, True))
                    if kind == "gang":
                        pend_piao.pop(seat, None)
                    for s in [x for x in pend_gang if pend_gang[x][0] == "明杠"]:
                        k, t, b = pend_gang.pop(s)
                        rows.append(("gang", groups[s], k, b, False))
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
                elif kind == "round_ended":
                    detail = data.get("detail") or []
                    fan = data.get("fan") or 0
                    sc = data.get("scores") or []
                    if not data.get("draw") and len(sc) == 4 and max(sc) > 0:
                        winner = max(range(4), key=lambda s: sc[s])
        except (ValueError, KeyError, IndexError, TypeError):
            continue
        for s, (ch, b) in pend_piao.items():
            if winner == s:
                rows.append(("piao_sel", groups[s], ch, b, False))                  # 直接胡
        for s, lst in piao_open.items():
            for ch, b in lst:
                rows.append(("piao_res", groups[s], ch, b, (winner == s, fan if winner == s else 0)))
        for s in groups:
            pairs = sum(1 for i, v in enumerate(to_counts(rnd["start_hands"][s])) if i != J and v >= 2)
            if pairs >= 4:
                won = winner == s
                qd = won and any("七对" in x for x in detail)
                rows.append(("qidui", groups[s], min(pairs, 6), None,
                             (won, qd, (meta.get("scores") or [0] * 4)[s])))
        out += rows
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
    G = ("高手", "我们(当前)", "我们(以前)")

    sel = defaultdict(lambda: [0, 0, 0])      # (g, ch, b) -> [飘, 直接胡, 其他]
    res = defaultdict(lambda: [0, 0, 0])      # (g, ch) -> [n, 胡, 番和]
    gang = defaultdict(lambda: [0, 0])
    qd = defaultdict(lambda: [0, 0, 0, 0.0])
    for kind, g, a, b, v in rows:
        if kind == "piao_sel":
            for key in ((g, a, b), (g, a, "全部")):
                sel[key][0 if v else (1 if v is False else 2)] += 1
        elif kind == "piao_res":
            r = res[(g, a)]
            r[0] += 1
            r[1] += v[0]
            r[2] += v[1]
        elif kind == "gang":
            for key in ((g, a, b), (g, a, "全部")):
                gang[key][0] += 1
                gang[key][1] += v
        elif kind == "qidui":
            q = qd[(g, a)]
            q[0] += 1
            q[1] += v[0]
            q[2] += v[1]
            q[3] += v[2]

    buckets = ("40+", "20-39", "8-19", "<8", "全部")
    print("\n=== A 财飘：爆头听摸牌、飘后仍爆头时  点数 / 飘的比例（剩余可摸张数）===")
    print("%-8s %-10s " % ("已飘次数", "组") + " ".join("%13s" % b for b in buckets))
    for ch in (0, 1, 2):
        for g in G:
            cells = []
            for b in buckets:
                p, h, o = sel.get((g, ch, b), (0, 0, 0))
                n = p + h + o
                cells.append("%4d %6.1f%%" % (n, 100.0 * p / n) if n >= 5 else "%13s" % "-")
            if any(c.strip() != "-" for c in cells):
                print("%-8s %-10s " % (ch, g) + " ".join(cells))
        print()
    print("飘了之后：最终胡率 / 胡时平均番")
    for ch in (0, 1, 2):
        for g in G:
            n, w, f = res.get((g, ch), (0, 0, 0))
            if n >= 5:
                print("  已飘%d次  %-10s 飘 %4d 次  最终胡 %5.1f%%  胡时平均番 %.2f" % (
                    ch, g, n, 100.0 * w / n, f / w if w else 0))

    print("\n=== B 杠：能杠时 点数 / 杠的比例（剩余可摸张数）===")
    print("%-6s %-10s " % ("类型", "组") + " ".join("%13s" % b for b in buckets))
    for k in ("暗杠", "补杠", "明杠"):
        for g in G:
            cells = []
            for b in buckets:
                n, t = gang.get((g, k, b), (0, 0))
                cells.append("%4d %6.1f%%" % (n, 100.0 * t / n) if n >= 5 else "%13s" % "-")
            print("%-6s %-10s " % (k, g) + " ".join(cells))
        print()

    print("=== C 七对：起手对子数（不含财神）≥4 的局 ===")
    print("%-6s %-10s %6s %8s %8s %9s" % ("对子", "组", "局数", "七对胡", "总胜率", "平均得分"))
    for p in (4, 5, 6):
        for g in G:
            n, w, q, s = qd.get((g, p), (0, 0, 0, 0.0))
            if n >= 10:
                print("%-6s %-10s %6d %7.1f%% %7.1f%% %+9.2f" % ("6+" if p == 6 else p, g, n, 100.0 * q / n, 100.0 * w / n, s / n))
        print()
    print("读法：同一格里我们的选择比例和高手差得多、且高手那样选之后结果更好（飘后胡率/番、七对局得分），就是要改的决策。")


if __name__ == "__main__":
    main()
