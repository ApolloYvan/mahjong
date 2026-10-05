"""庄家 vs 闲家、起手无财神的局：高手和我们在吃碰积极性、副露时机、听牌速度上差在哪。

    python3 tools/dealer_study.py

分组：高手 / 我们(当前)（--since 之后开打的房）/ 我们(以前)；每组再分 庄 / 闲，只看起手 0 财神的局。
A 局面结果：局数、胜率、胡在第几摸、胡时副露数、首次副露在第几手、首次听牌在第几手（没听牌的局不计）
B 吃碰窗口：能碰 / 能吃（上家打出、吃摊 <2）时接受的比例，按「吃碰后向听是否变好」拆
   —— 只算本人明确表态（吃/碰 或 显式 pass）的窗口；被别家先抢、超时代打的跳过。
   服务端先开碰窗口（所有人）、再开吃窗口（下家）：碰看本人窗口内第 1 个动作；吃看是否吃了，
   没吃则要求本人在窗口内至少 2 个动作且最后一个是显式 pass（只在碰窗口 pass 的不算"不吃"）。
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
from mj.tiles import INDEX_TILE, TILE_INDEX, to_counts  # noqa: E402

JOKER = "白"
RESPONSE_TYPES = ("pass", "timeout", "chi", "peng", "gang")
_RECENT = set()


def _init(recent):
    _RECENT.update(recent)


def _sh(tiles, melds):
    return route_shanten(tuple(to_counts(tiles)), melds)


def _after_claim(hand, take, melds):
    """吃/碰 take（手里拿出的两张）后，再打一张的最好向听。"""
    rest = list(hand)
    for t in take:
        rest.remove(t)
    best = 9
    for d in set(rest):
        left = list(rest)
        left.remove(d)
        best = min(best, _sh(left, melds + 1))
    return best


def _chi_takes(hand, tile):
    i = TILE_INDEX.get(tile)
    if i is None or i >= 27:
        return []
    out = []
    for offs in ((-2, -1), (-1, 1), (1, 2)):
        idx = [i + o for o in offs]
        if min(idx) < 0 or max(idx) >= 27 or any(k // 9 != i // 9 for k in idx):
            continue
        names = [INDEX_TILE[k] for k in idx]
        if all(hand.count(n) for n in names):
            out.append(names)
    return out


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
        dealer = meta.get("dealer", rnd.get("dealer"))
        track = {s: {"role": "庄" if s == dealer else "闲"} for s in groups if JOKER not in hands[s]}
        if not track:
            continue
        melds, chis, disc, draws = [0] * 4, [0] * 4, [0] * 4, [0] * 4
        events = rnd["events"]
        try:
            for idx, ev in enumerate(events):
                kind, seat, tile = ev["type"], ev.get("seat"), ev.get("tile")
                data = ev.get("data") or {}
                if kind == "tile_drawn":
                    hands[seat].append(tile)
                    draws[seat] += 1
                elif kind == "tile_discarded":
                    hands[seat].remove(tile)
                    disc[seat] += 1
                    t = track.get(seat)
                    if t is not None and "tenpai" not in t and _sh(hands[seat], melds[seat]) == 0:
                        t["tenpai"] = disc[seat]
                    if tile == JOKER or data.get("catch_play"):
                        continue
                    window = []
                    for ev2 in events[idx + 1:idx + 13]:
                        if ev2["type"] not in RESPONSE_TYPES:
                            break
                        window.append(ev2)
                    # 服务端先开碰窗口（所有人）再开吃窗口（下家）；显式 pass 不带窗口信息，
                    # 所以按每家在窗口里的动作顺序判：第 1 个动作属于碰窗口，其后的属于吃窗口。
                    seq = defaultdict(list)
                    for ev2 in window:
                        seq[ev2.get("seat")].append(ev2["type"])
                    claimed = [ev2 for ev2 in window if ev2["type"] in ("peng", "gang")]
                    for r, t in track.items():
                        acts = seq.get(r) or []
                        if r == seat or not acts:
                            continue
                        before = _sh(hands[r], melds[r])
                        if hands[r].count(tile) >= 2 and acts[0] in ("peng", "pass"):
                            better = _after_claim(hands[r], (tile, tile), melds[r]) < before
                            t.setdefault("claims", []).append(("碰", better, acts[0] == "peng"))
                        if r == (seat + 1) % 4 and chis[r] < 2 and not claimed:
                            took = "chi" in acts
                            declined = len(acts) >= 2 and acts[-1] == "pass"
                            takes = _chi_takes(hands[r], tile)
                            if takes and (took or declined):
                                better = min(_after_claim(hands[r], tk, melds[r]) for tk in takes) < before
                                t.setdefault("claims", []).append(("吃", better, took))
                elif kind in ("chi", "peng", "gang"):
                    if kind == "chi":
                        used = list(data.get("tiles") or [])
                        used.remove(tile)
                        for x in used:
                            hands[seat].remove(x)
                        chis[seat] += 1
                    elif kind == "peng":
                        for _ in range(2):
                            hands[seat].remove(tile)
                    else:
                        gk = data.get("kind")
                        for _ in range({"an": 4, "ming": 3, "bu": 1}[gk]):
                            hands[seat].remove(tile)
                        if gk == "bu":
                            continue
                    melds[seat] += 1
                    t = track.get(seat)
                    if t is not None and "first_meld" not in t:
                        t["first_meld"] = disc[seat] + 1
        except (ValueError, KeyError, IndexError, TypeError):
            continue
        winner = None if meta.get("is_draw") else meta.get("winner")
        for s, t in track.items():
            won = winner == s
            out.append((groups[s], t["role"], won, draws[s] if won else None, melds[s] if won else None,
                        t.get("first_meld"), t.get("tenpai"), t.get("claims") or []))
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
    st = defaultdict(lambda: defaultdict(float))
    cl = defaultdict(lambda: [0, 0])
    for g, role, won, wd, wm, fm, tp, claims in rows:
        x = st[(g, role)]
        x["n"] += 1
        x["won"] += won
        if won:
            x["wd"] += wd
            x["wm"] += wm
        if fm:
            x["fm_n"] += 1
            x["fm"] += fm
        if tp:
            x["tp_n"] += 1
            x["tp"] += tp
        for kind, better, took in claims:
            for key in ((g, role, kind, better), (g, role, kind, "全部")):
                cl[key][0] += 1
                cl[key][1] += took

    print("\n=== A 起手无财神：局面结果 ===")
    print("%-4s %-10s %6s %7s %9s %9s %10s %9s %9s" % (
        "角色", "组", "局数", "胜率", "胡在第几摸", "胡时副露", "有副露的局", "首副露手", "首听牌手"))
    for role in ("庄", "闲"):
        for g in G:
            x = st.get((g, role))
            if not x or x["n"] < 20:
                continue
            n, w = x["n"], x["won"]
            print("%-4s %-10s %6d %6.1f%% %9.1f %9.2f %9.1f%% %9.1f %9.1f" % (
                role, g, n, 100 * w / n, x["wd"] / w if w else 0, x["wm"] / w if w else 0,
                100 * x["fm_n"] / n, x["fm"] / x["fm_n"] if x["fm_n"] else 0, x["tp"] / x["tp_n"] if x["tp_n"] else 0))
        print()

    print("=== B 吃碰窗口：接受比例（点数）；「向听变好」= 吃碰后再打一张，向听比现在少 ===")
    print("%-4s %-4s %-10s %16s %16s %16s" % ("角色", "类型", "组", "向听变好", "向听不变/变差", "全部"))
    for role in ("庄", "闲"):
        for kind in ("碰", "吃"):
            for g in G:
                cells = []
                for b in (True, False, "全部"):
                    n, t = cl.get((g, role, kind, b), (0, 0))
                    cells.append("%5d %6.1f%%" % (n, 100 * t / n) if n >= 10 else "%12s" % "-")
                print("%-4s %-4s %-10s %16s %16s %16s" % (role, kind, g, *cells))
            print()
    print("读法：庄家那几行里，高手接受比例明显高于我们（尤其「向听变好」），而且 A 里高手首副露更早、胜率更高，")
    print("  说明高手当庄时更积极抢速度（庄家胡一次 24×番、别人胡庄家也多付）——那就是该改的吃碰门禁。")


if __name__ == "__main__":
    main()
