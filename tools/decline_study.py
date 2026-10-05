"""弃胡（S1）对照：摸到能胡的普通牌、且有一张弃牌能转成爆头听时，高手 vs 我们 各弃胡多少、在哪些局面弃。

    python3 tools/decline_study.py

决策点与 tools/rollout.py --mode hu 完全一致（collect_hu_file）。按 财神数 × 副露 × 剩余可摸张数 分格，
「我们(当前)」= --since 之后开打的房（与 baotou_funnel 一致）。
弃后胡率 = 选择弃胡的那些点，本局最终是本人胡的比例；直接胡 的点胡率恒为 100%，不列。
"""
import argparse
import json
import os
import sys
from collections import Counter, defaultdict
from multiprocessing import Pool

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))

from mining_common import discover_files  # noqa: E402
from mining_common import seat_names  # noqa: E402
from baotou_funnel import recent_rooms  # noqa: E402
from rollout import collect_hu_file  # noqa: E402

JOKER = "白"
_RECENT = set()


def _init(recent):
    _RECENT.update(recent)


def scan(path):
    recs, _stats = collect_hu_file((path, 1.0))
    if not recs:
        return []
    with open(path, encoding="utf-8") as source:
        game = json.load(source)
    room = game.get("room_id") or ""
    names = seat_names(game)
    winners = {r.get("round_no"): (None if r.get("is_draw") else r.get("winner")) for r in game.get("rounds") or []}
    out = []
    for st in recs:
        g = "高手" if st["g"] == "master" else ("我们(当前)" if room in _RECENT else "我们(以前)")
        seat = names.index(st["p"])
        left = st["wall"] - 20
        bucket = "<8" if left < 8 else ("8-15" if left < 16 else ("16-27" if left < 28 else "28+"))
        cell = (min(st["hand"].count(JOKER), 2), min(len(st["melds"]), 2))
        declined = st["actual"] != 0
        out.append((g, cell, bucket, declined, declined and winners.get(st["round_no"]) == seat))
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

    c = defaultdict(Counter)
    for g, cell, bucket, dec, won in rows:
        for key in ((g, cell, bucket), (g, cell, "全部"), (g, "合计", bucket), (g, "合计", "全部")):
            c[key]["n"] += 1
            c[key]["dec"] += dec
            c[key]["won"] += won

    def show(k):
        x = c.get(k)
        if not x or x["n"] < 5:
            return "%16s" % "-"
        won = "%3.0f%%" % (100.0 * x["won"] / x["dec"]) if x["dec"] >= 5 else "  - "
        return "%4d %5.1f%% %s" % (x["n"], 100.0 * x["dec"] / x["n"], won)

    buckets = ("28+", "16-27", "8-15", "<8", "全部")
    print("\n=== 能胡普通牌、且可转爆头时：点数 / 弃胡率 / 弃后胡率（按剩余可摸张数）===")
    print("%-9s %-10s " % ("财神/副露", "组") + " ".join("%16s" % b for b in buckets))
    cells = sorted({k[1] for k in c if k[1] != "合计"}) + ["合计"]
    for cell in cells:
        label = cell if cell == "合计" else "%s白/%s露" % ("2+" if cell[0] == 2 else cell[0], "2+" if cell[1] == 2 else cell[1])
        for g in ("高手", "我们(当前)", "我们(以前)"):
            if (g, cell, "全部") in c:
                print("%-9s %-10s " % (label, g) + " ".join(show((g, cell, b)) for b in buckets))
        print()
    print("我们线上：剩余可摸 <8 张一律直接胡（墙尾闸门），其余格子只要能转爆头就弃。")


if __name__ == "__main__":
    main()
