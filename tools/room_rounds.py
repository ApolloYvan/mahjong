"""单房逐局对照：我们 vs 指定对手，每局一行（庄闲、起手财神/向听、首次听牌、吃碰、是否到过爆头听、能胡没胡、结果），
末尾按 庄/闲 汇总。用来回答"同一房里他做庄为什么比我们好"——先看是起手（运气）不同，还是同样起手下打得不同。

    python3 tools/room_rounds.py a_b892490a6e28 --rival "两脚离地了 病毒就关闭了"
"""
import argparse
import json
import os
import sys
from collections import defaultdict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path[:0] = [ROOT, os.path.join(ROOT, "tools")]

from latency_check import ab_labels  # noqa: E402
from mining_common import OUR_NAME, discover_files, merge_rounds, seat_names  # noqa: E402
from mj.rules import _wins_any, baotou  # noqa: E402
from mj.shanten import route_shanten, shanten  # noqa: E402
from mj.tiles import to_counts  # noqa: E402


def _seat_round(rnd, seat):
    hands = rnd["start_hands"]
    h = list(hands[seat])
    melds = draws = jd = 0
    ftn, bt, declined, pend, claims, to = None, False, 0, False, "", 0
    for ev in rnd["events"]:
        k, s, t = ev["type"], ev.get("seat"), ev.get("tile")
        d = ev.get("data") or {}
        if k == "round_ended":
            break
        if s != seat:
            continue
        if k == "tile_drawn":
            h.append(t)
            draws += 1
            jd += t == "白"
            pend = _wins_any(tuple(to_counts(h)), melds)
        elif k == "tile_discarded":
            declined += pend
            pend = False
            h.remove(t)
            if len(h) + 3 * melds == 13:
                c = tuple(to_counts(h))
                if ftn is None and route_shanten(c, melds) == 0:
                    ftn = draws
                bt = bt or baotou(c, melds)
        elif k in ("chi", "peng", "gang"):
            claims += k[0]
            if k == "chi":
                used = list(d.get("tiles") or [])
                used.remove(t)
                for x in used:
                    h.remove(x)
            elif k == "peng":
                h.remove(t)
                h.remove(t)
            else:
                for _ in range({"an": 4, "ming": 3, "bu": 1}[d.get("kind")]):
                    h.remove(t)
            if not (k == "gang" and d.get("kind") == "bu"):
                melds += 1
        elif k == "timeout" and d.get("kind") == "discard":
            to += 1
    return {"j": hands[seat].count("白"), "jd": jd, "sh": shanten(tuple(to_counts(hands[seat])), 0), "ftn": ftn,
            "bt": bt, "dec": declined, "claims": claims or "-", "to": to}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("room")
    ap.add_argument("--rival", required=True)
    args = ap.parse_args()
    label = ab_labels("2026-10-01", None).get(args.room)
    print("房间 %s  A/B 组：%s" % (args.room, label or "（非 A/B）"))
    agg = defaultdict(lambda: defaultdict(float))
    for p in sorted(x for x in discover_files() if os.path.basename(x).startswith(args.room)):
        with open(p, encoding="utf-8") as f:
            g = json.load(f)
        names = seat_names(g)
        if OUR_NAME not in names or args.rival not in names:
            continue
        seats = {"我们": names.index(OUR_NAME), "对手": names.index(args.rival)}
        info = {r.get("round_no"): r for r in g.get("rounds") or []}
        for rnd in merge_rounds(g):
            meta = info.get(rnd["round_no"]) or {}
            if not rnd.get("start_hands"):
                continue
            dealer, winner, scores = meta.get("dealer"), meta.get("winner"), meta.get("scores") or [0] * 4
            fan = next(((e.get("data") or {}).get("fan") for e in rnd["events"] if e.get("type") == "round_ended"), None)
            for who, s in seats.items():
                x = _seat_round(rnd, s)
                role = "庄" if dealer == s else "闲"
                print("%s r%d %s %s 起手白%d(+摸%d) 向听%d 首听%-4s 吃碰%-4s 爆头听%s 能胡没胡%d 超时%d | %s%s | %+d" % (
                    os.path.basename(p)[13:19], rnd["round_no"], who, role, x["j"], x["jd"], x["sh"],
                    x["ftn"] if x["ftn"] is not None else "-", x["claims"], "Y" if x["bt"] else "-", x["dec"], x["to"],
                    "胡" if winner == s else ("被%s胡" % (names[winner][:6] if winner is not None else "流")),
                    (" %s番" % fan) if winner is not None else "", scores[s]))
                a = agg[(who, role)]
                a["n"] += 1
                a["won"] += winner == s
                a["score"] += scores[s]
                a["j"] += x["j"]
                a["sh"] += x["sh"]
                a["tn"] += x["ftn"] is not None
                a["tn8"] += x["ftn"] is not None and x["ftn"] <= 8
                a["fan"] += fan if winner == s and fan else 0
            print()
    print("%-8s %4s %8s %7s %8s %8s %8s %8s %7s" % ("", "局数", "分/局", "胜率", "番/胡", "起手白", "起手向听", "听过牌", "8摸内听"))
    for who in ("我们", "对手"):
        for role in ("庄", "闲"):
            a = agg[(who, role)]
            n = a["n"] or 1
            print("%-8s %4d %+8.2f %6.1f%% %8.2f %8.2f %8.2f %7.1f%% %7.1f%%" % (
                who + role, a["n"], a["score"] / n, 100 * a["won"] / n, a["fan"] / max(1, a["won"]),
                a["j"] / n, a["sh"] / n, 100 * a["tn"] / n, 100 * a["tn8"] / n))


if __name__ == "__main__":
    main()
