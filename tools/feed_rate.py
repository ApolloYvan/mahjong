"""喂牌率：每个玩家打出的牌有多少被别人吃/碰走（自摸制下唯一能"干扰"别人的杠杆）。

    python3 tools/feed_rate.py            # 全语料
    python3 tools/feed_rate.py --min-rounds 300 --top 12

只读 models/events 与 tools/models/events，不联网。
"""
import argparse
import glob
import json
import os
import sys
from collections import Counter, defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mining_common import seat_names  # noqa: E402

OUR = "重生之我是雀神"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-rounds", type=int, default=300)
    ap.add_argument("--top", type=int, default=12)
    args = ap.parse_args()

    st = defaultdict(Counter)
    seen = set()
    for path in glob.glob("tools/models/events/*.json") + glob.glob("models/events/*.json"):
        base = os.path.basename(path)
        if base in seen:
            continue
        seen.add(base)
        try:
            with open(path, encoding="utf-8") as source:
                game = json.JSONDecoder().raw_decode(source.read())[0]
        except (OSError, ValueError):
            continue
        names = seat_names(game)
        if len(names) != 4:
            continue
        for rnd in game.get("rounds", []):
            scores = rnd.get("scores") or []
            if len(scores) == 4:
                for seat, name in enumerate(names):
                    st[name]["rounds"] += 1
                    st[name]["score"] += scores[seat]
        for block in game.get("blocks", []):
            last = None
            for ev in block.get("events", []):
                kind, seat = ev.get("type"), ev.get("seat")
                if kind == "tile_discarded" and seat is not None:
                    last = seat
                    st[names[seat]]["discards"] += 1
                elif kind in ("chi", "peng") and last is not None and seat is not None and seat != last:
                    st[names[last]]["fed"] += 1
                    if kind == "chi":
                        st[names[last]]["fed_chi"] += 1
                    last = None
                elif kind == "tile_drawn":
                    last = None

    rows = [(n, s) for n, s in st.items() if s["rounds"] >= args.min_rounds and s["discards"]]
    rows.sort(key=lambda x: x[1]["score"] / x[1]["rounds"], reverse=True)
    show = rows[:args.top] + [r for r in rows if r[0] == OUR and r not in rows[:args.top]]
    print("%-24s %6s %8s %8s %9s %9s" % ("玩家", "局数", "分/局", "弃牌数", "被吃碰率", "被下家吃率"))
    print("-" * 72)
    for name, s in show:
        print("%-24s %6d %+8.3f %8d %8.2f%% %8.2f%%%s" % (
            name[:24], s["rounds"], s["score"] / s["rounds"], s["discards"],
            100 * s["fed"] / s["discards"], 100 * s["fed_chi"] / s["discards"],
            "  <== 我们" if name == OUR else ""))
    allf = sum(s["fed"] for _, s in rows)
    alld = sum(s["discards"] for _, s in rows)
    print("\n全体（≥%d 局）平均被吃碰率 %.2f%%" % (args.min_rounds, 100 * allf / max(alld, 1)))


if __name__ == "__main__":
    main()
