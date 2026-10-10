"""顶级高手逐局回放：在高手的每个决策点，用我们生产版（models/weights.json）走同一个局面，对比选择。

    python3 tools/top_replay.py --uids u_fcbd1c02a686,u_d0680514ca1b,u_b2aa6abe7811,u_d0668704e7a9 \
        --out reports/top_replay_1010.txt

对局文件有四家起手牌和全部事件 → 可还原高手每一步的完整手牌（只用他当时能看到的公开信息 + 他自己的手牌）。
对比四类决策：
  出牌：摸牌后他打了哪张 vs 我们会打哪张（不同的话，记双方打完后的向听/进张/是否爆头听/财神数）
  胡：  他能胡时胡/不胡 vs 我们胡/不胡/杠
  碰：  别人打出他手里有对子的牌，他碰没碰 vs 我们碰不碰
  吃：  上家打出他能吃的牌，他吃没吃 vs 我们吃不吃
抓打圈（有人打出财神后受限）期间的决策跳过，免得把强制出牌算成选择。
"""
import argparse
import glob
import json
import os
import sys
from collections import Counter, defaultdict
from multiprocessing import Pool

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path[:0] = [ROOT, os.path.join(ROOT, "tools")]
os.environ["MJ_WEIGHTS_NO_FILE"] = "0"

from mining_common import OUR_UID, discover_files, merge_rounds  # noqa: E402
from mj import bot  # noqa: E402
from mj.responses import choose_chi, choose_peng  # noqa: E402
from mj.rules import _wins_any, baotou  # noqa: E402
from mj.shanten import combined_route, route_shanten, ukeire  # noqa: E402
from mj.tiles import INDEX_TILE, TILE_INDEX, to_counts  # noqa: E402

J = "白"
_T = set()


def _init(targets):
    _T.update(targets)


def _snap(hand, seat, dealer, melds, rivers, wall, round_no, drawn=None, window=None, phase="draw", turn=None):
    return {"my_hand": list(hand), "seat": seat, "dealer": dealer, "melds": [list(m) for m in melds],
            "discards": [list(r) for r in rivers], "wall_remaining": wall, "round_no": round_no,
            "drawn_tile": drawn or "", "phase": phase, "turn": seat if turn is None else turn,
            "window_tile": window, "last_discard": window, "responding_seats": [seat], "god": {}}


def _after(hand, t, m):
    h = list(hand)
    h.remove(t)
    c = tuple(to_counts(h))
    sh, _ = combined_route(c, m)
    return {"sh": sh, "uk": len(ukeire(c, m)), "bt": baotou(c, m) if sh == 0 else False, "j": h.count(J)}


def scan(path):
    out = []
    try:
        with open(path, encoding="utf-8") as f:
            g = json.load(f)
    except (OSError, ValueError):
        return out
    uids = [s.get("user_id") for s in g.get("seats") or []]
    names = [s.get("name") or "" for s in g.get("seats") or []]
    if len(uids) != 4 or OUR_UID not in uids or not (_T & set(uids)):
        return out
    info = {r.get("round_no"): r for r in g.get("rounds") or []}
    for rnd in merge_rounds(g):
        hs = rnd.get("start_hands")
        meta = info.get(rnd["round_no"]) or {}
        if not hs or len(hs) != 4 or not all(hs) or rnd.get("truncated"):
            continue
        dealer = meta.get("dealer", rnd.get("dealer"))
        hand = [list(h) for h in hs]
        melds = [[] for _ in range(4)]
        rivers = [[] for _ in range(4)]
        wall = 83
        catch_until = None          # 抓打圈：有人打出财神后，到他下一次摸牌为止都跳过
        ev = rnd["events"]
        end = next((e for e in ev if e.get("type") == "round_ended"), None)
        d_end = (end or {}).get("data") or {}
        winner = None if d_end.get("draw") else (end or {}).get("seat")
        fan = d_end.get("fan") or 0
        recs = []
        try:
            for i, e in enumerate(ev):
                k, s, t = e["type"], e.get("seat"), e.get("tile")
                d = e.get("data") or {}
                if k == "round_ended":
                    break
                if k == "tile_drawn":
                    wall -= 1
                    hand[s].append(t)
                    if catch_until == s:
                        catch_until = None
                    if uids[s] in _T and catch_until is None:
                        nxt = next((y for y in ev[i + 1:] if y.get("seat") == s or y["type"] == "round_ended"), None)
                        if nxt is None:
                            continue
                        mg = len(melds[s])
                        snap = _snap(hand[s], s, dealer, melds, rivers, wall, rnd["round_no"], drawn=t)
                        ours = bot._choose_action_production(snap) or {}
                        if nxt["type"] == "round_ended" and nxt.get("seat") == s:
                            theirs = {"action": "hu"}
                        elif nxt["type"] == "gang":
                            theirs = {"action": "gang", "tile": nxt.get("tile")}
                        elif nxt["type"] == "tile_discarded":
                            theirs = {"action": "discard", "tile": nxt.get("tile")}
                        else:
                            continue
                        can_hu = _wins_any(tuple(to_counts(hand[s])), mg)
                        r = {"kind": "draw", "uid": uids[s], "name": names[s], "seat": s, "d": s == dealer,
                             "j": hand[s].count(J), "mg": mg, "wall": wall, "can_hu": can_hu,
                             "theirs": theirs, "ours": {"action": ours.get("action"), "tile": ours.get("tile")},
                             "hand": "".join(sorted(hand[s])), "drawn": t}
                        if theirs["action"] == "discard" and ours.get("action") == "discard" \
                                and theirs["tile"] != ours.get("tile"):
                            r["a_theirs"] = _after(hand[s], theirs["tile"], mg)
                            r["a_ours"] = _after(hand[s], ours["tile"], mg)
                        recs.append(r)
                elif k == "tile_discarded":
                    hand[s].remove(t)
                    rivers[s].append(t)
                    if t == J:
                        catch_until = s
                    if catch_until is None and t != J:
                        for o in range(4):
                            if o == s or uids[o] not in _T:
                                continue
                            window = ev[i + 1:i + 13]
                            took = lambda kind: any(y["type"] == kind and y.get("seat") == o for y in window  # noqa: E731
                                                    if y["type"] in ("pass", "peng", "chi", "gang", "timeout"))
                            if hand[o].count(t) >= 2:
                                snap = _snap(hand[o], o, dealer, melds, rivers, wall, rnd["round_no"],
                                             window=t, phase="response_peng", turn=s)
                                ours = bool(choose_peng(snap))
                                b = route_shanten(tuple(to_counts(hand[o])), len(melds[o]))
                                h2 = list(hand[o])
                                h2.remove(t)
                                h2.remove(t)
                                # 碰完要再打一张：取最优的那张后的向听
                                a = min(route_shanten(tuple(to_counts([x for j2, x in enumerate(h2) if j2 != q])),
                                                      len(melds[o]) + 1) for q in range(len(h2)))
                                recs.append({"kind": "peng", "uid": uids[o], "name": names[o], "seat": o,
                                             "d": o == dealer, "j": hand[o].count(J), "mg": len(melds[o]),
                                             "theirs": took("peng") or took("gang"), "ours": ours,
                                             "imp": a < b, "wall": wall})
                            if o == (s + 1) % 4 and len([m for m in melds[o] if m["kind"] == "chi"]) < 2:
                                snap = _snap(hand[o], o, dealer, melds, rivers, wall, rnd["round_no"],
                                             window=t, phase="response_chi", turn=s)
                                ours_chi = choose_chi(snap)
                                idx = TILE_INDEX.get(t)
                                can = idx is not None and idx < 27 and any(
                                    all(0 <= idx + x < 27 and (idx + x) // 9 == idx // 9 and
                                        INDEX_TILE[idx + x] in hand[o] for x in offs)
                                    for offs in ((-2, -1), (-1, 1), (1, 2)))
                                if can:
                                    recs.append({"kind": "chi", "uid": uids[o], "name": names[o], "seat": o,
                                                 "d": o == dealer, "j": hand[o].count(J), "mg": len(melds[o]),
                                                 "theirs": took("chi"), "ours": bool(ours_chi), "wall": wall})
                elif k == "peng":
                    hand[s].remove(t)
                    hand[s].remove(t)
                    melds[s].append({"kind": "peng", "tiles": [t, t, t]})
                elif k == "chi":
                    used = list(d.get("tiles") or [])
                    melds[s].append({"kind": "chi", "tiles": list(used)})
                    used.remove(t)
                    for u in used:
                        hand[s].remove(u)
                elif k == "gang":
                    kind = d.get("kind")
                    for _ in range({"an": 4, "ming": 3, "bu": 1}[kind]):
                        hand[s].remove(t)
                    if kind == "bu":
                        for m in melds[s]:
                            if m["kind"] == "peng" and m["tiles"][0] == t:
                                m["kind"], m["tiles"] = "gang", [t] * 4
                    else:
                        melds[s].append({"kind": "gang", "tiles": [t] * 4})
        except (ValueError, KeyError, IndexError, TypeError):
            continue
        for r in recs:
            r.update(room=os.path.basename(path)[:22], round=rnd["round_no"], won=winner == r["seat"],
                     fan=fan if winner == r["seat"] else 0, score=(meta.get("scores") or [0] * 4)[r["seat"]])
        out += recs
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--uids", required=True)
    ap.add_argument("--jobs", type=int, default=6)
    ap.add_argument("--out", default="reports/top_replay.txt")
    ap.add_argument("--dump", default="reports/top_replay_recs.jsonl")
    args = ap.parse_args()
    targets = args.uids.split(",")
    files = [p for p in discover_files() if any(u in open(p, encoding="utf-8").read() for u in targets)]
    print("相关对局文件 %d 个" % len(files), file=sys.stderr)
    recs = []
    with Pool(args.jobs, initializer=_init, initargs=(targets,)) as pool:
        for n, part in enumerate(pool.imap_unordered(scan, files, chunksize=4), 1):
            recs += part
            if n % 100 == 0:
                print("  已回放 %d/%d" % (n, len(files)), file=sys.stderr, flush=True)
    with open(args.dump, "w", encoding="utf-8") as f:
        for r in recs:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    print("决策点 %d 个 -> %s" % (len(recs), args.dump), file=sys.stderr)


if __name__ == "__main__":
    main()
