"""route EV（mj/route_ev.py，开关 rule_route_ev_enabled 及 docs/IMPL_UNIFIED_EV_V2.md
的 P1~P5 扩展开关）离线验证。

只写这个脚本，不在本次任务里跑全量——由用户执行：

    python3 tools/route_ev_check.py --limit 20                    # 先 profiling，估算全量耗时
    python3 tools/route_ev_check.py --jobs 6                      # 全量（默认只测 rule_route_ev_enabled）
    python3 tools/route_ev_check.py --limit 200 --jobs 8 --on rev_speed_enabled=1   # 叠加测试某个 P2~P5 开关

「关开关」一侧 = 当前线上配置（``models/weights.json``，与 bot 实际读取的一致）；
「开开关」一侧 = 当前线上配置 + ``--on`` 给的覆盖（可多个，默认
``rule_route_ev_enabled=1``，即只测首版开关本身）。

输出四块：
  1. 覆盖：门清+持财神的弃牌决策点数；叠加逻辑改变选择的比例，按
     （财神数 x 真实对子数）分格。
  2. 与高手对照：只看叠加逻辑改变了选择的那些点，高手/我们的真实弃牌
     与「关开关」「开开关」两个版本的一致率。
  3. 校准：按叠加逻辑给出的七对系（C/D）期望值分档，对比该决策点所在的
     那一局最终是否以七对/七对·爆头胡牌——高手、我们分开报。
  4. 速度：范围内每个决策点，算一次「关」+一次「开」两个版本的耗时
     p50/p99/最大（毫秒）；改写次数按「开开关」一侧选中的路线字母分布。
"""
import argparse
import json
import os
import sys
import time
from collections import Counter, defaultdict
from multiprocessing import Pool

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))

from mining_common import discover_files, merge_rounds  # noqa: E402
from mining_common import seat_names  # noqa: E402
from baotou_funnel import MASTERS  # noqa: E402
from mj.fit import load_weights  # noqa: E402
from mj.route_ev import _pair_rich, best_route  # noqa: E402
from mj.ev import choose_route_discard  # noqa: E402
from mj.strategy import choose_discard  # noqa: E402
from mj.tiles import JOKER, TILE_INDEX, to_counts  # noqa: E402

OUR = "重生之我是雀神"
_OFF_WEIGHTS = {"rule_route_ev_enabled": 0}
_ON_WEIGHTS = {"rule_route_ev_enabled": 1}


def _parse_overrides(pairs):
    out = {}
    for item in pairs or []:
        key, _, raw = item.partition("=")
        try:
            value = int(raw)
        except ValueError:
            try:
                value = float(raw)
            except ValueError:
                value = raw
        out[key] = value
    return out


def _init_weights(off_weights, on_weights):
    global _OFF_WEIGHTS, _ON_WEIGHTS
    _OFF_WEIGHTS = off_weights
    _ON_WEIGHTS = on_weights


def _real_pairs(hand):
    counts = Counter(hand)
    return sum(1 for t, n in counts.items() if t != JOKER and n >= 2)


def scan(path):
    stats = Counter()
    calib = defaultdict(Counter)
    agree = Counter()
    timings = []
    try:
        with open(path, encoding="utf-8") as source:
            game = json.load(source)
    except (OSError, ValueError):
        return stats, calib, agree, timings
    names = seat_names(game)
    if len(names) != 4 or (not any(n in MASTERS for n in names) and OUR not in names):
        return stats, calib, agree, timings
    info = {r.get("round_no"): r for r in game.get("rounds") or []}
    for rnd in merge_rounds(game):
        meta = info.get(rnd["round_no"])
        if not meta or rnd.get("truncated") or not rnd.get("start_hands"):
            continue
        dealer = meta.get("dealer", rnd.get("dealer"))
        hands = [list(h) for h in rnd["start_hands"]]
        wall = 136 - sum(len(h) for h in hands)      # 与线上 wall_remaining 同口径：每摸一张减 1
        melds = [0, 0, 0, 0]
        seen = [0] * 34
        points = []          # (seat, group, decile) 局末回填是否七对系胡牌
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
                        stats["decision_total"] += 1
                        own = to_counts(hand)
                        visible = [min(4, seen[i] + own[i]) for i in range(34)]
                        in_scope = groups == 0 and hand.count(JOKER) >= 1
                        if in_scope:
                            stats["decision_in_scope"] += 1
                            cell = "j%d_p%d" % (min(hand.count(JOKER), 2), min(_real_pairs(hand), 6))
                            stats["cell_" + cell] += 1
                            # 对子不够的手，叠加逻辑在线上直接跳过（route_ev._pair_rich 预筛），选择必然不变；
                            # 这里同样跳过，只对「可能改写」的点做两次完整选牌（完整选牌持财神手约 0.2~0.3 s/次）。
                            rich = _pair_rich([tuple(to_counts(hand[:i] + hand[i + 1:])) for i in range(len(hand))])
                            stats["pair_rich"] += rich
                            if not rich:
                                hand.remove(tile)
                                seen[TILE_INDEX[tile]] += 1
                                continue
                            t0 = time.perf_counter()
                            # 与线上一致：带上 _ctx（剩余牌数、庄闲）；庄家走 choose_route_discard
                            ctx = {"wall": wall, "dealer": seat == dealer, "catch_play": False,
                                  "round_no": rnd["round_no"]}
                            is_d = seat == dealer
                            pick_fn = choose_route_discard if is_d else choose_discard
                            extra = {"_ctx": ctx, "dealer_hint": True} if is_d else {"_ctx": ctx}
                            off_pick = pick_fn(hand, groups, 0, 0, dict({"_weights": _OFF_WEIGHTS}, **extra), visible)
                            on_pick = pick_fn(hand, groups, 0, 0, dict({"_weights": _ON_WEIGHTS}, **extra), visible)
                            timings.append(time.perf_counter() - t0)
                            changed = off_pick != on_pick
                            stats["changed_total"] += changed
                            stats["changed_cell_" + cell] += changed
                            group = "master" if names[seat] in MASTERS else ("ours" if names[seat] == OUR else None)
                            if group and changed:
                                agree[group + "_n"] += 1
                                agree[group + "_off_eq_actual"] += off_pick == tile
                                agree[group + "_on_eq_actual"] += on_pick == tile
                            if group:
                                try:
                                    left = list(hand)
                                    left.remove(tile)
                                    value, route = best_route(to_counts(left), groups, ctx, _ON_WEIGHTS,
                                                              visible, routes=("A", "B", "C", "D"))
                                    decile = min(9, int(value / 50)) if route in ("C", "D") else 0
                                except Exception:
                                    route, decile = None, 0
                                points.append((seat, group, decile))
                                if changed:
                                    stats["route_" + (route or "none")] += 1
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
            stats["bad_round"] += 1
            continue
        # round_ended 事件里没有 winner 字段，赢家以对局元数据为准（与其它工具一致）
        winner = None if meta.get("is_draw") else meta.get("winner")
        pair_win = winner is not None and any("七对" in d for d in win_detail)
        for seat, group, decile in points:
            won_pair = pair_win and winner == seat
            bucket = calib[decile]
            bucket["n"] += 1
            bucket["won_pair"] += won_pair
            bucket[group + "_n"] += 1
            bucket[group + "_won_pair"] += won_pair
    return stats, calib, agree, timings


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--jobs", type=int, default=6)
    ap.add_argument("--limit", type=int, default=None, help="只跑前 N 个文件（先 profiling）")
    ap.add_argument("--on", nargs="*", default=["rule_route_ev_enabled=1"],
                    help="叠加到'开开关'一侧的权重覆盖 KEY=VALUE，可多个，如 --on rev_speed_enabled=1")
    args = ap.parse_args()
    prod_weights = load_weights()
    off_weights = dict(prod_weights)
    on_weights = dict(prod_weights, **_parse_overrides(args.on))
    print("关开关（当前线上配置）与开开关的差异键：%s" % {
        k: (off_weights.get(k), on_weights.get(k)) for k in on_weights if on_weights.get(k) != off_weights.get(k)})
    files = discover_files(limit=args.limit)
    t_start = time.time()
    total = Counter()
    calib_total = defaultdict(Counter)
    agree_total = Counter()
    all_timings = []
    with Pool(args.jobs, initializer=_init_weights, initargs=(off_weights, on_weights)) as pool:
        for done, (stats, calib, agree, timings) in enumerate(pool.imap_unordered(scan, files, chunksize=4), 1):
            total.update(stats)
            for k, v in calib.items():
                calib_total[k].update(v)
            agree_total.update(agree)
            all_timings.extend(timings)
            if done % 10 == 0 or done == len(files):
                spent = time.time() - t_start
                print("  进度 %d / %d 个文件，已用 %.0f 分钟，预计还需 %.0f 分钟；改变选择 %d 处（高手 %d / 我们 %d）"
                      % (done, len(files), spent / 60, spent / done * (len(files) - done) / 60,
                         total["changed_total"], agree_total["master_n"], agree_total["ours_n"]), flush=True)
    elapsed = time.time() - t_start
    print("\n耗时 %.1fs，%d 个文件（%.2fs/文件），--jobs %d" % (elapsed, len(files), elapsed / max(len(files), 1),
                                                        args.jobs))
    if args.limit:
        print("按此速率外推全量 ~1250 个文件：约 %.1f 分钟" % (elapsed / max(len(files), 1) * 1250 / 60))

    print("\n=== 1. 覆盖 ===")
    print("弃牌决策点总数 %d；门清+持财神（范围内）%d（%.2f%%）" % (
        total["decision_total"], total["decision_in_scope"],
        100.0 * total["decision_in_scope"] / max(total["decision_total"], 1)))
    print("范围内叠加逻辑改变选择：%d / %d = %.2f%%" % (
        total["changed_total"], total["decision_in_scope"],
        100.0 * total["changed_total"] / max(total["decision_in_scope"], 1)))
    print("\n%-10s %8s %10s" % ("财神x对子", "决策点", "改变率"))
    for jb in (0, 1, 2):
        for pb in range(0, 7):
            cell = "j%d_p%d" % (jb, pb)
            n = total["cell_" + cell]
            if not n:
                continue
            changed = total["changed_cell_" + cell]
            print("%-10s %8d %9.1f%%" % ("%s白/%d对" % ("2+" if jb == 2 else jb, pb), n, 100.0 * changed / n))

    print("\n=== 2. 与高手对照（只看叠加改变了选择的点）===")
    for group, label in (("master", "高手"), ("ours", "我们")):
        n = agree_total[group + "_n"]
        if not n:
            print("%s：无样本" % label)
            continue
        print("%s：n=%d，关开关时与真实一致 %.1f%%，开开关后 %.1f%%" % (
            label, n, 100.0 * agree_total[group + "_off_eq_actual"] / n,
            100.0 * agree_total[group + "_on_eq_actual"] / n))

    print("\n=== 3. 校准（叠加逻辑给出的七对系期望值分档 vs 本局是否七对系胡牌）===")
    print("%-6s %8s %8s %8s %8s %8s" % ("档位", "样本", "命中率", "高手n", "高手命中率", "我们命中率"))
    for decile in sorted(calib_total):
        c = calib_total[decile]
        n = c["n"]
        master_n = c["master_n"]
        ours_n = c["ours_n"]
        print("%-6d %8d %7.1f%% %8d %9s %9s" % (
            decile, n, 100.0 * c["won_pair"] / max(n, 1), master_n,
            ("%.1f%%" % (100.0 * c["master_won_pair"] / master_n)) if master_n else "  -  ",
            ("%.1f%%" % (100.0 * c["ours_won_pair"] / ours_n)) if ours_n else "  -  "))

    print("\n=== 4. 速度与改写路线分布 ===")
    if all_timings:
        all_timings.sort()
        avg_ms = 1000.0 * sum(all_timings) / len(all_timings)
        max_ms = 1000.0 * all_timings[-1]
        p50_ms = 1000.0 * all_timings[int(len(all_timings) * 0.50)]
        p99_ms = 1000.0 * all_timings[int(len(all_timings) * 0.99)]
        print("范围内决策点单次（含一次关+一次开，单进程）耗时：平均 %.2fms，p50 %.2fms，p99 %.2fms，最大 %.2fms（n=%d）" % (
            avg_ms, p50_ms, p99_ms, max_ms, len(all_timings)))
    else:
        print("没有范围内决策点，无法统计耗时")
    changed_by_route = {k[len("route_"):]: v for k, v in total.items() if k.startswith("route_")}
    if changed_by_route:
        print("改写次数按开开关一侧选中的路线分布：%s" % dict(sorted(changed_by_route.items())))

    print("\n坏局（重放失败）：%d" % total["bad_round"])


if __name__ == "__main__":
    main()
