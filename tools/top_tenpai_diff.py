"""top_player_diff 的细分：听牌时的出牌 + 自摸杠——这两类是唯一"高手偏离生产、结果反而更好"的信号。

top_player_diff.txt（2026-10-04）：
- 出牌·听牌（生产这一打后仍听牌）：高手分歧 15%，分歧−一致 +1.45 [+0.51, +2.37]；
- 自摸杠机会·生产=杠：高手不杠 70.6%（对照 50%），+4.16 [-1.28, +10.51]。
这里拆开看高手到底换成了什么：破听、转爆头、听口更宽/更窄、七对路线……每类给出比例和结果差（按房聚类自助）。

只取有真高手的场；高手的摸牌决策全取，同场其他玩家按 --ctrl-keep 抽样作对照。先用便宜的向听预筛，只有可能听牌/可杠时才调生产。

    python3 tools/top_tenpai_diff.py --smoke                               # 冒烟（20 个文件，单进程）
    caffeinate -i nice -n 15 python3 tools/top_tenpai_diff.py --jobs 6     # 全量；结果 reports/top_tenpai_diff.txt
    python3 tools/top_tenpai_diff.py --report                              # 只读缓存重出表
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
from mj.rules import baotou  # noqa: E402
from mj.shanten import combined_route, route_shanten  # noqa: E402
from mj.tiles import JOKER, JOKER_IDX, NSUITS, TILE_INDEX, to_counts  # noqa: E402
from top_player_diff import TOP_DEFAULT, _boot_diff, _mean  # noqa: E402

CACHE = os.path.join(ROOT, "tools", ".cache", "top_tenpai_rows.jsonl")
OUT = os.path.join(ROOT, "reports", "top_tenpai_diff.txt")
_CFG = {}
_FAN = {}


def _init(cfg):
    _CFG.update(cfg)
    ctx = frozen_file_weights()
    ctx.__enter__()


def _keep(name, phase, action):
    if phase != "draw" or name == OUR_NAME:
        return 0.0
    return 1.0 if name in _CFG["top"] else _CFG["ctrl_keep"]


def _visible(snap):
    seen = [0] * NSUITS
    for t in snap["my_hand"]:
        seen[TILE_INDEX[t]] += 1
    for pile in snap.get("discards") or []:
        for t in pile:
            if t in TILE_INDEX:
                seen[TILE_INDEX[t]] += 1
    for seat_melds in snap.get("melds") or []:
        for m in seat_melds:
            for t in (m.get("tiles") if isinstance(m, dict) else []) or []:
                if t in TILE_INDEX:
                    seen[TILE_INDEX[t]] += 1
    return seen


def _after(hand, tile, groups, seen):
    rest = list(hand)
    rest.remove(tile)
    c = to_counts(rest)
    sh, waits = combined_route(tuple(c), groups)
    bt = sh <= 0 and baotou(tuple(c), groups)
    live = sum(max(0, 4 - seen[i]) for i in waits) if sh <= 0 else 0
    return {"sh": sh, "bt": bool(bt), "live": live, "nw": len(waits), "jok": c[JOKER_IDX]}


def _joker_conv(c13, groups):
    """这手 13 张再摸一张财神，能否打掉某张变成爆头听（"摸财神转爆头"的潜力）。"""
    c14 = list(c13)
    c14[JOKER_IDX] += 1
    for i in range(NSUITS):
        if i == JOKER_IDX or not c14[i]:
            continue
        c14[i] -= 1
        ok = baotou(tuple(c14), groups)
        c14[i] += 1
        if ok:
            return True
    return False


def _conv_of(hand, tile, groups):
    rest = list(hand)
    rest.remove(tile)
    return _joker_conv(to_counts(rest), groups)


def scan(path):
    try:
        with open(path, encoding="utf-8") as f:
            game = json.load(f)
    except (OSError, ValueError):
        return []
    names = seat_names(game)
    if not any(n in _CFG["top"] for n in names):
        return []
    _FAN.clear()
    for r in game.get("rounds") or []:
        _FAN[r.get("round_no")] = r.get("multiplier") or 1
    rows = []
    for rec in iter_decisions(path, keep=_keep, seed=0):
        try:
            row = _one(rec)
        except Exception:
            row = {"err": 1}
        if row:
            rows.append(row)
    return rows


def _one(rec):
    snap, act = rec["snapshot"], rec["action"]
    me = snap["seat"]
    god = snap.get("god") or {}
    if god.get("catch_play") and god.get("god_discarder_seat") != me:
        return None
    hand = snap["my_hand"]
    groups = len(snap["melds"][me])
    drawn = snap.get("drawn_tile")
    wall = snap.get("wall_remaining") or 0
    can_gang = wall > 20 and any(
        t != JOKER and (hand.count(t) >= 4 or any(isinstance(m, dict) and m.get("kind") == "peng" and t in (m.get("tiles") or [])
                                                  for m in snap["melds"][me]))
        for t in set(hand))
    min_sh = min(route_shanten(tuple(to_counts([x for j, x in enumerate(hand) if j != hand.index(t)])), groups) for t in set(hand))
    if min_sh > 0 and not can_gang:
        return None
    if bot.hu_result(snap):
        return None
    prod = bot._choose_action_production(snap) or {}
    qa, pa = prod.get("action"), act.get("action")
    base = {"room": rec["room_name"], "who": "top" if rec["name"] in _CFG["top"] else "ctrl", "name": rec["name"],
            "dealer": snap.get("dealer") == me, "delta": rec["ret"]["delta"], "win": rec["ret"]["win"],
            "fan": _FAN.get(rec["round_no"], 1) if rec["ret"]["win"] else 0,
            "jok": min(2, hand.count(JOKER)), "melds": min(2, groups),
            "wall": "早" if wall - 20 > 40 else "中" if wall - 20 > 16 else "晚"}
    if qa == "gang" or pa == "gang":
        gt = prod.get("tile") if qa == "gang" else act.get("tile")
        kind = "暗杠" if hand.count(gt) >= 4 else "补杠"
        pre = route_shanten(tuple(to_counts([x for j, x in enumerate(hand) if j != hand.index(drawn)])), groups) if drawn in hand else 9
        base.update(fam="gang", kind=kind, prod_gang=qa == "gang", div=(pa == "gang") != (qa == "gang"),
                    tenpai_pre=pre <= 0, gtile_type="字" if TILE_INDEX.get(gt, 0) >= 27 else "数")
        return base
    if qa != "discard" or pa != "discard":
        return None
    qt, pt = prod.get("tile"), act.get("tile")
    if qt not in hand or pt not in hand:
        return None
    seen = _visible(snap)
    aq = _after(hand, qt, groups, seen)
    if aq["sh"] > 0:
        return None            # 只看"生产这一打后听牌"的局面
    base.update(fam="tenpai", div=qt != pt, prod_bt=aq["bt"], prod_live=aq["live"], prod_nw=aq["nw"])
    if qt != pt or random.random() < 0.25:
        base["prod_conv"] = (not aq["bt"]) and _conv_of(hand, qt, groups)
    if qt != pt:
        ap = _after(hand, pt, groups, seen)
        if ap["sh"] > 0:
            sub = "高手破听"
        elif pt == JOKER:
            sub = "高手打财神(仍听)"
        elif ap["bt"] and not aq["bt"]:
            sub = "高手转爆头"
        elif aq["bt"] and not ap["bt"]:
            sub = "高手放弃爆头"
        elif aq["bt"] and ap["bt"]:
            sub = "都爆头·换牌"
        elif ap["live"] > aq["live"]:
            sub = "同听·高手听口更宽"
        elif ap["live"] < aq["live"]:
            sub = "同听·高手听口更窄"
        else:
            sub = "同听·听口一样"
        base.update(sub=sub, p_live=ap["live"], dlive=ap["live"] - aq["live"], p_nw=ap["nw"],
                    p_conv=ap["sh"] <= 0 and (not ap["bt"]) and _conv_of(hand, pt, groups))
    return base


def _ci(rows, min_n=30):
    nd = sum(r["div"] for r in rows)
    if nd < min_n:
        return "（分歧<%d）" % min_n
    bd = _boot_diff(rows)
    return "%+6.2f [%+6.2f,%+6.2f]%s" % (bd[0], bd[1], bd[2], "  <<<" if (bd[1] > 0 or bd[2] < 0) else "")


def report(rows, say):
    good = [r for r in rows if "err" not in r]
    ten = [r for r in good if r["fam"] == "tenpai"]
    gang = [r for r in good if r["fam"] == "gang"]
    say("听牌出牌决策 %d（高手 %d / 对照 %d）；杠机会 %d；报错 %d" % (
        len(ten), sum(r["who"] == "top" for r in ten), sum(r["who"] == "ctrl" for r in ten), len(gang), len(rows) - len(good)))
    say()

    say("=== 1. 听牌出牌：按局面拆（分歧−一致 = 高手偏离生产 vs 跟生产一样，本局得分差）===")
    say("  %-22s %7s %8s %8s %26s" % ("局面", "高手n", "高手分歧", "对照分歧", "分歧−一致 [95%CI]"))
    cuts = [("全部", lambda r: True)]
    cuts += [("庄" if d else "闲", (lambda d: lambda r: r["dealer"] == d)(d)) for d in (True, False)]
    cuts += [("财神%s" % ("2+" if j == 2 else j), (lambda j: lambda r: r["jok"] == j)(j)) for j in (0, 1, 2)]
    cuts += [("%s露" % ("2+" if m == 2 else m), (lambda m: lambda r: r["melds"] == m)(m)) for m in (0, 1, 2)]
    cuts += [("墙%s" % w, (lambda w: lambda r: r["wall"] == w)(w)) for w in ("早", "中", "晚")]
    cuts += [("生产选的是爆头听", lambda r: r["prod_bt"]), ("生产选的非爆头听", lambda r: not r["prod_bt"])]
    cuts += [("生产听口活牌≤4", lambda r: not r["prod_bt"] and r["prod_live"] <= 4),
             ("生产听口活牌5-8", lambda r: not r["prod_bt"] and 5 <= r["prod_live"] <= 8),
             ("生产听口活牌≥9", lambda r: not r["prod_bt"] and r["prod_live"] >= 9)]
    for name, f in cuts:
        t = [r for r in ten if r["who"] == "top" and f(r)]
        c = [r for r in ten if r["who"] == "ctrl" and f(r)]
        if not t:
            continue
        say("  %-22s %7d %7.1f%% %7.1f%% %26s" % (name, len(t), 100.0 * sum(r["div"] for r in t) / len(t),
                                               100.0 * sum(r["div"] for r in c) / max(1, len(c)), _ci(t)))
    say()

    say("=== 2. 听牌出牌：高手换成了什么（占高手听牌出牌的比例；结果差 = 该类分歧 vs 全部一致）===")
    for jname, jf in (("全部", lambda r: True), ("财神0", lambda r: r["jok"] == 0), ("财神1", lambda r: r["jok"] == 1),
                      ("财神2+", lambda r: r["jok"] == 2)):
        t = [r for r in ten if r["who"] == "top" and jf(r)]
        c = [r for r in ten if r["who"] == "ctrl" and jf(r)]
        if not t:
            continue
        say("  -- %s：高手 %d，对照 %d" % (jname, len(t), len(c)))
        agree = [r for r in t if not r["div"]]
        cs = Counter(r["sub"] for r in c if r["div"])
        for sub, n in Counter(r["sub"] for r in t if r["div"]).most_common():
            rs = [r for r in t if r["div"] and r["sub"] == sub]
            extra = ""
            if sub.startswith("同听"):
                extra = "  活牌差均值 %+.1f" % _mean([r["dlive"] for r in rs])
            say("    %-18s 高手 %5.1f%%  对照 %5.1f%% | %s | 分歧胜率 %4.1f%%%s" % (
                sub, 100.0 * n / len(t), 100.0 * cs.get(sub, 0) / max(1, len(c)), _ci(agree + rs),
                100 * _mean([1.0 if r["win"] else 0.0 for r in rs]), extra))
    say()

    say("=== 3. 自摸杠：生产要杠时高手不杠（按杠型 / 财神 / 杠前是否听牌 / 牌型拆）===")
    say("  %-26s %7s %8s %8s %26s" % ("局面", "高手n", "高手不杠", "对照不杠", "不杠−杠 [95%CI]"))
    pg = [r for r in gang if r["prod_gang"]]
    gcuts = [("全部", lambda r: True), ("暗杠", lambda r: r["kind"] == "暗杠"), ("补杠", lambda r: r["kind"] == "补杠"),
             ("杠前已听牌", lambda r: r["tenpai_pre"]), ("杠前未听", lambda r: not r["tenpai_pre"]),
             ("财神0", lambda r: r["jok"] == 0), ("财神1+", lambda r: r["jok"] >= 1),
             ("字牌杠", lambda r: r["gtile_type"] == "字"), ("数牌杠", lambda r: r["gtile_type"] == "数"),
             ("庄", lambda r: r["dealer"]), ("闲", lambda r: not r["dealer"])]
    for name, f in gcuts:
        t = [r for r in pg if r["who"] == "top" and f(r)]
        c = [r for r in pg if r["who"] == "ctrl" and f(r)]
        if not t:
            continue
        say("  %-26s %7d %7.1f%% %7.1f%% %26s" % (name, len(t), 100.0 * sum(r["div"] for r in t) / len(t),
                                               100.0 * sum(r["div"] for r in c) / max(1, len(c)), _ci(t, 20)))
    say("  逐人（生产要杠时）：" + "；".join(
        "%s 不杠 %d/%d" % (n, sum(r["div"] for r in pg if r["name"] == n), sum(1 for r in pg if r["name"] == n))
        for n in sorted({r["name"] for r in pg if r["who"] == "top"})))
    say()
    say("=== 4. 听牌换牌的去向：摸财神转爆头潜力（conv）与胡牌番数 ===")
    t_all = [r for r in ten if r["who"] == "top"]
    agree = [r for r in t_all if not r["div"]]
    samp = [r for r in agree if "prod_conv" in r]
    say("  一致组（抽样 %d）：生产这手有转爆头潜力 %.1f%%" % (len(samp), 100 * _mean([1.0 if r["prod_conv"] else 0.0 for r in samp])))
    wins = [r for r in agree if r["win"]]
    say("  一致组：胜率 %.1f%%，胡时平均番 %.2f，胡时 2番+ %.1f%%" % (
        100 * _mean([1.0 if r["win"] else 0.0 for r in agree]), _mean([r["fan"] for r in wins]),
        100 * _mean([1.0 if r["fan"] >= 2 else 0.0 for r in wins])))
    for sub in ("同听·高手听口更窄", "同听·高手听口更宽", "同听·听口一样", "高手破听"):
        rs = [r for r in t_all if r["div"] and r.get("sub") == sub]
        if not rs:
            continue
        w = [r for r in rs if r["win"]]
        say("  %-16s n=%-5d 胜率 %4.1f%% 胡时均番 %.2f 2番+ %4.1f%% | 生产手有潜力 %4.1f%% 高手手有潜力 %4.1f%% 高手听牌种类1张 %4.1f%%（生产 %4.1f%%）" % (
            sub, len(rs), 100 * _mean([1.0 if r["win"] else 0.0 for r in rs]), _mean([r["fan"] for r in w]),
            100 * _mean([1.0 if r["fan"] >= 2 else 0.0 for r in w]),
            100 * _mean([1.0 if r.get("prod_conv") else 0.0 for r in rs]), 100 * _mean([1.0 if r.get("p_conv") else 0.0 for r in rs]),
            100 * _mean([1.0 if r.get("p_nw") == 1 else 0.0 for r in rs]), 100 * _mean([1.0 if r.get("prod_nw") == 1 else 0.0 for r in rs])))
    chg = [r for r in t_all if r["div"] and r.get("sub", "").startswith("同听") and r.get("sub") != "同听·听口一样"]
    say("  换听口的分歧按（生产潜力→高手潜力）拆，结果差 = 这一格 vs 全部一致：")
    for pc in (False, True):
        for qc in (False, True):
            rs = [r for r in chg if bool(r.get("prod_conv")) == pc and bool(r.get("p_conv")) == qc]
            if not rs:
                continue
            w = [r for r in rs if r["win"]]
            say("    生产%s→高手%s  n=%-5d %s | 胜率 %4.1f%% 胡时均番 %.2f" % (
                "有" if pc else "无", "有" if qc else "无", len(rs), _ci(agree + rs), 100 * _mean([1.0 if r["win"] else 0.0 for r in rs]),
                _mean([r["fan"] for r in w])))
    say()
    say("怎么读：第 2 节找 ①高手占比明显高于对照 ②结果差区间整体>0 的类型——那就是高手听牌时比生产多做对的事；")
    say("  第 3 节若\"不杠−杠\"在某类里区间整体>0，说明生产在这类杠得太多（注意：C2 杠门槛的 arena 消融是无效，要对照着看）。")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--jobs", type=int, default=int(os.environ.get("MJ_JOBS", "6")))
    ap.add_argument("--players", default=",".join(TOP_DEFAULT))
    ap.add_argument("--ctrl-keep", type=float, default=0.5, help="同场其他玩家的摸牌决策抽样比例")
    ap.add_argument("--max-minutes", type=float, default=None)
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--smoke", action="store_true")
    args = ap.parse_args()
    cfg = {"top": set(p for p in args.players.split(",") if p), "ctrl_keep": args.ctrl_keep}
    cache = CACHE + (".smoke" if args.smoke else "")
    t0 = time.time()
    stopped = False
    if args.report:
        rows = [json.loads(l) for l in open(cache, encoding="utf-8")]
    else:
        files = discover_files()
        if args.smoke:
            files = files[::max(1, len(files) // 20)][:20]
        deadline = t0 + args.max_minutes * 60 if args.max_minutes else None
        rows, done = [], 0
        pool = None if args.smoke or args.jobs <= 1 else Pool(args.jobs, initializer=_init, initargs=(cfg,))
        if pool is None:
            _init(cfg)
        it = pool.imap_unordered(scan, files, chunksize=4) if pool else map(scan, files)
        try:
            for got in it:
                rows.extend(got)
                done += 1
                if done % 400 == 0:
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

    say("=== top_tenpai_diff（%.0fs%s）===" % (time.time() - t0, "；到时间上限提前停，只用了部分场" if stopped else ""))
    report(rows, say)
    if not args.smoke:
        with open(OUT, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        print("\n已写 %s；缓存 %s" % (OUT, cache))


if __name__ == "__main__":
    main()
