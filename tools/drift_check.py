"""版本漂移检查：把各个历史版本（build_hash）在实战里真实做过的决策，逐个喂给**现在的**生产代码 + 现在的 weights.json，
看现在会不会做同样的选择。代码和参数都没变的版本，一致率应接近 100%；哪个版本之后一致率掉下来，就是从那里开始变了。

- 版本 → 房间：扫 logs/*.jsonl 里 decision 记录的 build_hash（用 rg，几秒）；局面来自 models/events 里同房间的对局文件
  （tools/train/build_dataset.iter_decisions 逐事件重放，和 S2/E4 同一套快照），只取我们座位、非服务端代打的决策。
- 现在的选择 = mj.bot._choose_action_production（frozen_file_weights，读当前 models/weights.json）。
- 输出：每个版本的房间数、实战分/局、各类决策一致率；以及"好时期"版本里不一致的决策样例（手牌、当时打的、现在打的）。

    python3 tools/drift_check.py --smoke             # 每个版本 1 个房间的 1 场，<1 分钟
    python3 tools/drift_check.py --jobs 6            # 每个版本最多 6 个房间；结果 reports/drift_check.txt
"""
import argparse
import glob
import json
import os
import pickle
import re
import subprocess
import sys
import time
from collections import Counter, defaultdict
from multiprocessing import Pool

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))
sys.path.insert(0, os.path.join(ROOT, "tools", "train"))
os.environ.setdefault("MJ_WEIGHTS_NO_FILE", "1")

from build_dataset import iter_decisions  # noqa: E402
from mining_common import OUR_NAME, discover_files  # noqa: E402
from mj import bot  # noqa: E402
from mj.fit import frozen_file_weights  # noqa: E402

OUT = os.path.join(ROOT, "reports", "drift_check.txt")
RX = r'"game_id": "(a_[0-9a-f]+)_[^"]*".*?"build_hash": "([0-9a-f]+)"'


def room_builds():
    """{room: (build_hash, 日期)}，取该房间出现次数最多的 build。"""
    cnt = Counter()
    first = {}
    for path in sorted(glob.glob(os.path.join(ROOT, "logs", "2026-*.jsonl"))):
        day = os.path.basename(path)[:10]
        try:
            out = subprocess.run(["rg", "-o", "-P", "-r", "$1 $2", '"kind": "decision".*?' + RX, path],
                                 capture_output=True, text=True).stdout
        except FileNotFoundError:          # 没装 rg：退回 Python 逐行（慢）
            out = []
            with open(path, encoding="utf-8", errors="replace") as f:
                for line in f:
                    if '"kind": "decision"' in line:
                        m = re.search(RX, line)
                        if m:
                            out.append("%s %s" % m.groups())
            out = "\n".join(out)
        for line in out.splitlines():
            room, b = line.split()
            cnt[(room, b)] += 1
            first.setdefault(room, day)
    best = {}
    for (room, b), n in cnt.items():
        if room not in best or n > best[room][1]:
            best[room] = (b, n)
    return {room: (b, first[room]) for room, (b, _) in best.items()}


def _init():
    ctx = frozen_file_weights()
    ctx.__enter__()


def _norm(act):
    a = (act or {}).get("action") or "pass"
    if a == "discard":
        return "discard:%s" % act.get("tile")
    if a in ("gang", "peng"):
        return "%s:%s" % (a, act.get("tile"))
    return a


def scan(task):
    path, build = task
    out = []
    for rec in iter_decisions(path, keep=lambda nm, ph, ac: 1.0 if nm == OUR_NAME else 0.0, seed=0):
        snap = rec["snapshot"]
        try:
            now = bot._choose_action_production(snap) or {"action": "pass"}
        except Exception as e:     # 现在的代码在旧局面上报错，本身就是漂移信号
            now = {"action": "error:%s" % type(e).__name__}
        then = rec["action"]
        fam = "draw" if rec["phase"] == "draw" else "resp"
        if fam == "draw":
            kind = "hu" if bot.hu_result(snap) else ("discard" if then.get("action") == "discard" and _norm(now).startswith("discard") else "other")
        else:
            kind = "resp"
        same = _norm(now) == _norm(then)
        row = {"build": build, "kind": kind, "same": same, "game": os.path.basename(path)}
        if not same:
            row.update(then=_norm(then), now=_norm(now), hand=" ".join(sorted(snap["my_hand"])),
                       melds=len(snap["melds"][snap["seat"]]), wall=snap.get("wall_remaining"),
                       dealer=snap.get("dealer") == snap["seat"], drawn=snap.get("drawn_tile"))
        out.append(row)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--jobs", type=int, default=int(os.environ.get("MJ_JOBS", "6")))
    ap.add_argument("--rooms-per-build", type=int, default=6)
    ap.add_argument("--since", default="2026-09-27", help="只看这天及以后首次出现的版本")
    ap.add_argument("--smoke", action="store_true")
    args = ap.parse_args()
    if subprocess.run(["pgrep", "-f", "mj[.]bot"], stdout=subprocess.DEVNULL).returncode == 0:
        sys.exit("bot 正在运行，先停 bot")
    t0 = time.time()
    rb = room_builds()
    files_by_room = defaultdict(list)
    for p in discover_files():
        files_by_room[os.path.basename(p).split("_")[1]].append(p)
    score = defaultdict(lambda: [0, 0])
    cache = os.path.join(ROOT, "tools", ".cache", "master_oos.pkl")
    if os.path.exists(cache):
        for room, _, seats in pickle.load(open(cache, "rb")):
            for n, s, k in seats:
                if n == OUR_NAME:
                    score["a_" + room if not room.startswith("a_") else room][0] += s
                    score["a_" + room if not room.startswith("a_") else room][1] += k
    builds = defaultdict(list)
    for room, (b, day) in rb.items():
        if day >= args.since:
            builds[b].append((day, room))
    order = sorted(builds, key=lambda b: min(builds[b]))
    tasks = []
    info = {}
    for b in order:
        rooms = sorted(builds[b])
        have = [(d, r) for d, r in rooms if files_by_room.get(r[2:] if r.startswith("a_") else r)]
        pick = have[:1] if args.smoke else have[:args.rooms_per_build]
        s = [score[r] for _, r in rooms if score[r][1]]
        info[b] = {"days": "%s~%s" % (rooms[0][0][5:], rooms[-1][0][5:]), "rooms": len(rooms), "picked": len(pick),
                   "spr": sum(x[0] for x in s) / max(1, sum(x[1] for x in s)), "n": sum(x[1] for x in s)}
        for _, r in pick:
            fs = files_by_room[r[2:] if r.startswith("a_") else r]
            tasks += [(p, b) for p in (fs[:1] if args.smoke else fs)]
    print("版本 %d 个，待重放对局文件 %d 个（%.0fs 建索引）" % (len(order), len(tasks), time.time() - t0), flush=True)
    if args.smoke or args.jobs <= 1:
        _init()
        rows = [r for t in tasks for r in scan(t)]
    else:
        rows = []
        with Pool(args.jobs, initializer=_init) as pool:
            for i, sub in enumerate(pool.imap_unordered(scan, tasks, chunksize=1), 1):
                rows.extend(sub)
                if i % 50 == 0:
                    print("  已重放 %d / %d 个文件，%.0fs" % (i, len(tasks), time.time() - t0), flush=True)

    lines = []

    def say(s=""):
        print(s)
        lines.append(s)

    say("=== drift_check：历史版本的实战决策 vs 现在的代码+参数（%.0fs）===" % (time.time() - t0))
    say("%-18s %-12s %5s %7s %8s | %7s %14s %14s %14s %14s" % (
        "版本", "日期", "房", "局", "实战分/局", "重放房", "出牌一致", "可胡一致", "响应一致", "其他一致"))
    for b in order:
        rs = [r for r in rows if r["build"] == b]
        cells = []
        for k in ("discard", "hu", "resp", "other"):
            sub = [r for r in rs if r["kind"] == k]
            cells.append("%5.1f%%(%6d)" % (100.0 * sum(r["same"] for r in sub) / len(sub), len(sub)) if sub else "-")
        i = info[b]
        say("%-18s %-12s %5d %7d %+8.2f | %7d %14s %14s %14s %14s" % (b, i["days"], i["rooms"], i["n"], i["spr"], i["picked"], *cells))
    say()
    say("不一致的类型（当时 → 现在），每个版本前 8 类：")
    for b in order:
        diff = [r for r in rows if r["build"] == b and not r["same"]]
        if not diff:
            continue
        c = Counter("%s|%s→%s" % (r["kind"], r["then"].split(":")[0], r["now"].split(":")[0]) for r in diff)
        say("  %s：%s" % (b, "；".join("%s %d" % kv for kv in c.most_common(8))))
    say()
    say("样例（每个版本最多 5 条非出牌类 + 5 条出牌类）：")
    for b in order:
        diff = [r for r in rows if r["build"] == b and not r["same"]]
        ex = [r for r in diff if r["kind"] != "discard"][:5] + [r for r in diff if r["kind"] == "discard"][:5]
        for r in ex:
            say("  %s %-7s 手牌[%s] 摸%s 露%d 墙%s %s | 当时 %s → 现在 %s  (%s)" % (
                b[:8], r["kind"], r["hand"], r["drawn"] or "-", r["melds"], r["wall"], "庄" if r["dealer"] else "闲",
                r["then"], r["now"], r["game"]))
    say()
    say("怎么读：同一份代码+参数重放，一致率应 ≥98%（差的那点来自时间预算类计算和快照细节）。")
    say("  哪个版本一致率明显偏低，就是它和现在不一样；看\"不一致的类型\"和样例判断变的是什么、往哪个方向变。")
    if not args.smoke:
        with open(OUT, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        print("\n已写 %s" % OUT)


if __name__ == "__main__":
    main()
