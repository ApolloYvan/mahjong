"""T0(a)（docs/IMPL_TABLE_EV.md）：按线上可观测特征拟合"这一摸是否自摸胡"的概率表。

    python3 tools/hazard_fit.py
    python3 tools/hazard_fit.py --jobs 6 --no-cache   # 忽略缓存全量重扫

样本：所有文件、所有座位的每一次摸牌；标签：这一摸是否自摸胡（本游戏只能自摸，
``round_ended`` 非流局时赢家的最后一次摸牌 = 胡牌摸），只能确定性推出，不用猜。

特征（线上可观测，逐一核对过——见下）：
  role     ：这一摸时是否庄家（0/1）
  melds    ：这一摸时副露组数（0/1/2/3+），全公开信息
  draw_idx ：本座位本局第几次摸牌（1..19，19 代表 19+），回合顺序公开可数
  late_mid ：本座位最近 <=3 张弃牌里，数牌 3~7（中张）的张数（0..3），牌河公开
未采用 tsumogiri_streak（摸切/手切）：对手实际摸到的牌本身是隐藏信息，只有完整
回放日志能重建，线上 bot 拿不到对手的 drawn_tile，故按"拿不到的不用"跳过。

样本 <200 的格子按 late_mid → melds → role 顺序逐级回退到更粗的格子（先丢
late_mid，再丢 melds，再丢 role，draw_idx 永远保留——它是自摸率最主要的驱动
维度），backoff 字段记录用的是哪一级。

输出 models/hazard_table.json：
    {"cells": {"role:melds:draw:late_mid": {"h":.., "n":.., "wins":.., "backoff":".."}},
     "fan_by_role": {"dealer": .., "idle": ..},
     "meta": {...}}
另输出 models/opp_profile.json：{"name": {"ratio":.., "n":.., "actual":.., "predicted":..}}
（速度倍率 = (实际自摸数+30)/(表预测自摸数+30)，样本 <300 摸的名字不输出）。
"""
import argparse
import hashlib
import json
import os
import pickle
import sys
from collections import Counter, defaultdict
from multiprocessing import Pool

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))

from mining_common import discover_files, merge_rounds, seat_names  # noqa: E402
from baotou_funnel import MASTERS, OUR  # noqa: E402

CACHE_PATH = os.path.join(ROOT, "tools", ".cache", "hazard_fit.pkl")
OUT_TABLE = os.path.join(ROOT, "models", "hazard_table.json")
OUT_PROFILE = os.path.join(ROOT, "models", "opp_profile.json")
MIN_CELL_N = 200
MIN_PROFILE_DRAWS = 300


def _melds_bucket(n):
    return min(n, 3)


def _draw_bucket(n):
    return min(n, 19)


def _is_mid(tile):
    return len(tile) == 2 and tile[0] in "34567"


def _key(role, melds, draw, late_mid):
    return "%d:%d:%d:%d" % (role, melds, draw, late_mid)


def scan(path):
    """返回 (cells, name_cells, fan_role)。
    cells：feature_key -> Counter{n, wins}
    name_cells：(name, feature_key) -> Counter{n, wins}（供 opp_profile 用；
      predicted 值要等全量 cells 表算出最终 h 之后才能在 main() 里二次算，
      这里只存原始计数，不重复扫文件）
    fan_role：Counter{dealer_sum, dealer_n, idle_sum, idle_n}
    """
    cells = defaultdict(Counter)
    name_cells = defaultdict(Counter)
    fan_role = Counter()
    try:
        with open(path, encoding="utf-8") as source:
            game = json.load(source)
    except (OSError, ValueError):
        return cells, name_cells, fan_role
    names = seat_names(game)
    if len(names) != 4:
        return cells, name_cells, fan_role
    info = {r.get("round_no"): r for r in game.get("rounds") or []}
    for rnd in merge_rounds(game):
        meta = info.get(rnd["round_no"])
        if not meta or rnd.get("truncated") or not rnd.get("start_hands"):
            continue
        dealer = meta.get("dealer", rnd.get("dealer"))
        draw_idx = [0, 0, 0, 0]
        melds = [0, 0, 0, 0]
        discard_hist = [[], [], [], []]
        samples = [[], [], [], []]   # 每座位本局的 (feature_key,) 列表，最后一个可能被标 win
        try:
            for ev in rnd["events"]:
                kind, seat, tile = ev["type"], ev.get("seat"), ev.get("tile")
                data = ev.get("data") or {}
                if kind == "tile_drawn":
                    draw_idx[seat] += 1
                    role = 1 if seat == dealer else 0
                    fk = _key(role, _melds_bucket(melds[seat]), _draw_bucket(draw_idx[seat]),
                             sum(1 for t in discard_hist[seat][-3:] if _is_mid(t)))
                    samples[seat].append(fk)
                elif kind == "tile_discarded":
                    discard_hist[seat].append(tile)
                    if len(discard_hist[seat]) > 3:
                        discard_hist[seat] = discard_hist[seat][-3:]
                elif kind == "chi":
                    melds[seat] += 1
                elif kind == "peng":
                    melds[seat] += 1
                elif kind == "gang":
                    if data.get("kind") != "bu":
                        melds[seat] += 1
        except (ValueError, KeyError, IndexError, TypeError):
            continue
        winner = None if meta.get("is_draw") else meta.get("winner")
        win_fan = None
        for ev in rnd["events"]:
            if ev.get("type") == "round_ended":
                d = ev.get("data") or {}
                if not d.get("draw") and d.get("fan"):
                    win_fan = d["fan"]
        for seat in range(4):
            for i, fk in enumerate(samples[seat]):
                is_win = winner == seat and i == len(samples[seat]) - 1
                cells[fk]["n"] += 1
                cells[fk]["wins"] += is_win
                name_cells[(names[seat], fk)]["n"] += 1
                name_cells[(names[seat], fk)]["wins"] += is_win
        if winner is not None and win_fan is not None:
            role_label = "dealer" if winner == dealer else "idle"
            fan_role[role_label + "_sum"] += win_fan
            fan_role[role_label + "_n"] += 1
    return cells, name_cells, fan_role


def _resolve_backoff(cells):
    """返回 {full_key: (h, n, wins, backoff_label)}，按 late_mid→melds→role 逐级回退。"""
    l1 = defaultdict(Counter)   # (role,melds,draw)
    l2 = defaultdict(Counter)   # (role,draw)
    l3 = defaultdict(Counter)   # (draw,)
    parsed = {}
    for fk, c in cells.items():
        role, melds, draw, late_mid = (int(x) for x in fk.split(":"))
        parsed[fk] = (role, melds, draw, late_mid)
        l1[(role, melds, draw)]["n"] += c["n"]
        l1[(role, melds, draw)]["wins"] += c["wins"]
        l2[(role, draw)]["n"] += c["n"]
        l2[(role, draw)]["wins"] += c["wins"]
        l3[(draw,)]["n"] += c["n"]
        l3[(draw,)]["wins"] += c["wins"]

    resolved = {}
    for fk, c in cells.items():
        role, melds, draw, late_mid = parsed[fk]
        if c["n"] >= MIN_CELL_N:
            resolved[fk] = (c["wins"] / c["n"], c["n"], c["wins"], "full")
            continue
        c1 = l1[(role, melds, draw)]
        if c1["n"] >= MIN_CELL_N:
            resolved[fk] = (c1["wins"] / c1["n"], c1["n"], c1["wins"], "drop_late_mid")
            continue
        c2 = l2[(role, draw)]
        if c2["n"] >= MIN_CELL_N:
            resolved[fk] = (c2["wins"] / c2["n"], c2["n"], c2["wins"], "drop_melds")
            continue
        c3 = l3[(draw,)]
        if c3["n"] >= MIN_CELL_N:
            resolved[fk] = (c3["wins"] / c3["n"], c3["n"], c3["wins"], "drop_role")
        else:
            n = max(1, c3["n"])
            resolved[fk] = (c3["wins"] / n, c3["n"], c3["wins"], "global_thin")
    return resolved


def _scan_path(path):
    return path, scan(path)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--jobs", type=int, default=6)
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--no-cache", action="store_true")
    args = ap.parse_args()
    files = discover_files(limit=args.limit)

    with open(os.path.abspath(__file__), "rb") as own:
        code_ver = hashlib.md5(own.read()).hexdigest()
    cache = {}
    if not args.no_cache:
        try:
            with open(CACHE_PATH, "rb") as source:
                saved = pickle.load(source)
            if saved.get("ver") == code_ver:
                cache = saved.get("files") or {}
        except (OSError, ValueError, EOFError, pickle.UnpicklingError, AttributeError):
            cache = {}

    def file_key(path):
        st = os.stat(path)
        return (st.st_mtime, st.st_size)

    cells_total = defaultdict(Counter)
    name_cells_total = defaultdict(Counter)
    fan_role_total = Counter()
    todo, keys = [], {}
    for path in files:
        k = file_key(path)
        keys[path] = k
        hit = cache.get(path)
        if hit and hit[0] == k:
            cells, name_cells, fan_role = hit[1]
        else:
            todo.append(path)
            continue
        for fk, c in cells.items():
            cells_total[fk].update(c)
        for nk, c in name_cells.items():
            name_cells_total[nk].update(c)
        fan_role_total.update(fan_role)
    print("  缓存命中 %d 个文件，需要扫描 %d 个" % (len(files) - len(todo), len(todo)), flush=True)

    fresh = {}
    if todo:
        with Pool(args.jobs) as pool:
            for done, (path, (cells, name_cells, fan_role)) in enumerate(
                    pool.imap_unordered(_scan_path, todo, chunksize=8), 1):
                plain_cells = {k: dict(v) for k, v in cells.items()}
                plain_names = {k: dict(v) for k, v in name_cells.items()}
                fresh[path] = (keys[path], (plain_cells, plain_names, dict(fan_role)))
                for fk, c in plain_cells.items():
                    cells_total[fk].update(c)
                for nk, c in plain_names.items():
                    name_cells_total[nk].update(c)
                fan_role_total.update(fan_role)
                if done % 200 == 0 or done == len(todo):
                    print("  已扫 %d / %d" % (done, len(todo)), flush=True)
    if not args.no_cache:
        merged = {p: cache[p] for p in files if p in cache and p not in fresh}
        merged.update(fresh)
        os.makedirs(os.path.dirname(CACHE_PATH), exist_ok=True)
        tmp = CACHE_PATH + ".tmp"
        with open(tmp, "wb") as out:
            pickle.dump({"ver": code_ver, "files": merged}, out, protocol=pickle.HIGHEST_PROTOCOL)
        os.replace(tmp, CACHE_PATH)

    resolved = _resolve_backoff(cells_total)
    fan_by_role = {
        "dealer": (fan_role_total["dealer_sum"] / fan_role_total["dealer_n"]) if fan_role_total["dealer_n"] else 1.3,
        "idle": (fan_role_total["idle_sum"] / fan_role_total["idle_n"]) if fan_role_total["idle_n"] else 1.3,
    }
    total_draws = sum(c["n"] for c in cells_total.values())
    total_wins = sum(c["wins"] for c in cells_total.values())
    table = {
        "cells": {fk: {"h": h, "n": n, "wins": w, "backoff": b} for fk, (h, n, w, b) in resolved.items()},
        "fan_by_role": fan_by_role,
        "meta": {"total_draws": total_draws, "total_wins": total_wins, "files": len(files),
                 "backoff_order": ["late_mid", "melds", "role"], "min_cell_n": MIN_CELL_N},
    }
    os.makedirs(os.path.dirname(OUT_TABLE), exist_ok=True)
    with open(OUT_TABLE, "w", encoding="utf-8") as out:
        json.dump(table, out, ensure_ascii=False, indent=2)
    print("\n已写 %s（%d 个格子，覆盖 %d 次摸牌，总体自摸率 %.2f%%）" % (
        OUT_TABLE, len(resolved), total_draws, 100.0 * total_wins / max(total_draws, 1)))
    print("fan_by_role: 庄 %.2f  闲 %.2f" % (fan_by_role["dealer"], fan_by_role["idle"]))

    # ---- opp_profile：实际自摸数 vs 用最终表预测的自摸数 ----
    by_name = defaultdict(lambda: Counter())
    for (name, fk), c in name_cells_total.items():
        h = resolved.get(fk, (0.0,))[0]
        by_name[name]["n"] += c["n"]
        by_name[name]["actual"] += c["wins"]
        by_name[name]["predicted"] += h * c["n"]
    profile = {}
    for name, c in by_name.items():
        if c["n"] < MIN_PROFILE_DRAWS:
            continue
        ratio = (c["actual"] + 30) / (c["predicted"] + 30)
        profile[name] = {"ratio": ratio, "n": c["n"], "actual": c["actual"], "predicted": c["predicted"]}
    with open(OUT_PROFILE, "w", encoding="utf-8") as out:
        json.dump(profile, out, ensure_ascii=False, indent=2)
    print("已写 %s（%d 个名字，样本阈值 %d 摸）" % (OUT_PROFILE, len(profile), MIN_PROFILE_DRAWS))

    # ---- 校准：按预测值（h）分 10 档，预测 vs 实际自摸率 ----
    print("\n=== 校准：按格子预测值分 10 档 ===")
    ranked = sorted(resolved.items(), key=lambda kv: kv[1][0])
    total_n = sum(v[1] for v in resolved.values()) or 1
    print("%-6s %10s %10s %10s" % ("档位", "样本数", "预测自摸率", "实际自摸率"))
    bucket_n = total_n / 10.0
    acc = 0
    bucket = []
    decile = 0
    for fk, (h, n, wins, backoff) in ranked:
        bucket.append((h, n, wins))
        acc += n
        if acc >= bucket_n * (decile + 1) or fk == ranked[-1][0]:
            bn = sum(b[1] for b in bucket)
            bw = sum(b[2] for b in bucket)
            bh = sum(b[0] * b[1] for b in bucket) / bn if bn else 0.0
            print("%-6d %10d %9.2f%% %9.2f%%" % (decile, bn, 100 * bh, 100 * bw / max(bn, 1)))
            bucket = []
            decile += 1
            if decile >= 10:
                break

    print("\n=== 分组：高手 / 我们 / 其他 实际/预测 自摸比 ===")
    groups = Counter()
    group_pred = Counter()
    for name, c in by_name.items():
        g = "高手" if name in MASTERS else ("我们" if name == OUR else "其他")
        groups[g + "_actual"] += c["actual"]
        groups[g + "_n"] += c["n"]
        group_pred[g] += c["predicted"]
    for g in ("高手", "我们", "其他"):
        n = groups[g + "_n"]
        if not n:
            continue
        pred = group_pred[g]
        print("%-4s n=%-8d 实际/预测 = %.3f（实际自摸率 %.2f%%，预测自摸率 %.2f%%）" % (
            g, n, groups[g + "_actual"] / max(pred, 1e-9), 100 * groups[g + "_actual"] / n, 100 * pred / n))


if __name__ == "__main__":
    main()
