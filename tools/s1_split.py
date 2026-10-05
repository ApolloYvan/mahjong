"""S1（弃胡转爆头）按庄家/闲家拆开：触发次数、最终胡牌率、净得分差（相对"当场胡"），带自助法置信区间（按房聚类）；
以及庄家 S1 的"盈亏平衡成功率"。

    python3 tools/s1_split.py --smoke                       # 冒烟（只扫最后一个日志文件的前 200 MB 左右）
    caffeinate -i nice -n 15 python3 tools/s1_split.py --jobs 6 --max-minutes 30    # 全量：扫 logs/*.jsonl（~6GB），结果缓存

数据：``logs/*.jsonl`` 的 decision 记录（我们自己 bot 的真实摸牌决策）里，"手牌构成非爆头胡牌、但动作不是 hu"的决策 = S1 弃胡
（同一局多次只计一次，口径同 ``tools/batch_review.py::s1_audit``）；对局结局取 ``models/events`` 里同 game_id 同局的
``rounds[].scores``（本局增量）与 ``round_ended``（番数）。"当场胡"的得分 = ``payout(当时的番数, 庄/闲)``（自摸，``mj.rules.payout``）。
每个触发记一行：庄/闲、是否最终自己胡、实际得分、当场胡得分、当时番数、最终番数。**净得分差 = 实际得分 − 当场胡得分**。
置信区间：按房（game_id 第二段）有放回重抽 2000 次。

盈亏平衡成功率（庄家，也给闲家）——**"Astra 的公式"我在仓库里没找到原文，下面是按你描述的口径（计入对手自摸时的支付、
不考虑流局和连庄）自己推的，请核对**：

    弃胡成功（概率 p）后自摸得 G_win = payout(F, 庄/闲)，F = 最终番数；失败（概率 1-p）假定是对手自摸，付出 L；
    当场胡得 G_now = payout(f, 庄/闲)。令 p*G_win - (1-p)*L = G_now，得  p* = (G_now + L) / (G_win + L)。

L 取实测：触发后"别人自摸赢"的那些局里我们的得分取反的均值；F/f 取实测（成功样本里最终番数/当时番数的均值）。
另给 f=1,2,4 且 F=2f（转爆头翻倍）的理论表。实测成功率 p_obs 和 p* 比：p_obs > p* 才赚。
"""
import argparse
import glob
import json
import os
import random
import sys
import time
from collections import Counter, defaultdict
from multiprocessing import Pool

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))
os.environ.setdefault("MJ_WEIGHTS_NO_FILE", "1")

from mining_common import seat_names  # noqa: E402
from mj.rules import evaluate, payout  # noqa: E402
from mj.tiles import JOKER, TILE_INDEX, to_counts  # noqa: E402

CACHE = os.path.join(ROOT, "tools", ".cache", "s1_split_rows.jsonl")
_GAMES = {}


def load_game(game_id):
    if game_id in _GAMES:
        return _GAMES[game_id]
    game = None
    for pat in ("models/events/%s.json", "tools/models/events/%s.json"):
        path = os.path.join(ROOT, pat % game_id)
        if os.path.exists(path):
            try:
                with open(path, encoding="utf-8") as f:
                    game = json.JSONDecoder().raw_decode(f.read())[0]
            except (OSError, ValueError):
                game = None
            break
    if len(_GAMES) > 40:
        _GAMES.clear()
    _GAMES[game_id] = game
    return game


def round_outcome(game, round_no):
    rnd = next((r for r in game.get("rounds", []) if r.get("round_no") == round_no), None)
    if not rnd:
        return None
    fan = None
    for block in game.get("blocks", []):
        if block.get("round_no") == round_no:
            for ev in block.get("events", []):
                if ev.get("type") == "round_ended":
                    fan = (ev.get("data") or {}).get("fan")
    return rnd, fan


def scan_log(task):
    path, max_bytes = task
    rows = {}
    read = 0
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            read += len(line)
            if max_bytes and read > max_bytes:
                break
            if '"kind": "decision"' not in line[:80] or '"phase": "draw"' not in line:
                continue
            try:
                p = json.loads(line)["payload"]
            except (ValueError, KeyError):
                continue
            hand, drawn, seat = p.get("hand") or [], p.get("drawn_tile"), p.get("seat")
            melds = p.get("melds") or []
            groups = len(melds[seat]) if isinstance(melds, list) and len(melds) == 4 and isinstance(seat, int) else 0
            if not drawn or drawn not in hand or len(hand) + 3 * groups != 14:
                continue
            if (p.get("decision") or {}).get("action") == "hu":
                continue
            rest = list(hand)
            rest.remove(drawn)
            counts = tuple(to_counts(rest))
            god = p.get("god") or {}
            try:
                now = evaluate(counts, TILE_INDEX[drawn], chain_count=god.get("chain_count") or 0,
                               piao=p.get("piao") or 0, meld_groups=groups)
            except (KeyError, TypeError):
                continue
            if not now or now["baotou"]:
                continue
            gid = p.get("game_id")
            key = (gid, p.get("round_no"))
            if key in rows:
                continue
            game = load_game(gid) if gid else None
            if not game:
                continue
            out = round_outcome(game, p.get("round_no"))
            if not out:
                continue
            rnd, fan_end = out
            if not isinstance(rnd.get("scores"), list) or len(rnd["scores"]) != 4:
                continue
            dealer = p.get("dealer") == seat
            won = rnd.get("winner") == seat and not rnd.get("is_draw")
            rows[key] = {"game": gid, "room": gid.split("_")[1] if "_" in gid else gid, "round": p.get("round_no"),
                         "dealer": dealer, "jokers": counts[TILE_INDEX[JOKER]] + (drawn == JOKER), "melds": groups,
                         "wall": p.get("wall_remaining"), "won": bool(won), "draw": bool(rnd.get("is_draw")),
                         "other_won": (not won) and not rnd.get("is_draw") and rnd.get("winner") is not None,
                         "got": rnd["scores"][seat], "fan_now": now["fan"], "fan_end": fan_end if won else None,
                         "hu_now": payout(now["fan"], dealer=dealer)}
    return list(rows.values())


def boot_ci(rows, fn, n=2000, seed=0):
    """按房聚类自助：返回 (点估计, lo, hi)。"""
    if not rows:
        return float("nan"), float("nan"), float("nan")
    by = defaultdict(list)
    for r in rows:
        by[r["room"]].append(r)
    rooms = list(by)
    rng = random.Random(seed)
    stats = []
    for _ in range(n):
        sample = []
        for rm in rng.choices(rooms, k=len(rooms)):
            sample.extend(by[rm])
        stats.append(fn(sample))
    stats.sort()
    return fn(rows), stats[int(n * 0.025)], stats[int(n * 0.975)]


def mean_net(rs):
    return sum(r["got"] - r["hu_now"] for r in rs) / len(rs) if rs else float("nan")


def win_rate(rs):
    return sum(r["won"] for r in rs) / len(rs) if rs else float("nan")


def breakeven(rows, dealer):
    rs = [r for r in rows if r["dealer"] == dealer]
    if not rs:
        return None
    won = [r for r in rs if r["won"] and r["fan_end"]]
    lost = [r for r in rs if r["other_won"]]
    L = -sum(r["got"] for r in lost) / len(lost) if lost else float("nan")
    ratio = sum(r["fan_end"] / r["fan_now"] for r in won) / len(won) if won else float("nan")
    f = sum(r["fan_now"] for r in rs) / len(rs)
    g_now = payout(f, dealer=dealer)
    g_win = payout(f * ratio, dealer=dealer)
    p_star = (g_now + L) / (g_win + L) if g_win + L else float("nan")
    return {"n": len(rs), "p_obs": win_rate(rs), "L": L, "ratio": ratio, "f": f, "p_star": p_star,
            "n_lost": len(lost), "n_won": len(won)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--jobs", type=int, default=int(os.environ.get("MJ_JOBS", "6")))
    ap.add_argument("--logs", default=os.path.join(ROOT, "logs", "*.jsonl"))
    ap.add_argument("--max-minutes", type=float, default=None)
    ap.add_argument("--no-cache", action="store_true")
    ap.add_argument("--smoke", action="store_true")
    args = ap.parse_args()
    started = time.time()
    cache = CACHE + (".smoke" if args.smoke else "")
    rows = []
    if os.path.exists(cache) and not args.no_cache:
        with open(cache, encoding="utf-8") as f:
            rows = [json.loads(l) for l in f]
        print("读取缓存 %s：%d 个触发（--no-cache 重扫）" % (cache, len(rows)))
    else:
        files = sorted(glob.glob(args.logs))
        if args.smoke:
            files = files[-1:]
        tasks = [(p, 150_000_000 if args.smoke else None) for p in files]
        deadline = started + args.max_minutes * 60 if args.max_minutes else None
        pool = Pool(args.jobs) if args.jobs > 1 and not args.smoke else None
        it = pool.imap_unordered(scan_log, tasks) if pool else map(scan_log, tasks)
        uniq = {}
        stopped = False
        try:
            for got in it:
                for r in got:
                    uniq[(r["game"], r["round"])] = r
                if deadline and time.time() > deadline:
                    stopped = True
                    break
        finally:
            if pool:
                pool.terminate()
                pool.join()
        rows = list(uniq.values())
        if not stopped:
            os.makedirs(os.path.dirname(cache), exist_ok=True)
            with open(cache, "w", encoding="utf-8") as f:
                for r in rows:
                    f.write(json.dumps(r, ensure_ascii=False) + "\n")
        else:
            print("到 --max-minutes 上限，只用已扫完的日志文件出结果（未写缓存）")
    print("=== s1_split（%d 个 S1 触发，%d 个房；%.0fs） ===" % (len(rows), len({r["room"] for r in rows}), time.time() - started))
    if not rows:
        print("没有触发记录")
        return
    print("%-4s %5s %8s %-26s %-26s %s" % ("角色", "触发", "最终胡率", "胡率 95%CI(按房)", "净得分差/次 95%CI", "合计净差"))
    for role, flag in (("庄", True), ("闲", False), ("全部", None)):
        rs = rows if flag is None else [r for r in rows if r["dealer"] == flag]
        if not rs:
            continue
        w, wl, wh = boot_ci(rs, win_rate)
        m, ml, mh = boot_ci(rs, mean_net)
        print("%-4s %5d %7.1f%% [%5.1f%%, %5.1f%%]          %+7.2f [%+7.2f, %+7.2f]   %+d" % (
            role, len(rs), 100 * w, 100 * wl, 100 * wh, m, ml, mh, sum(r["got"] - r["hu_now"] for r in rs)))
    print("按（财神数,副露数）分格（庄/闲分开；次数 / 胡率 / 平均净差）：")
    cells = defaultdict(list)
    for r in rows:
        cells[("庄" if r["dealer"] else "闲", min(r["jokers"], 2), min(r["melds"], 2))].append(r)
    for key in sorted(cells):
        rs = cells[key]
        print("  %s %s白/%s露 n=%-3d 胡率 %3.0f%% 净差 %+7.2f" % (key[0], "2+" if key[1] == 2 else key[1], "2+" if key[2] == 2 else key[2],
                                                         len(rs), 100 * win_rate(rs), mean_net(rs)))
    print("盈亏平衡成功率 p*=(G_now+L)/(G_win+L)（自推口径：计入对手自摸付分，不含流局/连庄；Astra 原公式未在仓库找到，请核对）：")
    for role, flag in (("庄", True), ("闲", False)):
        b = breakeven(rows, flag)
        if not b:
            continue
        verdict = "实测胡率 %.1f%% %s p*" % (100 * b["p_obs"], ">" if b["p_obs"] > b["p_star"] else "<=") if b["p_star"] == b["p_star"] else ""
        print("  %s：n=%d（成功 %d / 败给别家自摸 %d） 平均当时番 %.2f，成功时 F/f=%.2f，败时付出 L=%.1f → p*=%.1f%%；%s" % (
            role, b["n"], b["n_won"], b["n_lost"], b["f"], b["ratio"], b["L"], 100 * b["p_star"], verdict))
    print("  理论表（F=2f）：  f   庄 p*(L=庄实测)   闲 p*(L=闲实测)")
    bd, bn = breakeven(rows, True), breakeven(rows, False)
    for f in (1, 2, 4):
        cols = []
        for b, dealer in ((bd, True), (bn, False)):
            if b and b["L"] == b["L"]:
                cols.append("%.1f%%" % (100 * (payout(f, dealer=dealer) + b["L"]) / (payout(2 * f, dealer=dealer) + b["L"])))
            else:
                cols.append("-")
        print("                    %d      %s            %s" % (f, cols[0], cols[1]))


if __name__ == "__main__":
    main()
