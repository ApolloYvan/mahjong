"""「胡得小」拆到番种：高手每次胡牌多出来的番，来自爆头 / 财飘 / 杠开 / 七对 / 4白 哪一项。

    python3 tools/fan_sources.py

每个番种都是 ×2，所以 log2(番) = 带了几个翻倍项；两组的 平均log2番 之差 = 各番种出现率之差的和。
按 起手财神数 0/1/2+ 分格（庄闲合并；庄只影响底分不影响番）。
"""
import argparse
import json
import math
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

JOKER = "白"
TAGS = ("爆头", "财飘", "杠开", "七对", "4个白板", "其他")


def tag_of(x):
    if "飘" in x:
        return "财飘"
    if "七对" in x:
        return "七对"
    for t in ("爆头", "杠开", "4个白板"):
        if t in x:
            return t
    return None if x == "平胡" else "其他"


def scan(path):
    try:
        with open(path, encoding="utf-8") as source:
            game = json.load(source)
    except (OSError, ValueError):
        return None, []
    names = seat_names(game)
    if len(names) != 4:
        return None, []
    out = []
    for rnd in merge_rounds(game):
        hands = rnd.get("start_hands")
        if not hands or len(hands) != 4 or rnd.get("truncated"):
            continue
        for ev in rnd.get("events") or []:
            if ev.get("type") != "round_ended":
                continue
            d = ev.get("data") or {}
            scores = d.get("scores") or []
            if d.get("draw") or not d.get("fan") or len(scores) != 4:
                continue
            w = max(range(4), key=lambda s: scores[s])
            if scores[w] <= 0 or not hands[w]:
                continue
            tags = defaultdict(int)
            for x in d.get("detail") or []:
                t = tag_of(x)
                if t:
                    tags[t] += 1
            out.append((names[w], min(hands[w].count(JOKER), 2), d["fan"], dict(tags)))
    return game.get("room_id"), out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default="2026-09-25T15:30")
    ap.add_argument("--jobs", type=int, default=6)
    args = ap.parse_args()
    recent = recent_rooms(args.since)
    rows = []
    with Pool(args.jobs) as pool:
        files = discover_files()
        for done, (room, part) in enumerate(pool.imap_unordered(scan, files, chunksize=8), 1):
            for name, j, fan, tags in part:
                g = ("我们(当前)" if room in recent else "我们(以前)") if name == OUR else (
                    "高手" if name in MASTERS else None)
                if g:
                    rows.append((g, j, fan, tags))
            if done % 500 == 0 or done == len(files):
                print("  已扫 %d / %d" % (done, len(files)), flush=True)

    st = defaultdict(lambda: defaultdict(float))
    for g, j, fan, tags in rows:
        for c in (j, "全部"):
            x = st[(g, c)]
            x["n"] += 1
            x["fan"] += fan
            x["log"] += math.log2(fan)
            x["f4"] += fan >= 4
            for t, k in tags.items():
                x[t] += k

    print("\n=== 胡牌番种：每 100 次胡牌里各番种出现次数（×2 项），及对平均 log2番 的差距贡献 ===")
    head = "%-6s %-10s %6s %7s %7s %6s | " % ("起手白", "组", "胡次", "平均番", "log2番", "4番+")
    print(head + " ".join("%7s" % t for t in TAGS))
    for c in (0, 1, 2, "全部"):
        label = "全部" if c == "全部" else ("2+" if c == 2 else str(c))
        m = st.get(("高手", c))
        for g in ("高手", "我们(当前)", "我们(以前)"):
            x = st.get((g, c))
            if not x or x["n"] < 10:
                continue
            n = x["n"]
            print("%-6s %-10s %6d %7.2f %7.3f %5.1f%% | " % (label, g, n, x["fan"] / n, x["log"] / n, 100 * x["f4"] / n)
                  + " ".join("%7.1f" % (100 * x[t] / n) for t in TAGS))
            if g == "我们(当前)" and m:
                print("%-6s %-10s %6s %7s %+7.3f %6s | " % ("", "  差(我-高)", "", "", x["log"] / n - m["log"] / m["n"], "")
                      + " ".join("%+7.1f" % (100 * (x[t] / n - m[t] / m["n"])) for t in TAGS))
        print()
    print("读法：「差」那一行里负得最多的番种，就是我们胡得小的来源；再去查产生这个番种的那几步决策。")


if __name__ == "__main__":
    main()
