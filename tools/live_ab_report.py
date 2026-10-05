"""实战按房随机 A/B 汇总（配套 ``python3 -m mj.bot --rooms N --ab A.json,B.json``）。

    python3 tools/live_ab_report.py [--logs 'logs/*.jsonl'] [--since 2026-10-05] [--smoke]

数据：``logs/*.jsonl`` 里的 ``ab_assign``（房 -> A/B、配置名、种子、overlay）和 ``ab_game``（房/局/标签）；对局结果取
``models/events/<game_id>.json``（赛后用 tools/pull_all_events.py 拉取；没拉到的局会列出数量）：我们座位每局得分、整场总分、
整场名次（并列取最好名次）。按版本（A/B）汇总：房数/局数/轮数、每局场均分、平均名次、第一名率、前二率；
**差值（A−B）的 95% 置信区间按房聚类**——每个版本内独立地对房有放回重抽（2000 次），再做差。
每个房是一个聚类单位（同一房里的多场对局共享对手和运气，不当独立样本）。
"""
import argparse
import glob
import json
import os
import random
import sys
from collections import defaultdict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))

from mining_common import OUR_NAME, seat_names  # noqa: E402


def read_ab(log_glob, since=None):
    assign, games, seeds = {}, {}, {}
    for path in sorted(glob.glob(log_glob)):
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                if '"kind": "ab_' not in line[:80]:
                    continue
                try:
                    rec = json.loads(line)
                    kind, p = rec["kind"], rec["payload"]
                except (ValueError, KeyError):
                    continue
                if since and str(rec.get("time", ""))[:10] < since:
                    continue
                if kind == "ab_assign":
                    assign[p["room_id"]] = p["label"]
                    seeds[p["room_id"]] = (p.get("config"), p.get("seed"))
                elif kind == "ab_game":
                    games[p["game_id"]] = (p["room_id"], p["label"])
    return assign, games, seeds


def load_game(game_id):
    for pat in ("models/events/%s.json", "tools/models/events/%s.json"):
        path = os.path.join(ROOT, pat % game_id)
        if os.path.exists(path):
            try:
                with open(path, encoding="utf-8") as f:
                    return json.JSONDecoder().raw_decode(f.read())[0]
            except (OSError, ValueError):
                return None
    return None


def game_result(game):
    names = seat_names(game)
    if OUR_NAME not in names:
        return None
    me = names.index(OUR_NAME)
    rows = [r["scores"] for r in game.get("rounds", []) if isinstance(r.get("scores"), list) and len(r["scores"]) == 4]
    if not rows:
        return None
    totals = [sum(r[s] for r in rows) for s in range(4)]
    rank = 1 + sum(1 for s in range(4) if totals[s] > totals[me])
    return {"rounds": len(rows), "total": totals[me], "rank": rank}


def room_stats(games_list):
    rounds = sum(g["rounds"] for g in games_list)
    return {"rounds": rounds, "score": sum(g["total"] for g in games_list), "n": len(games_list),
            "rank": sum(g["rank"] for g in games_list), "first": sum(g["rank"] == 1 for g in games_list),
            "top2": sum(g["rank"] <= 2 for g in games_list)}


METRICS = (("每局场均分", lambda a: a["score"] / a["rounds"] if a["rounds"] else float("nan")),
           ("平均名次", lambda a: a["rank"] / a["n"] if a["n"] else float("nan")),
           ("第一名率", lambda a: a["first"] / a["n"] if a["n"] else float("nan")),
           ("前二率", lambda a: a["top2"] / a["n"] if a["n"] else float("nan")))


def pool(rooms):
    out = {"rounds": 0, "score": 0, "n": 0, "rank": 0, "first": 0, "top2": 0}
    for r in rooms:
        for k in out:
            out[k] += r[k]
    return out


def diff_ci(rooms_a, rooms_b, fn, n=2000, seed=0):
    rng = random.Random(seed)
    stats = []
    for _ in range(n):
        sa = pool(rng.choices(rooms_a, k=len(rooms_a)))
        sb = pool(rng.choices(rooms_b, k=len(rooms_b)))
        stats.append(fn(sa) - fn(sb))
    stats.sort()
    return fn(pool(rooms_a)) - fn(pool(rooms_b)), stats[int(n * 0.025)], stats[int(n * 0.975)]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--logs", default=os.path.join(ROOT, "logs", "*.jsonl"))
    ap.add_argument("--since", default=None, help="只看这个日期（YYYY-MM-DD）之后的 ab 记录")
    ap.add_argument("--smoke", action="store_true", help="用合成数据自检（不读真实日志）")
    args = ap.parse_args()
    if args.smoke:
        rng = random.Random(1)
        by_room = {"A": [], "B": []}
        for i in range(30):
            for label, mu in (("A", 1.0), ("B", 0.0)):
                gs = [{"rounds": 8, "total": rng.gauss(mu * 8, 20), "rank": rng.randint(1, 4)} for _ in range(rng.randint(2, 6))]
                by_room[label].append(room_stats(gs))
        missing, seeds, n_games = 0, {}, sum(r["n"] for l in by_room.values() for r in l)
    else:
        assign, games, seeds = read_ab(args.logs, args.since)
        by_room = {"A": [], "B": []}
        room_games = defaultdict(list)
        missing = 0
        for gid, (room, label) in games.items():
            g = load_game(gid)
            res = game_result(g) if g else None
            if res is None:
                missing += 1
                continue
            room_games[(room, label)].append(res)
        for (room, label), gl in room_games.items():
            by_room[label].append(room_stats(gl))
        n_games = sum(len(v) for v in room_games.values())
    print("=== live_ab_report（%s；房 A %d / B %d，局 %d%s） ===" % (
        "合成自检" if args.smoke else "实战", len(by_room["A"]), len(by_room["B"]), n_games,
        "；没有拉到事件的局 %d" % missing if missing else ""))
    if not by_room["A"] or not by_room["B"]:
        print("A/B 任一版本没有可汇总的房（日志里没有 ab_game 记录，或赛后事件还没拉取：tools/pull_all_events.py）")
        return
    if seeds:
        cfg = {}
        for room, (name, seed) in seeds.items():
            cfg[name] = cfg.get(name, 0) + 1
        print("配置（房数）：%s；种子 %s" % (cfg, sorted({s for _, s in seeds.values()})))
    pa, pb = pool(by_room["A"]), pool(by_room["B"])
    print("%-8s %5s %5s %6s | %-10s %-10s | %s" % ("指标", "房A", "房B", "", "A", "B", "A−B 95%CI（按房聚类）"))
    for name, fn in METRICS:
        d, lo, hi = diff_ci(by_room["A"], by_room["B"], fn)
        fmt = "%.3f" if "率" in name else "%+.3f" if "分" in name else "%.3f"
        print("%-8s %5d %5d %6s | %-10s %-10s | %+.3f [%+.3f, %+.3f]%s" % (
            name, len(by_room["A"]), len(by_room["B"]), "", fmt % fn(pa), fmt % fn(pb), d, lo, hi,
            "  （CI 含 0）" if lo <= 0 <= hi else ""))
    print("局数 A %d B %d，轮数 A %d B %d（名次：越小越好，所以平均名次 A−B<0 表示 A 更好）" % (pa["n"], pb["n"], pa["rounds"], pb["rounds"]))


if __name__ == "__main__":
    main()
