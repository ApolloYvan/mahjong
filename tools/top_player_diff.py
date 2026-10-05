"""真高手 vs 生产：同一局面下，生产会怎么做、真高手实际怎么做，分歧在哪、分歧后结果如何。

背景（2026-10-04）：master_oos.py 显示旧 MASTERS 名单里一半人样本外只有 +0.2 左右，之前的模仿/一致率分析被稀释了。
这里只看样本外仍稳定在前列的玩家（--players 可改），并用"其他玩家"抽样做对照：
高手分歧明显多于对照、且分歧后结果更好的类别 = 候选改进点，再交给 arena 验证（结果差是相关不是因果）。

口径：
- 决策点来自 tools/train/build_dataset.iter_decisions（真实事件逐步重放 + 线上同款快照），跳过服务端代打、抓打圈受限的摸牌。
- 生产 = mj.bot._choose_action_production + 当前 models/weights.json（frozen_file_weights）。
- 分歧：动作类型不同，或都出牌但出的牌不同（吃的搭子不同不算分歧）。
- 结果 = 该座位本局得分增量 / 是否本局自己胡。分歧组 − 一致组 的差按房间聚类自助取 95% 区间。

    python3 tools/top_player_diff.py --smoke                                   # 冒烟（20 个文件，单进程）
    caffeinate -i nice -n 15 python3 tools/top_player_diff.py --jobs 6         # 全量，十几分钟；结果 reports/top_player_diff.txt
    python3 tools/top_player_diff.py --report                                  # 只读缓存重出表
"""
import argparse
import json
import os
import random
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
from mining_common import OUR_NAME, discover_files, seat_names  # noqa: E402
from mj import bot  # noqa: E402
from mj.fit import frozen_file_weights  # noqa: E402
from mj.shanten import route_shanten  # noqa: E402
from mj.tiles import JOKER, TILE_INDEX, to_counts  # noqa: E402

TOP_DEFAULT = ["腾蛇-0638", "歪比巴卜肉蛋葱鸡", "⭐꧁༺🀆🀆🀆🀆༻꧂⭐", "玄武-2346", "鲲鹏~6383", "算我求您了"]
CACHE = os.path.join(ROOT, "tools", ".cache", "top_diff_rows.jsonl")
OUT = os.path.join(ROOT, "reports", "top_player_diff.txt")
_CFG = {}


def _init(cfg):
    _CFG.update(cfg)
    ctx = frozen_file_weights()
    ctx.__enter__()


def _sh(tiles, groups):
    return route_shanten(tuple(to_counts(tiles)), groups)


def _ttype(t):
    if t == JOKER:
        return "财神"
    i = TILE_INDEX.get(t, 99)
    if i >= 27:
        return "字"
    return "幺九" if i % 9 in (0, 8) else "中张"


def _jbin(n):
    return "2+" if n >= 2 else str(n)


def _wbin(wall):
    left = wall - 20
    return "早(>40)" if left > 40 else "中(17-40)" if left > 16 else "晚(≤16)"


def _shbin(s):
    return "听牌" if s <= 0 else "1向听" if s == 1 else "2向听" if s == 2 else "3+向听"


def _keep(name, phase, action):
    if name == OUR_NAME:
        return 0.0
    if name in _CFG["top"]:
        return 1.0
    return _CFG["ctrl_keep"]


def scan(path):
    try:
        with open(path, encoding="utf-8") as f:
            names = seat_names(json.load(f))
    except (OSError, ValueError):
        return []
    if not any(n in _CFG["top"] for n in names):     # 没有高手的场只取对照，按概率跳过整场省时间
        if random.Random(path).random() >= _CFG["ctrl_file_keep"]:
            return []
    rows = []
    for rec in iter_decisions(path, keep=_keep, seed=0):
        try:
            row = _one(rec)
        except Exception:      # 生产在个别重放快照上报错：计数，不让整场丢掉
            row = {"err": 1}
        if row:
            rows.append(row)
    return rows


def _one(rec):
    snap, act = rec["snapshot"], rec["action"]
    me = snap["seat"]
    groups = len(snap["melds"][me])
    hand = snap["my_hand"]
    base = {"room": rec["room_name"], "who": "top" if rec["name"] in _CFG["top"] else "ctrl", "name": rec["name"],
            "dealer": snap.get("dealer") == me, "delta": rec["ret"]["delta"], "win": rec["ret"]["win"]}
    pa = act.get("action")
    if rec["phase"] == "draw":
        god = snap.get("god") or {}
        if god.get("catch_play") and god.get("god_discarder_seat") != me:
            return None
        prod = bot._choose_action_production(snap) or {}
        qa = prod.get("action")
        res = bot.hu_result(snap)
        if res:
            drawn = snap.get("drawn_tile")
            base.update(fam="hu", keys=["可胡·摸%s·%s·生产=%s" % ("财神" if drawn == JOKER else "普通牌",
                                                             "爆头" if res.get("baotou") else "非爆头",
                                                             "胡" if qa == "hu" else "不胡")],
                        div=(pa == "hu") != (qa == "hu"))
            return base
        if pa == "gang" or qa == "gang":
            base.update(fam="gang", keys=["自摸杠机会·生产=%s" % ("杠" if qa == "gang" else "不杠")],
                        div=(pa == "gang") != (qa == "gang"))
            return base
        if pa != "discard" or qa != "discard":
            return None
        pt, qt = act.get("tile"), prod.get("tile")
        if pt not in hand or qt not in hand:
            return None
        jok = hand.count(JOKER)
        rest_p, rest_q = list(hand), list(hand)
        rest_p.remove(pt)
        rest_q.remove(qt)
        sp, sq = _sh(rest_p, groups), _sh(rest_q, groups)
        div = pt != qt
        sub = None
        if div:
            if pt == JOKER:
                sub = "高手打财神"
            elif qt == JOKER:
                sub = "生产打财神(高手留)"
            elif sp > sq:
                sub = "高手拆(向听更差)"
            elif sp < sq:
                sub = "高手向听更好"
            else:
                sub = "同向听·高手打%s/生产打%s" % (_ttype(pt), _ttype(qt))
        ctx = ["出牌·全部", "出牌·%s" % ("庄" if base["dealer"] else "闲"), "出牌·财神%s" % _jbin(jok),
               "出牌·%s" % _shbin(sq), "出牌·墙%s" % _wbin(snap.get("wall_remaining") or 0),
               "出牌·%s露" % ("0" if groups == 0 else "1" if groups == 1 else "2+")]
        base.update(fam="discard", keys=ctx, div=div, sub=sub, jok=_jbin(jok))
        return base
    # 响应窗口
    prod = bot._choose_action_production(snap) or {"action": "pass"}
    qa = prod.get("action") or "pass"
    if pa not in ("pass", "peng", "chi", "gang"):
        return None
    win = "碰窗" if rec["phase"] == "response_peng" else "吃窗"
    tenpai = _sh(hand, groups) <= 0
    base.update(fam="resp", keys=["%s·%s·生产=%s" % (win, "听牌" if tenpai else "未听", {"pass": "过", "peng": "碰", "chi": "吃", "gang": "杠"}.get(qa, qa))],
                div=(pa != qa))
    return base


# ---------------------------------------------------------------- 统计

def _boot_diff(rows, n_boot=400, seed=0):
    """分歧组 − 一致组 的得分差（按房聚类自助）。-> (diff, lo, hi) 或 None"""
    by = defaultdict(lambda: [0, 0.0, 0, 0.0])
    for r in rows:
        if r["delta"] is None:
            continue
        c = by[r["room"]]
        if r["div"]:
            c[2] += 1
            c[3] += r["delta"]
        else:
            c[0] += 1
            c[1] += r["delta"]

    def stat(cells):
        na = sum(c[0] for c in cells)
        nd = sum(c[2] for c in cells)
        if not na or not nd:
            return None
        return sum(c[3] for c in cells) / nd - sum(c[1] for c in cells) / na

    cells = list(by.values())
    point = stat(cells)
    if point is None:
        return None
    rng = random.Random(seed)
    bs = sorted(s for s in (stat(rng.choices(cells, k=len(cells))) for _ in range(n_boot)) if s is not None)
    if len(bs) < n_boot // 2:
        return point, float("nan"), float("nan")
    return point, bs[int(0.025 * len(bs))], bs[int(0.975 * len(bs)) - 1]


def _mean(xs):
    xs = [x for x in xs if x is not None]
    return sum(xs) / len(xs) if xs else float("nan")


def report(rows, top_names, say):
    good = [r for r in rows if "err" not in r]
    say("决策 %d 条（高手 %d，对照 %d）；生产报错跳过 %d" % (
        len(good), sum(r["who"] == "top" for r in good), sum(r["who"] == "ctrl" for r in good), len(rows) - len(good)))
    say("高手：" + "、".join(top_names))
    say()

    say("=== A. 逐人与生产的一致率（出牌 / 响应 / 可胡 / 杠）===")
    by_name = defaultdict(list)
    for r in good:
        by_name[r["name"] if r["who"] == "top" else "〈对照：其他玩家〉"].append(r)
    say("  %-22s %16s %16s %16s %16s" % ("玩家", "出牌", "响应", "可胡", "杠"))
    for name in list(top_names) + ["〈对照：其他玩家〉"]:
        rs = by_name.get(name, [])
        cells = []
        for fam in ("discard", "resp", "hu", "gang"):
            sub = [r for r in rs if r["fam"] == fam]
            cells.append("%5.1f%% (%6d)" % (100.0 * sum(not r["div"] for r in sub) / len(sub), len(sub)) if sub else "-")
        say("  %-22s %16s %16s %16s %16s" % (name, *cells))
    say()

    def table(title, fam_rows, keys_of, min_div=30):
        say(title)
        say("  %-34s %7s %8s %8s | %8s %8s %24s %7s" % (
            "类别", "高手n", "高手分歧", "对照分歧", "一致分/局", "分歧分/局", "分歧−一致 [95%CI]", "分歧胜率"))
        groups = defaultdict(lambda: {"top": [], "ctrl": []})
        for r in fam_rows:
            for k in keys_of(r):
                groups[k][r["who"]].append(r)
        out = []
        for k, g in groups.items():
            t, c = g["top"], g["ctrl"]
            nd = sum(r["div"] for r in t)
            if not t:
                continue
            bd = _boot_diff(t) if nd >= min_div else None
            out.append((k, t, c, nd, bd))
        out.sort(key=lambda x: x[0])
        for k, t, c, nd, bd in out:
            dt = 100.0 * nd / len(t)
            dc = 100.0 * sum(r["div"] for r in c) / len(c) if c else float("nan")
            agree = _mean([r["delta"] for r in t if not r["div"]])
            dv = _mean([r["delta"] for r in t if r["div"]])
            winr = _mean([1.0 if r["win"] else 0.0 for r in t if r["div"]])
            if bd:
                flag = "  <<<" if (bd[1] > 0 or bd[2] < 0) else ""
                ci = "%+6.2f [%+6.2f,%+6.2f]%s" % (bd[0], bd[1], bd[2], flag)
            else:
                ci = "（分歧<%d）" % min_div
            say("  %-34s %7d %7.1f%% %7.1f%% | %+8.2f %+8.2f %24s %6.1f%%" % (k, len(t), dt, dc, agree, dv, ci, 100 * winr))
        say()

    table("=== B. 可胡 / 自摸杠 / 吃碰杠响应：生产动作 vs 高手实际 ===",
          [r for r in good if r["fam"] in ("hu", "gang", "resp")], lambda r: r["keys"])
    disc = [r for r in good if r["fam"] == "discard"]
    table("=== C. 出牌分歧率（按局面拆，每行是边际分组）===", disc, lambda r: r["keys"])

    say("=== D. 出牌分歧的类型（占全部出牌决策的比例；结果差 = 该类型分歧 vs 全部一致）===")
    for jb in ("全部", "0", "1", "2+"):
        sel = disc if jb == "全部" else [r for r in disc if r.get("jok") == jb]
        t = [r for r in sel if r["who"] == "top"]
        c = [r for r in sel if r["who"] == "ctrl"]
        if not t:
            continue
        say("  -- 财神 %s：高手出牌 %d，对照 %d" % (jb, len(t), len(c)))
        subs = Counter(r["sub"] for r in t if r["div"])
        csubs = Counter(r["sub"] for r in c if r["div"])
        agree_t = [r for r in t if not r["div"]]
        for sub, n in subs.most_common(10):
            sel_rows = agree_t + [r for r in t if r["div"] and r["sub"] == sub]
            bd = _boot_diff(sel_rows) if n >= 30 else None
            ci = ("%+6.2f [%+6.2f,%+6.2f]%s" % (bd[0], bd[1], bd[2], "  <<<" if (bd[1] > 0 or bd[2] < 0) else "")) if bd else "（n<30）"
            say("    %-30s 高手 %5.1f%%  对照 %5.1f%% | %s" % (
                sub, 100.0 * n / len(t), 100.0 * csubs.get(sub, 0) / max(1, len(c)), ci))
    say()
    say("怎么读：找 ①高手分歧率明显高于对照 ②分歧−一致 的区间整体>0（标 <<<）的行——这是候选改进点；")
    say("  区间整体<0 的行说明高手在这类局面偏离生产反而更差（生产在这里比高手好）。")
    say("  结果差是相关不是因果（高手偏离时可能看到了更好的局面），且行数多、偶有假阳性；候选点要用 arena 同牌 A/B 确认。")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--jobs", type=int, default=int(os.environ.get("MJ_JOBS", "6")))
    ap.add_argument("--players", default=",".join(TOP_DEFAULT), help="逗号分隔的高手名字")
    ap.add_argument("--ctrl-keep", type=float, default=0.15, help="对照组（其他玩家）决策抽样比例")
    ap.add_argument("--ctrl-file-keep", type=float, default=0.3, help="没有高手的场，取多大比例做对照")
    ap.add_argument("--max-minutes", type=float, default=None)
    ap.add_argument("--report", action="store_true", help="只读缓存出表")
    ap.add_argument("--smoke", action="store_true")
    args = ap.parse_args()
    top = [p for p in args.players.split(",") if p]
    cfg = {"top": set(top), "ctrl_keep": args.ctrl_keep, "ctrl_file_keep": args.ctrl_file_keep}
    cache = CACHE + (".smoke" if args.smoke else "")
    t0 = time.time()
    if args.report:
        rows = [json.loads(l) for l in open(cache, encoding="utf-8")]
        stopped = False
    else:
        files = discover_files()
        if args.smoke:
            files = files[::max(1, len(files) // 20)][:20]
        deadline = t0 + args.max_minutes * 60 if args.max_minutes else None
        rows, stopped, done = [], False, 0
        pool = None if args.smoke or args.jobs <= 1 else Pool(args.jobs, initializer=_init, initargs=(cfg,))
        if pool is None:
            _init(cfg)
        it = pool.imap_unordered(scan, files, chunksize=4) if pool else map(scan, files)
        try:
            for got in it:
                rows.extend(got)
                done += 1
                if done % 200 == 0:
                    print("  已扫 %d / %d 场，%d 条，%.0fs" % (done, len(files), len(rows), time.time() - t0), flush=True)
                if deadline and time.time() > deadline:
                    stopped = True
                    break
        finally:
            if pool:
                pool.terminate()
                pool.join()
        os.makedirs(os.path.dirname(cache), exist_ok=True)
        with open(cache, "w", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
    lines = []

    def say(s=""):
        print(s)
        lines.append(s)

    say("=== top_player_diff（%.0fs%s）===" % (time.time() - t0, "；到时间上限提前停，只用了部分场" if stopped else ""))
    report(rows, top, say)
    if not args.smoke:
        os.makedirs(os.path.dirname(OUT), exist_ok=True)
        with open(OUT, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        print("\n已写 %s；缓存 %s" % (OUT, cache))


if __name__ == "__main__":
    main()
