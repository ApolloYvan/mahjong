"""我们做庄时的真实出牌，用「庄家路径」（现行：dealer_hint + route 出牌）和「闲家路径」（dealer_route_enabled=0）各算一遍，
看两者在多少手上不同、不同时谁更靠近爆头。只读回放，不跑对局。

背景（2026-10-07 发现）：持财神拟合打分的 plan_gain 特征在拟合时按闲家口径算（rules=None，slow1 溢价 3000），
线上庄家带 dealer_hint 改用 slow1_dealer=9000，特征值放大到 3 倍，乘上拟合权重 fj_plan_gain=-40，
庄家比闲家更狠地压掉"留财神慢一步做爆头"的打法——与 dealer_hint 的设计意图相反。

    python3 tools/dealer_replay.py --since 2026-10-06T05:00
"""
import argparse
import os
import sys
from collections import Counter
from multiprocessing import Pool

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))
sys.path.insert(0, os.path.join(ROOT, "tools", "train"))
os.environ["MJ_WEIGHTS_NO_FILE"] = "0"   # build_dataset 默认置 1（=用默认权重）；这里必须用线上 models/weights.json

from baotou_funnel import recent_rooms  # noqa: E402
from build_dataset import iter_decisions  # noqa: E402
from mining_common import OUR_NAME, discover_files  # noqa: E402
from mj import bot  # noqa: E402
from mj.joker_ev import to_baotou_distance  # noqa: E402
from mj.rules import baotou  # noqa: E402
from mj.shanten import route_shanten  # noqa: E402
from mj.strategy import choose_discard as idle_discard  # noqa: E402
from mj.tiles import JOKER, to_counts  # noqa: E402


def _after(hand, tile, groups):
    rest = list(hand)
    rest.remove(tile)
    c = tuple(to_counts(rest))
    d = to_baotou_distance(c, groups)
    return route_shanten(c, groups), (9 if d is None else d), baotou(c, groups)


def scan(path):
    out = []
    for rec in iter_decisions(path, keep=lambda nm, ph, ac: 1.0 if nm == OUR_NAME and ph == "draw" else 0.0, seed=0):
        snap = rec["snapshot"]
        if snap.get("dealer") != snap.get("seat"):
            continue
        god = snap.get("god") or {}
        if god.get("catch_play") and god.get("god_discarder_seat") != snap.get("seat"):
            continue
        if bot.hu_result(snap):
            continue
        hand, groups, chain, piao, rules, visible, use_route = bot._discard_inputs(snap, {})
        if not hand or chain or piao:
            continue
        try:
            d_pick = bot.choose_route_discard(hand, groups, chain, piao, rules, visible) if use_route else None
            plain = {k: v for k, v in rules.items() if k != "dealer_hint"}
            plain["_ctx"] = {**(rules.get("_ctx") or {}), "dealer": False}   # 闲家路径连同 route EV 的庄闲上下文一起换成闲家
            i_pick = idle_discard(hand, groups, chain, piao, plain, visible)
        except Exception:
            continue
        if d_pick is None or i_pick is None:
            continue
        j = min(hand.count(JOKER), 2)
        row = {"j": j, "same": d_pick == i_pick, "real": rec["action"].get("tile")}
        if d_pick != i_pick:
            row["d"] = _after(hand, d_pick, groups)
            row["i"] = _after(hand, i_pick, groups)
            row["ex"] = "手[%s] 露%d 墙%s：庄家路径打%s 闲家路径打%s" % (
                " ".join(sorted(hand)), groups, snap.get("wall_remaining"), d_pick, i_pick)
        out.append(row)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", required=True)
    ap.add_argument("--jobs", type=int, default=6)
    args = ap.parse_args()
    rooms = recent_rooms(args.since)
    files = [p for p in discover_files() if os.path.basename(p).split("_r")[0] in rooms]
    rows = []
    with Pool(args.jobs) as pool:
        for part in pool.imap_unordered(scan, files, chunksize=2):
            rows += part
    print("=== 我们做庄的出牌决策（%s 之后 %d 房）：庄家路径 vs 闲家路径 ===" % (args.since, len(rooms)))
    print("%-8s %7s %8s | 不同时：%-22s %-22s %-14s" % ("起手财神", "决策数", "不同比例", "向听 庄/闲", "离爆头步数 庄/闲",
                                                     "打完即爆头听 庄/闲"))
    for j in (0, 1, 2):
        sub = [r for r in rows if r["j"] == j]
        diff = [r for r in sub if not r["same"]]
        if not sub:
            continue
        if diff:
            n = len(diff)
            cells = ("%.2f / %.2f" % (sum(r["d"][0] for r in diff) / n, sum(r["i"][0] for r in diff) / n),
                     "%.2f / %.2f" % (sum(r["d"][1] for r in diff) / n, sum(r["i"][1] for r in diff) / n),
                     "%d / %d" % (sum(r["d"][2] for r in diff), sum(r["i"][2] for r in diff)))
        else:
            cells = ("-", "-", "-")
        print("%-8s %7d %7.1f%% | %-22s %-22s %-14s" % ("%s 白" % ("2+" if j == 2 else j), len(sub),
                                                         100.0 * len(diff) / len(sub), *cells))
    ex = [r for r in rows if not r["same"] and r["j"] >= 1]
    print("\n持财神时两条路径不同的样例（前 15 个）：")
    for r in ex[:15]:
        print("  " + r["ex"] + "   向听/离爆头 庄%s 闲%s" % (r["d"][:2], r["i"][:2]))


if __name__ == "__main__":
    main()
