# -*- coding: utf-8 -*-
"""庄家杠杆：庄家胡牌 24×番、闲家 10×番，同样一手牌在庄位上值 2.4 倍。

这是一条一直没被检查过的通道。如果我们坐庄时的胡牌率明显低于坐闲时，
那就是一个纯粹的收入漏洞——1/4 的局里丢掉 2.4 倍的价值，而且完全不体现在
总胜率上（总胜率把庄闲混在一起平均掉了）。

    python3 tools/dealer_edge.py
"""
import argparse
import glob
import json
import os
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

OUR_UID = "u_fd06550b5fb3"


def scan(path):
    try:
        game = json.load(open(path, encoding="utf-8"))
    except (ValueError, OSError):
        return []
    uids = [s.get("user_id") for s in (game.get("seats") or [])]
    names = [s.get("name") or "?" for s in (game.get("seats") or [])]
    if len(uids) != 4:
        return []
    dealer_of = {}
    for block in game.get("blocks") or []:
        dealer_of.setdefault(block.get("round_no"), block.get("dealer"))
    out = []
    for rnd in game.get("rounds") or []:
        if rnd.get("is_draw"):
            continue
        dealer = rnd.get("dealer", dealer_of.get(rnd.get("round_no")))
        winner = rnd.get("winner")
        if dealer is None or winner is None:
            continue
        scores = rnd.get("scores") or [0] * 4
        out.append((uids, names, dealer, winner, scores))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--events", nargs="*",
                    default=["models/events/*.json", "tools/models/events/*.json"])
    a = ap.parse_args()

    files, seen = [], set()
    for pattern in a.events:
        for path in sorted(glob.glob(pattern)):
            name = os.path.basename(path)
            if name not in seen:
                seen.add(name)
                files.append(path)

    # uid -> {"庄": [局数, 胡数, 得分], "闲": [...]}, 外加名字
    cell = defaultdict(lambda: {"庄": [0, 0, 0.0], "闲": [0, 0, 0.0], "name": "?"})
    for path in files:
        for uids, names, dealer, winner, scores in scan(path):
            for seat in range(4):
                item = cell[uids[seat]]
                item["name"] = names[seat]
                role = "庄" if seat == dealer else "闲"
                item[role][0] += 1
                item[role][2] += scores[seat] if seat < len(scores) else 0
                if winner == seat:
                    item[role][1] += 1

    def line(tag, item):
        d, x = item["庄"], item["闲"]
        if not d[0] or not x[0]:
            return None
        dr, xr = d[1] / d[0], x[1] / x[0]
        return ("%-22s %6d %7.1f%% %+8.3f │ %6d %7.1f%% %+8.3f │ %+6.1fpp"
                % (tag[:20], d[0], 100 * dr, d[2] / d[0],
                   x[0], 100 * xr, x[2] / x[0], 100 * (dr - xr)))

    print("扫了 %d 个对局文件\n" % len(files))
    print("%-22s %6s %8s %8s │ %6s %8s %8s │ %s"
          % ("玩家", "坐庄局", "庄胡率", "庄分/局", "坐闲局", "闲胡率", "闲分/局", "庄−闲"))
    print("-" * 104)

    ours = cell.get(OUR_UID)
    rows = sorted((v for k, v in cell.items() if k != OUR_UID and v["庄"][0] >= 60),
                  key=lambda v: -(v["庄"][2] / max(1, v["庄"][0])))
    for item in rows[:10]:
        text = line(item["name"], item)
        if text:
            print(text)
    print("-" * 104)
    if ours:
        text = line(ours["name"] + "（我们）", ours)
        if text:
            print(text)

    # 全对手合并，作为基准
    agg = {"庄": [0, 0, 0.0], "闲": [0, 0, 0.0], "name": "全部对手合计"}
    for uid, item in cell.items():
        if uid == OUR_UID:
            continue
        for role in ("庄", "闲"):
            for i in range(3):
                agg[role][i] += item[role][i]
    text = line(agg["name"], agg)
    if text:
        print(text)

    print("""
读法：最后一列「庄−闲」是同一个人坐庄时比坐闲时高多少个百分点的胡牌率。
  * 这个数对所有人都该 ≈ 0 或略正（庄家先摸一张，理论上略占优）。
  * 如果**只有我们**明显为负，说明我们在庄位上有特定缺陷——1/4 的局里
    丢掉 2.4 倍的收入，而总胜率看不出来（庄闲被平均掉了）。
  * 「庄分/局」差距比胡牌率差距更直接：它已经把 24× 的赔率算进去了。""")


if __name__ == "__main__":
    main()
