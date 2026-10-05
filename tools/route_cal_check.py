"""C1b（docs/IMPL_ROUTE_CAL.md）：只用留出文件（md5(文件名)%5==0，与
tools/route_sim_fit.py 共用同一份 _is_holdout 判据，两边一定不重叠），检验
"理论估算" vs "查表"（models/route_cal.json）两种账本对胜率的校准好坏。

    python3 tools/route_cal_check.py --jobs 8

只有查表模型的 Brier 分数、对数损失都比理论估算低，且七对系不再被系统性
低估，才建议启用 rev_route_cal_enabled（见 docs/IMPL_ROUTE_CAL.md 交付部分）。
"""
import argparse
import json
import math
import os
import sys
from collections import Counter, defaultdict
from multiprocessing import Pool

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))

from mining_common import discover_files, merge_rounds, seat_names  # noqa: E402
from baotou_funnel import MASTERS  # noqa: E402
from route_sim_fit import _is_holdout  # noqa: E402
from mj.route_ev import route_win_probabilities  # noqa: E402
from mj.tiles import JOKER, TILE_INDEX, to_counts  # noqa: E402

OUR = "重生之我是雀神"
THEORY_WEIGHTS = {"rule_route_ev_enabled": 1}
TABLE_WEIGHTS = {"rule_route_ev_enabled": 1, "rev_route_cal_enabled": 1}
MODELS = ("理论", "查表")


def _real_pairs(hand):
    counts = Counter(hand)
    return sum(1 for t, n in counts.items() if t != JOKER and n >= 2)


def scan(path):
    """返回 points：每项 (group, decile理论, decile查表, best_route理论,
    best_route查表, prob理论, prob查表, won_any, won_pair)。"""
    points = []
    if not _is_holdout(path):
        return points
    try:
        with open(path, encoding="utf-8") as source:
            game = json.load(source)
    except (OSError, ValueError):
        return points
    names = seat_names(game)
    if len(names) != 4 or (not any(n in MASTERS for n in names) and OUR not in names):
        return points
    info = {r.get("round_no"): r for r in game.get("rounds") or []}
    for rnd in merge_rounds(game):
        meta = info.get(rnd["round_no"])
        if not meta or rnd.get("truncated") or not rnd.get("start_hands"):
            continue
        dealer = meta.get("dealer", rnd.get("dealer"))
        hands = [list(h) for h in rnd["start_hands"]]
        wall = 136 - sum(len(h) for h in hands)
        melds = [0, 0, 0, 0]
        seen = [0] * 34
        pending = []   # (seat, group, ctx, counts13, groups, visible)
        winner, win_detail = None, []
        try:
            for ev in rnd["events"]:
                kind, seat, tile = ev["type"], ev.get("seat"), ev.get("tile")
                data = ev.get("data") or {}
                if kind == "tile_drawn":
                    hands[seat].append(tile)
                    wall -= 1
                elif kind == "tile_discarded":
                    hand = hands[seat]
                    groups = melds[seat]
                    if len(hand) + 3 * groups == 14 and not data.get("catch_play"):
                        own = to_counts(hand)
                        visible = [min(4, seen[i] + own[i]) for i in range(34)]
                        in_scope = groups == 0 and hand.count(JOKER) >= 1 and _real_pairs(hand) >= 0
                        # 无财神的手（任意副露）只有 A 路线，P2 速度前瞻就作用在这类手上，
                        # 查表同样会改变它的胜率估计，所以也要检验。
                        no_joker = hand.count(JOKER) == 0
                        if in_scope or no_joker:
                            group = "高手" if names[seat] in MASTERS else ("我们" if names[seat] == OUR else None)
                            if group and no_joker:
                                group += "·无白"
                            if group:
                                left = list(hand)
                                left.remove(tile)
                                counts13 = tuple(to_counts(left))
                                ctx = {"wall": wall, "dealer": seat == dealer, "round_no": rnd["round_no"]}
                                pending.append((seat, group, ctx, counts13, groups, tuple(visible)))
                    hand.remove(tile)
                    seen[TILE_INDEX[tile]] += 1
                elif kind == "chi":
                    used = list(data.get("tiles") or [])
                    used.remove(tile)
                    for t in used:
                        hands[seat].remove(t)
                        seen[TILE_INDEX[t]] += 1
                    melds[seat] += 1
                elif kind == "peng":
                    for _ in range(2):
                        hands[seat].remove(tile)
                    seen[TILE_INDEX[tile]] += 2
                    melds[seat] += 1
                elif kind == "gang":
                    take = {"an": 4, "ming": 3, "bu": 1}[data.get("kind")]
                    for _ in range(take):
                        hands[seat].remove(tile)
                    seen[TILE_INDEX[tile]] += take
                    if data.get("kind") != "bu":
                        melds[seat] += 1
                elif kind == "round_ended":
                    win_detail = data.get("detail") or []
        except (ValueError, KeyError, IndexError, TypeError):
            continue
        winner = None if meta.get("is_draw") else meta.get("winner")
        pair_win = winner is not None and any("七对" in d for d in win_detail)
        for seat, group, ctx, counts13, groups, visible in pending:
            routes = ("A",) if group.endswith("·无白") else ("A", "B", "C", "D")
            theory = route_win_probabilities(counts13, groups, ctx, THEORY_WEIGHTS, visible, routes)
            table = route_win_probabilities(counts13, groups, ctx, TABLE_WEIGHTS, visible, routes)
            if not theory or not table:
                continue
            t_route = max(theory, key=theory.get)
            b_route = max(table, key=table.get)
            won_any = 1 if winner == seat else 0
            won_pair = 1 if (pair_win and winner == seat) else 0
            points.append((group, t_route, theory[t_route], b_route, table[b_route], won_any, won_pair))
    return points


def _decile(p):
    return min(9, int(max(0.0, min(0.999, p)) * 10))


def _brier_logloss(pairs):
    if not pairs:
        return None, None
    n = len(pairs)
    brier = sum((p - y) ** 2 for p, y in pairs) / n
    eps = 1e-9
    logloss = -sum(y * math.log(max(p, eps)) + (1 - y) * math.log(max(1 - p, eps)) for p, y in pairs) / n
    return brier, logloss


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--jobs", type=int, default=8)
    ap.add_argument("--limit-files", type=int, default=None)
    args = ap.parse_args()
    files = discover_files(limit=args.limit_files)
    holdout = [f for f in files if _is_holdout(f)]
    print("留出文件 %d / %d 个" % (len(holdout), len(files)))

    all_points = []
    with Pool(args.jobs) as pool:
        for done, part in enumerate(pool.imap_unordered(scan, holdout, chunksize=8), 1):
            all_points.extend(part)
            if done % 200 == 0 or done == len(holdout):
                print("  已扫 %d / %d，累计决策点 %d" % (done, len(holdout), len(all_points)), flush=True)

    print("\n=== 1. 路线级校准：模型认为最优路线是 C/D 的点，预测七对系胡率 vs 实际 ===")
    for group in ("高手", "我们"):
        print("-- %s --" % group)
        print("%-6s | %8s %10s %10s | %8s %10s %10s" % (
            "档位", "理论n", "理论预测", "理论实际", "查表n", "查表预测", "查表实际"))
        theory_buckets = defaultdict(lambda: [0, 0.0, 0])   # decile -> [n, sum_pred, sum_actual]
        table_buckets = defaultdict(lambda: [0, 0.0, 0])
        for g, t_route, t_prob, b_route, b_prob, won_any, won_pair in all_points:
            if g != group:
                continue
            if t_route in ("C", "D"):
                b = theory_buckets[_decile(t_prob)]
                b[0] += 1
                b[1] += t_prob
                b[2] += won_pair
            if b_route in ("C", "D"):
                b = table_buckets[_decile(b_prob)]
                b[0] += 1
                b[1] += b_prob
                b[2] += won_pair
        for d in range(10):
            tn, tp, ta = theory_buckets.get(d, [0, 0.0, 0])
            bn, bp, ba = table_buckets.get(d, [0, 0.0, 0])
            if not tn and not bn:
                continue
            print("%-6d | %8d %9.1f%% %9.1f%% | %8d %9.1f%% %9.1f%%" % (
                d, tn, 100 * tp / max(tn, 1), 100 * ta / max(tn, 1),
                bn, 100 * bp / max(bn, 1), 100 * ba / max(bn, 1)))

    print("\n=== 2. 整体校准：最优路线胜率分档，预测 vs 实际胡牌率（任意路线） ===")
    theory_pairs_all = defaultdict(list)   # group -> [(p,y)]
    table_pairs_all = defaultdict(list)
    for group in ("高手", "我们", "高手·无白", "我们·无白"):
        print("-- %s --" % group)
        print("%-6s | %8s %10s %10s | %8s %10s %10s" % (
            "档位", "理论n", "理论预测", "理论实际", "查表n", "查表预测", "查表实际"))
        theory_buckets = defaultdict(lambda: [0, 0.0, 0])
        table_buckets = defaultdict(lambda: [0, 0.0, 0])
        for g, t_route, t_prob, b_route, b_prob, won_any, won_pair in all_points:
            if g != group:
                continue
            theory_pairs_all[group].append((t_prob, won_any))
            table_pairs_all[group].append((b_prob, won_any))
            tb = theory_buckets[_decile(t_prob)]
            tb[0] += 1
            tb[1] += t_prob
            tb[2] += won_any
            bb = table_buckets[_decile(b_prob)]
            bb[0] += 1
            bb[1] += b_prob
            bb[2] += won_any
        for d in range(10):
            tn, tp, ta = theory_buckets.get(d, [0, 0.0, 0])
            bn, bp, ba = table_buckets.get(d, [0, 0.0, 0])
            if not tn and not bn:
                continue
            print("%-6d | %8d %9.1f%% %9.1f%% | %8d %9.1f%% %9.1f%%" % (
                d, tn, 100 * tp / max(tn, 1), 100 * ta / max(tn, 1),
                bn, 100 * bp / max(bn, 1), 100 * ba / max(bn, 1)))

    print("\n=== 3. 汇总：Brier 分数 / 对数损失（越低越好） ===")
    print("%-8s %10s %10s %10s %10s" % ("组", "理论Brier", "理论LogLoss", "查表Brier", "查表LogLoss"))
    for group in ("高手", "我们", "全体", "高手·无白", "我们·无白", "全体·无白"):
        if group.startswith("全体"):
            suffix = group[len("全体"):]
            t_pairs = theory_pairs_all["高手" + suffix] + theory_pairs_all["我们" + suffix]
            b_pairs = table_pairs_all["高手" + suffix] + table_pairs_all["我们" + suffix]
        else:
            t_pairs = theory_pairs_all[group]
            b_pairs = table_pairs_all[group]
        t_brier, t_logloss = _brier_logloss(t_pairs)
        b_brier, b_logloss = _brier_logloss(b_pairs)
        if t_brier is None:
            print("%-8s 无样本" % group)
            continue
        print("%-8s %10.4f %10.4f %10.4f %10.4f" % (group, t_brier, t_logloss, b_brier, b_logloss))

    print("\n读法：查表模型两个数字都比理论估算低，且第 1 张表里查表的'实际'列在低档也明显")
    print("不再是 0（对照 docs/IMPL_ROUTE_CAL.md 提到的'七对希望最低档仍有 10.7%% 实际胡成'），")
    print("才建议开 rev_route_cal_enabled；否则说明模拟本身也需要检查（样本不够/贪心策略有问题）。")


if __name__ == "__main__":
    main()
