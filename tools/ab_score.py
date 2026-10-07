"""bot --ab 两组的得分对比（房间聚类 95% 区间），按 全部/庄/闲 拆。

    python3 tools/ab_score.py --since 2026-10-07T07:20
"""
import argparse
import json
import math
import os
import sys
from collections import defaultdict

sys.path[:0] = [os.path.dirname(os.path.dirname(os.path.abspath(__file__))), os.path.dirname(os.path.abspath(__file__))]
from latency_check import ab_labels  # noqa: E402
from mining_common import OUR_NAME, discover_files, seat_names  # noqa: E402


def _cluster(xs):
    n = len(xs)
    m = sum(x[2] for x in xs) / n
    by = defaultdict(float)
    for x in xs:
        by[x[0]] += x[2] - m
    k = len(by)
    return m, 1.96 * math.sqrt(sum(v * v for v in by.values()) * k / max(1, k - 1)) / n, n, k


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", required=True)
    args = ap.parse_args()
    lab = ab_labels(args.since, None)
    rows = defaultdict(list)
    for p in discover_files():
        room = os.path.basename(p).split("_r")[0]
        if room not in lab:
            continue
        with open(p, encoding="utf-8") as f:
            g = json.load(f)
        names = seat_names(g)
        if OUR_NAME not in names:
            continue
        me = names.index(OUR_NAME)
        for rd in g.get("rounds") or []:
            sc = rd.get("scores")
            if sc:
                rows[lab[room]].append((room, "庄" if rd.get("dealer") == me else "闲", sc[me], rd.get("winner") == me))
    for arm in sorted(rows):
        for role in (None, "庄", "闲"):
            s = [x for x in rows[arm] if role is None or x[1] == role]
            m, h, n, k = _cluster(s)
            print("%s %-4s 局 %4d 房 %2d  分/局 %+.2f ±%.2f  胜率 %.1f%%" % (
                arm, role or "全部", n, k, m, h, 100.0 * sum(x[3] for x in s) / n))
    print("A/B/C = --ab 第 1/2/3 项。± 为房间聚类 95% 半宽。")


if __name__ == "__main__":
    main()
