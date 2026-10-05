# -*- coding: utf-8 -*-
"""得分归因：高手的分**从哪来**、**往哪漏**、**什么时候拿大分**。

之前所有分析都停在"番/胡多少、胜率多少"这类平均量上，回答不了
"什么情况下得分、什么情况下扣分、什么情况下得高分"。本工具把每一分钱
拆到条件上：

  收入侧  按番数档 / 按副露组数 / 按胡牌快慢，各贡献多少百分比的总收入
  支出侧  我们的钱是被多大的牌拿走的（1番小牌零敲碎打，还是 4番+ 一次性放血）

    python3 tools/score_attribution.py
    python3 tools/score_attribution.py --top 6
"""
import argparse
import glob
import json
import os
import sys
from collections import defaultdict
from multiprocessing import Pool

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

OUR_UID = "u_fd06550b5fb3"
JOKER = "白"


def merge_rounds(game):
    by = {}
    for block in game.get("blocks") or []:
        rno = block.get("round_no")
        item = by.setdefault(rno, dict(round_no=rno, start_hands=None,
                                       dealer=block.get("dealer"), events=[]))
        hands = block.get("start_hands")
        if hands and all(hands):
            item["start_hands"] = hands
        item["events"].extend(block.get("events") or [])
    out = []
    for rno in sorted(by, key=lambda x: (x is None, x)):
        by[rno]["events"].sort(key=lambda e: e.get("seq", 0))
        out.append(by[rno])
    return out


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
        if not info or info.get("is_draw"):
            continue
        winner = info.get("winner")
        if winner is None:
            continue
        dealer = info.get("dealer", rnd.get("dealer"))
        scores = info.get("scores") or [0] * 4
        got = scores[winner] if winner < len(scores) else 0
        per = 24 if dealer == winner else 10
        fan = max(1, int(round(got / float(per)))) if got > 0 else 0
        melds = draws = 0
        for event in rnd["events"]:
            if event.get("seat") != winner:
                continue
            kind = event.get("type")
            if kind == "tile_drawn":
                draws += 1
            elif kind in ("chi", "peng"):
                melds += 1
            elif kind == "gang" and (event.get("data") or {}).get("kind") != "bu":
                melds += 1
        start = (rnd["start_hands"] or [[]] * 4)[winner] if rnd["start_hands"] else []
        out.append((uids, names, winner, fan, melds, draws,
                    list(start).count(JOKER), scores))
    return out


def _bucket_fan(fan):
    if fan >= 8:
        return "8番+"
    if fan >= 4:
        return "4番"
    if fan >= 2:
        return "2番"
    return "1番"


def _bucket_speed(draws):
    if draws <= 8:
        return "快(≤8巡)"
    if draws <= 14:
        return "中(9-14)"
    return "慢(15+)"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--events", nargs="*",
                    default=["models/events/*.json", "tools/models/events/*.json"])
    ap.add_argument("--top", type=int, default=6)
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

    stat = defaultdict(lambda: {
        "name": "?", "n": 0, "wins": 0, "income": 0.0, "outgo": 0.0,
        "fan": defaultdict(float), "meld": defaultdict(float),
        "speed": defaultdict(float), "fan_n": defaultdict(int),
        "lost_to": defaultdict(float),
    })
    for uids, names, winner, fan, melds, draws, _jk, scores in rows:
        wbucket = _bucket_fan(fan)
        for seat in range(4):
            item = stat[uids[seat]]
            item["name"] = names[seat]
            item["n"] += 1
            score = scores[seat] if seat < len(scores) else 0
            if seat == winner:
                item["wins"] += 1
                item["income"] += score
                item["fan"][wbucket] += score
                item["fan_n"][wbucket] += 1
                item["meld"]["%d露" % min(melds, 2)] += score
                item["speed"][_bucket_speed(draws)] += score
            else:
                item["outgo"] += -score
                item["lost_to"][wbucket] += -score

    ranked = sorted(((v["income"] - v["outgo"]) / v["n"], uid)
                    for uid, v in stat.items() if v["n"] >= 300)
    focus = [uid for _, uid in ranked[::-1][:a.top]]
    if OUR_UID in stat and OUR_UID not in focus:
        focus.append(OUR_UID)

    def pct(d, total):
        return "  ".join("%s %2.0f%%" % (k, 100 * d[k] / total) for k in sorted(d, key=lambda k: -d[k]))

    print("%-20s %6s %7s %9s %9s %9s" % ("玩家", "局数", "胜率", "收入/局", "支出/局", "净/局"))
    print("-" * 70)
    for uid in focus:
        v = stat[uid]
        mark = "  <== 我们" if uid == OUR_UID else ""
        print("%-20s %6d %6.1f%% %+9.3f %+9.3f %+9.3f%s"
              % (v["name"][:18], v["n"], 100.0 * v["wins"] / v["n"],
                 v["income"] / v["n"], -v["outgo"] / v["n"],
                 (v["income"] - v["outgo"]) / v["n"], mark))

    for title, key in (("收入构成：多少钱来自几番的牌", "fan"),
                       ("收入构成：多少钱来自几组副露", "meld"),
                       ("收入构成：多少钱来自快胡/慢胡", "speed"),
                       ("支出构成：钱是被多大的牌拿走的", "lost_to")):
        print("\n=== %s ===" % title)
        for uid in focus:
            v = stat[uid]
            total = sum(v[key].values())
            if not total:
                continue
            mark = "  <== 我们" if uid == OUR_UID else ""
            print("%-20s %s%s" % (v["name"][:18], pct(v[key], total), mark))

    print("""
读法：
  「收入构成/番数」回答"什么情况下得高分"——高手多少比例的钱是 2番+ 赚来的。
  「支出构成」回答"什么情况下扣分"——是被小牌零敲碎打，还是被大牌一次性放血。
  支出侧我们改不了对手，但它能告诉我们"输"到底是不是问题（自摸制没有防守）。""")


if __name__ == "__main__":
    main()
