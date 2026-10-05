"""样本外检验：「高手 +1.10 分/局」里有多少是挑人挑出来的？

高手名单是看全历史成绩挑的，再用同一批数据算他们的分，必然偏高（赢家诅咒）。
本工具用两种办法估计"真实"水平：

1. 按房间随机对半拆（主结论，不依赖时间）：在 A 半按分/局挑前 K 名，
   去 B 半看这 K 人的分/局；重复 200 次随机拆分。A 半分数 − B 半分数 = 挑选虚高。
2. 名单本身：MASTERS 名单成员的全量分 vs 对半拆后"另一半"的分（名单是用全量挑的，
   两半都参与过挑选，所以这一项只作参照，不能当样本外）。
   另按文件修改时间（≈采集时间）在 --cutoff 前后切，看名单成员切点之后的分。

只读 models/events/*.json，纯 stdlib，不联网。
    python3 tools/master_oos.py                 # 约 1-2 分钟，结果写 reports/master_oos.txt
    python3 tools/master_oos.py --report        # 只读缓存重出表
"""
import argparse
import hashlib
import json
import os
import pickle
import random
import sys
import time
from collections import defaultdict
from multiprocessing import Pool

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))

from mining_common import OUR_NAME, discover_files, seat_names  # noqa: E402
from batch_dashboard import MASTERS  # noqa: E402

CACHE = os.path.join(ROOT, "tools", ".cache", "master_oos.pkl")
OUT = os.path.join(ROOT, "reports", "master_oos.txt")


def scan(path):
    """-> (room, mtime, [(name, score_sum, n_rounds), ...]) 或 None"""
    try:
        with open(path, encoding="utf-8") as source:
            game = json.load(source)
    except (OSError, ValueError):
        return None
    names = seat_names(game)
    rounds = [r for r in game.get("rounds") or [] if len(r.get("scores") or []) == 4]
    if len(names) != 4 or not rounds:
        return None
    sums = [0, 0, 0, 0]
    for r in rounds:
        for s in range(4):
            sums[s] += r["scores"][s]
    return (game.get("room_id") or path, os.path.getmtime(path),
            [(names[s], sums[s], len(rounds)) for s in range(4)])


def load(jobs, rescan):
    if not rescan and os.path.exists(CACHE):
        with open(CACHE, "rb") as source:
            return pickle.load(source)
    files = discover_files()
    with Pool(jobs) as pool:
        games = [g for g in pool.imap_unordered(scan, files, chunksize=16) if g]
    with open(CACHE, "wb") as out:
        pickle.dump(games, out)
    return games


def per_player(games, rooms=None):
    """{name: [score_sum, rounds]}，只统计 rooms 里的局（None=全部）"""
    acc = defaultdict(lambda: [0, 0])
    for room, _, seats in games:
        if rooms is not None and room not in rooms:
            continue
        for name, score, n in seats:
            acc[name][0] += score
            acc[name][1] += n
    return acc


def spr(cell):
    return cell[0] / cell[1] if cell[1] else float("nan")


def mean(xs):
    return sum(xs) / len(xs) if xs else float("nan")


def pct(xs, q):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(q * len(xs)))] if xs else float("nan")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--jobs", type=int, default=6)
    ap.add_argument("--report", action="store_true", help="只读缓存出表")
    ap.add_argument("--min-rounds", type=int, default=200, help="每半至少多少局才参与挑选")
    ap.add_argument("--splits", type=int, default=200)
    ap.add_argument("--cutoff", default="2026-09-28", help="按文件修改时间切的日期（本地时间）")
    args = ap.parse_args()

    t0 = time.time()
    games = load(args.jobs, rescan=not args.report)
    lines = []

    def say(s=""):
        print(s)
        lines.append(s)

    rooms = sorted({g[0] for g in games})
    total = per_player(games)
    us_all = total.get(OUR_NAME, [0, 0])
    say("对局 %d 场，房间 %d 个，玩家 %d 人（%.0fs）" % (len(games), len(rooms), len(total), time.time() - t0))
    say("我们（全历史，各版本混合）：%.2f 分/局（%d 局）；最新 30 房见 live30_dashboard：-0.02" % (spr(us_all), us_all[1]))
    say()

    # ---- 1. 对半拆：A 半挑前 K，B 半看分 ----
    say("=== 1. 按房间随机对半拆 %d 次：A 半挑前 K 名（每半 ≥%d 局的玩家），B 半看同一批人 ===" % (args.splits, args.min_rounds))
    say("  K   | A 半(挑选时)分/局 | B 半(样本外)分/局 [5%%~95%%] | 虚高(A−B) | 每次合格玩家数")
    rng = random.Random(20261004)
    res = defaultdict(lambda: ([], []))
    eligible_n = []
    picked_count = defaultdict(int)
    for _ in range(args.splits):
        half = set(r for r in rooms if rng.random() < 0.5)
        a = per_player(games, half)
        b = per_player(games, set(rooms) - half)
        ok = [n for n in a if n != OUR_NAME and a[n][1] >= args.min_rounds and b.get(n, [0, 0])[1] >= args.min_rounds]
        eligible_n.append(len(ok))
        ranked = sorted(ok, key=lambda n: -spr(a[n]))
        for name in ranked[:5]:
            picked_count[name] += 1
        for k in (3, 5, 10, 20):
            top = ranked[:k]
            if len(top) < k:
                continue
            res[k][0].append(mean([spr(a[n]) for n in top]))
            res[k][1].append(mean([spr(b[n]) for n in top]))
    for k in (3, 5, 10, 20):
        ins, oos = res[k]
        if not ins:
            say("  %-3d | 合格玩家不足" % k)
            continue
        say("  %-3d | %+16.2f | %+8.2f [%+.2f ~ %+.2f]       | %+8.2f | %d" % (
            k, mean(ins), mean(oos), pct(oos, 0.05), pct(oos, 0.95), mean(ins) - mean(oos), int(mean(eligible_n))))
    say("  （5%~95% 是 200 次拆分的波动，近似样本误差；B 半分/局就是\"顶尖 K 人\"的真实水平估计）")
    say("  最常进入 A 半前 5 的玩家：" + "，".join("%s(%d%%)" % (n, 100 * c // args.splits)
                                         for n, c in sorted(picked_count.items(), key=lambda x: -x[1])[:10]))
    say()

    # ---- 2. 名单成员逐人 ----
    say("=== 2. MASTERS 名单成员（参照：名单本身是用全量数据挑的）===")
    cutoff = time.mktime(time.strptime(args.cutoff, "%Y-%m-%d"))
    before = per_player([g for g in games if g[1] < cutoff])
    after = per_player([g for g in games if g[1] >= cutoff])
    say("  文件修改时间 %s 之前 %d 场 / 之后 %d 场（修改时间≈采集时间；若采集是集中批量补的，这一列不可靠）"
        % (args.cutoff, sum(g[1] < cutoff for g in games), sum(g[1] >= cutoff for g in games)))
    say("  %-24s %10s %8s | %10s %8s | %10s %8s" % ("玩家", "全量分/局", "局数", "切点前", "局数", "切点后", "局数"))
    pool_all, pool_bef, pool_aft = [0, 0], [0, 0], [0, 0]
    for name in sorted(MASTERS, key=lambda n: -spr(total.get(n, [0, 1]))):
        c, cb, ca = total.get(name, [0, 0]), before.get(name, [0, 0]), after.get(name, [0, 0])
        if not c[1]:
            continue
        for p, x in ((pool_all, c), (pool_bef, cb), (pool_aft, ca)):
            p[0] += x[0]
            p[1] += x[1]
        say("  %-24s %+10.2f %8d | %+10.2f %8d | %+10.2f %8d" % (
            name, spr(c), c[1], spr(cb) if cb[1] else float("nan"), cb[1], spr(ca) if ca[1] else float("nan"), ca[1]))
    say("  %-24s %+10.2f %8d | %+10.2f %8d | %+10.2f %8d" % (
        "名单合计(按局加权)", spr(pool_all), pool_all[1], spr(pool_bef), pool_bef[1], spr(pool_aft), pool_aft[1]))
    ub, ua = before.get(OUR_NAME, [0, 0]), after.get(OUR_NAME, [0, 0])
    say("  %-24s %+10.2f %8d | %+10.2f %8d | %+10.2f %8d" % (
        "我们", spr(us_all), us_all[1], spr(ub) if ub[1] else float("nan"), ub[1], spr(ua) if ua[1] else float("nan"), ua[1]))
    say()
    say("怎么读：第 1 节 K=5/10 的「B 半分/局」是顶尖玩家真实水平的无偏估计；")
    say("  它和 +1.10 的差 = 挑人虚高；它和我们 -0.02 的差 = 真实差距。")

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as out:
        out.write("\n".join(lines) + "\n")
    print("\n已写 %s" % OUT)


if __name__ == "__main__":
    main()
