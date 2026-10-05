"""E4（只做分析）：高手对照——"高手不胡、我们会胡"的局面（摸到普通牌、非爆头可胡）：高手最终胡牌的比例、相对当场胡的净得分差，
按财神数、副露数、墙剩余、番数统计，用来调 E2/E3 的参数（``s1_one_step_min_ukeire``、``s1_ev_margin``、p̂ 分格）。

    python3 tools/e4_masters_decline.py --smoke                 # 冒烟（20 个文件，单进程，<1 分钟）
    caffeinate -i nice -n 15 python3 tools/e4_masters_decline.py --jobs 6 --max-minutes 30     # 全量，结果缓存

口径：
- "高手" = ``tools/train/player_stats.py`` 里**用另一半房间选出的**高手（和 S2 一致，避免选人和取数用同一批对局）；先跑 player_stats.py。
- 决策点 = 高手的"摸牌后"决策，摸到的不是财神、手牌构成非爆头胡牌（``mj.bot.hu_result``），且**生产策略在这个快照上会胡**
  （``mj.bot._choose_action_production``，``frozen_file_weights``，即当前线上配置：现行 S1 没触发）。分母 = 这些决策；
  "高手不胡" = 高手实际动作不是 hu。
- 结局：该座位这一局是否最终自己胡、本局得分增量；相对当场胡的净得分差 = 得分增量 - payout(当时番数, 庄/闲)。
  置信区间按房聚类自助。盈亏平衡 p* 取 models/s1_phat.json 的 L / 番数倍率（没有就先跑 tools/s1_fit.py）。
- 对每个"高手弃胡"的决策还记：弃完能否直接转爆头（``_decline_discard_choice``）、是否存在 E2 的"差一步转爆头"候选
  （``mj.s1_variants``，没有这个模块时为空）。
"""
import argparse
import json
import os
import random
import sys
import time
from collections import defaultdict
from multiprocessing import Pool

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))
sys.path.insert(0, os.path.join(ROOT, "tools", "train"))
os.environ.setdefault("MJ_WEIGHTS_NO_FILE", "1")

from build_dataset import iter_decisions, room_of  # noqa: E402
from mining_common import discover_files  # noqa: E402
from mj import bot  # noqa: E402
from mj.fit import frozen_file_weights  # noqa: E402
from mj.hu_strategy import _decline_discard_choice  # noqa: E402
from mj.rules import payout  # noqa: E402
from mj.tiles import JOKER, JOKER_IDX  # noqa: E402

try:
    from mj import s1_variants
except ImportError:      # E1-E3 还没装上
    s1_variants = None

CACHE = os.path.join(ROOT, "tools", ".cache", "e4_rows.jsonl")
MASTERS_JSON = os.path.join(ROOT, "tools", ".cache", "train", "masters.json")
_SEL = {}


def _init():
    _SEL.update(json.load(open(MASTERS_JSON, encoding="utf-8"))["masters_from"])
    ctx = frozen_file_weights()
    ctx.__enter__()


def scan(path):
    base = os.path.basename(path)
    room, rh = room_of(base)
    sel = set(_SEL.get(str(1 - rh % 2), []))
    rows = []
    for rec in iter_decisions(path, keep=lambda nm, ph, ac: 1.0 if (nm in sel and ph == "draw") else 0.0, seed=0):
        snap = rec["snapshot"]
        drawn = snap.get("drawn_tile")
        if snap["phase"] != "draw" or not drawn or drawn == JOKER:
            continue
        res = bot.hu_result(snap)
        if not res or res.get("baotou"):
            continue
        prod = bot._choose_action_production(snap)
        if not prod or prod.get("action") != "hu":
            continue
        me = snap["seat"]
        dealer = snap.get("dealer") == me
        fan = max(1, res.get("fan", 1))
        ret = rec["ret"]
        row = {"room": room, "game": base, "jokers": min(res["counts"][JOKER_IDX], 2), "melds": min(len(snap["melds"][me]), 2),
               "draws_left": (snap.get("wall_remaining") or 0) - 20, "fan": fan, "dealer": dealer,
               "masters_hu": rec["action"].get("action") == "hu", "won": bool(ret.get("win")),
               "delta": ret.get("delta"), "hu_now": payout(fan, dealer=dealer)}
        if not row["masters_hu"]:
            row["direct"] = _decline_discard_choice(snap, res) is not None
            row["one_step"] = None
            if s1_variants is not None:
                row["one_step"] = s1_variants.one_step_choice(snap, res, {"s1_one_step_min_ukeire": 8}, len(snap["melds"][me])) is not None
        rows.append(row)
    return rows


def boot(rows, fn, n=1000, seed=0):
    by = defaultdict(list)
    for r in rows:
        by[r["room"]].append(r)
    rooms = list(by)
    rng = random.Random(seed)
    stats = []
    for _ in range(n):
        s = []
        for rm in rng.choices(rooms, k=len(rooms)):
            s.extend(by[rm])
        stats.append(fn(s))
    stats.sort()
    return fn(rows), stats[int(n * 0.025)], stats[int(n * 0.975)]


def wr(rs):
    return sum(r["won"] for r in rs) / len(rs) if rs else float("nan")


def net(rs):
    ok = [r for r in rs if r["delta"] is not None]
    return sum(r["delta"] - r["hu_now"] for r in ok) / len(ok) if ok else float("nan")


def wbin(d):
    return "<8" if d < 8 else "8-15" if d < 16 else "16-31" if d < 32 else "32+"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--jobs", type=int, default=int(os.environ.get("MJ_JOBS", "6")))
    ap.add_argument("--max-minutes", type=float, default=None)
    ap.add_argument("--no-cache", action="store_true")
    ap.add_argument("--smoke", action="store_true")
    ap.add_argument("--limit-files", type=int, default=None, help="（调试）间隔取这么多个文件，不写缓存")
    args = ap.parse_args()
    if not os.path.exists(MASTERS_JSON):
        print("没有 %s（先跑 python3 tools/train/player_stats.py）" % MASTERS_JSON)
        sys.exit(2)
    started = time.time()
    cache = CACHE + (".smoke" if args.smoke else "")
    if os.path.exists(cache) and not args.no_cache:
        rows = [json.loads(l) for l in open(cache, encoding="utf-8")]
        print("读取缓存 %s（%d 行；--no-cache 重扫）" % (cache, len(rows)))
    else:
        files = discover_files()
        if args.smoke or args.limit_files:
            n_files = args.limit_files or 20
            files = files[::max(1, len(files) // n_files)][:n_files]
        deadline = started + args.max_minutes * 60 if args.max_minutes else None
        rows, stopped = [], False
        pool = Pool(args.jobs, initializer=_init) if args.jobs > 1 and not args.smoke and not args.limit_files else None
        if not pool:
            _init()
        it = pool.imap_unordered(scan, files, chunksize=4) if pool else map(scan, files)
        try:
            for got in it:
                rows.extend(got)
                if deadline and time.time() > deadline:
                    stopped = True
                    break
        finally:
            if pool:
                pool.terminate()
                pool.join()
        if not stopped and not args.limit_files:
            os.makedirs(os.path.dirname(cache), exist_ok=True)
            with open(cache, "w", encoding="utf-8") as f:
                for r in rows:
                    f.write(json.dumps(r, ensure_ascii=False) + "\n")
    dec = [r for r in rows if not r["masters_hu"]]
    hu_ = [r for r in rows if r["masters_hu"]]
    print("=== e4_masters_decline（高手决策 %d：生产会胡的非爆头可胡局面；%.0fs） ===" % (len(rows), time.time() - started))
    if not rows or not dec:
        print("没有足够的样本（高手弃胡 %d）" % len(dec))
        return
    w, wl, wh = boot(dec, wr)
    n_, nl, nh = boot(dec, net)
    print("高手弃胡 %d / %d = %.1f%%；弃胡后最终自己胡 %.1f%% [%.1f%%, %.1f%%]；相对当场胡净 %+.2f 分/次 [%+.2f, %+.2f]（按房聚类自助）" % (
        len(dec), len(rows), 100.0 * len(dec) / len(rows), 100 * w, 100 * wl, 100 * wh, n_, nl, nh))
    print("对照：高手选择当场胡的 %d 个局面，当场胡得分均值 %.1f（弃胡净差是相对各自当场胡算的，不是对照组）" % (
        len(hu_), sum(r["hu_now"] for r in hu_) / max(1, len(hu_))))
    try:
        phat = json.load(open(os.path.join(ROOT, "models", "s1_phat.json"), encoding="utf-8"))
        L, ratio = phat["L"], phat["ratio"]
    except (OSError, ValueError, KeyError):
        L, ratio = None, None
    print("按（财神,副露）：全部局面 n / 弃胡率 | 弃胡后最终胡率 / 净差/次 / 盈亏平衡 p*(按这格平均番数、庄闲混合取闲)")
    for j in (0, 1, 2):
        for m in (0, 1, 2):
            allc = [r for r in rows if r["jokers"] == j and r["melds"] == m]
            dc = [r for r in allc if not r["masters_hu"]]
            if len(allc) < 5:
                continue
            ps = ""
            if dc and L:
                f = sum(r["fan"] for r in dc) / len(dc)
                ps = "p*=%.0f%%" % (100 * (payout(f, dealer=False) + L["non"]) / (payout(f * ratio, dealer=False) + L["non"]))
            print("  %s白/%s露 n=%-4d 弃胡 %4.1f%% | %s %s" % (
                "2+" if j == 2 else j, "2+" if m == 2 else m, len(allc), 100.0 * len(dc) / len(allc),
                ("胡率 %.0f%% 净 %+.1f (n=%d)" % (100 * wr(dc), net(dc), len(dc))) if dc else "-", ps))
    print("按墙剩余摸牌轮数（墙-20）：")
    for b in ("<8", "8-15", "16-31", "32+"):
        allc = [r for r in rows if wbin(r["draws_left"]) == b]
        dc = [r for r in allc if not r["masters_hu"]]
        if allc:
            print("  %-6s n=%-4d 弃胡 %4.1f%% | %s" % (b, len(allc), 100.0 * len(dc) / len(allc),
                                                    ("胡率 %.0f%% 净 %+.1f" % (100 * wr(dc), net(dc))) if dc else "-"))
    print("按当时番数：")
    for lo, hi, name in ((1, 1, "1番"), (2, 3, "2-3番"), (4, 99, "4番+")):
        allc = [r for r in rows if lo <= r["fan"] <= hi]
        dc = [r for r in allc if not r["masters_hu"]]
        if allc:
            print("  %-5s n=%-4d 弃胡 %4.1f%% | %s" % (name, len(allc), 100.0 * len(dc) / len(allc),
                                                    ("胡率 %.0f%% 净 %+.1f" % (100 * wr(dc), net(dc))) if dc else "-"))
    direct = [r for r in dec if r.get("direct")]
    one = [r for r in dec if not r.get("direct") and r.get("one_step")]
    rest = [r for r in dec if not r.get("direct") and not r.get("one_step")]
    print("高手弃胡的局面分类：能直接转爆头 %d（胡率 %.0f%% 净 %+.1f）；不能直接转、但有 E2 差一步候选 %d（胡率 %.0f%% 净 %+.1f）；"
          "两者都没有 %d（胡率 %.0f%% 净 %+.1f）%s" % (
              len(direct), 100 * wr(direct), net(direct), len(one), 100 * wr(one), net(one), len(rest), 100 * wr(rest), net(rest),
              "" if s1_variants else "（E1-E3 还没装上，差一步分类为空）"))


if __name__ == "__main__":
    main()
