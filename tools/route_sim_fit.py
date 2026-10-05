"""C1a（docs/IMPL_ROUTE_CAL.md）：离线模拟"坚持走某条路线"，得出真实的走完
概率曲线，供 mj/route_ev.py 在线查表替换理论估算（开关 rev_route_cal_enabled）。

    python3 tools/route_sim_fit.py --states 4000 --jobs 8
    python3 tools/route_sim_fit.py --states 100 --jobs 4      # 先小规模估算耗时

按文件名 md5 分组：md5(basename) % 5 != 0 的文件用于拟合，== 0 的留给
tools/route_cal_check.py 做留出检验——两个工具都用同一个 _is_holdout()，
不要各写一份，否则拟合/检验集可能重叠。

产出 models/route_cal.json：
    {"cells": {"R:d:live_bucket:jokers:melds": {"F": [F(1)..F(18)], "n":.., "backoff":".."}},
     "meta": {...}}
F(k) = 模拟里"坚持走这条路线"在 k 步（本家摸牌次数）内真正胡牌（win_standard /
seven_pairs 判定，不是"距离到 0"）的经验概率。

性能如实说明：本工具在 bot 正在跑局时开发，硬性约束不允许在此期间跑脚本，
所以没有跑过任何规模的计时。--rollouts 默认沿用任务书给的 24；如果
--states 4000 --jobs 8 明显超过 30 分钟，先用 --states 100 判断单状态耗时
再外推，觉得太慢就调低 --rollouts（不改默认值意味着我没有证据支持调低）。
"""
import argparse
import hashlib
import json
import os
import pickle
import random
import sys
import time
from collections import Counter, defaultdict
from multiprocessing import Pool

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))

from mining_common import discover_files, merge_rounds, seat_names  # noqa: E402
from mj.discard_features import baotou_waits  # noqa: E402
from mj.joker_ev import to_baotou_distance  # noqa: E402
from mj.route_ev import _advance_tiles, _pair_baotou_distance, _routes, route_cal_key  # noqa: E402
from mj.rules import seven_pairs, win_standard  # noqa: E402
from mj.shanten import pair_shanten, pair_ukeire, shanten, ukeire  # noqa: E402
from mj.tiles import JOKER_IDX, NSUITS, TILE_INDEX, to_counts  # noqa: E402

CACHE_PATH = os.path.join(ROOT, "tools", ".cache", "route_sim_fit.pkl")
OUT_PATH = os.path.join(ROOT, "models", "route_cal.json")
MAX_STEPS = 18
MIN_CELL_N = 30
ROUTES = ("A", "B", "C", "D")


def _is_holdout(path):
    """md5(文件名) % 5 == 0 留作 C1b 检验；两个工具共用同一份判据，不要各写一份。"""
    h = hashlib.md5(os.path.basename(path).encode("utf-8")).hexdigest()
    return int(h, 16) % 5 == 0


# ---------------------------------------------------------------------------
# 每条路线的距离函数 / 完成判定 / "坚持该路线"贪心弃牌用的前进牌函数。
# 完成判定用真实的胡牌函数（win_standard / seven_pairs），不是"距离到 0"——
# 距离到 0 只是听牌，还要再摸中那张才算数。
#
# D 路线的前进牌用通用 _advance_tiles(算到 _pair_baotou_distance) 而不是
# mj.route_ev._pair_baotou_ukeire——后者明确跳过财神（背景里的已知问题 (2)），
# 会让模拟里的"坚持 D"策略在摸到财神时不知道该往哪弃，人为拉低 D 的完成率。
# 这里只修策略（怎么下贪心弃牌），表的分档键仍然用 mj.route_ev._routes() 的
# 原始 p_rate/w_rate（含这个已知偏差）——因为在线查表也用同一份 _routes()
# 计算 live_for_lookup，键的口径必须和产线完全一致，否则查表会查错格子。
# ---------------------------------------------------------------------------

def _dist_A(c, m):
    return shanten(c, m)


def _dist_B(c, m):
    return to_baotou_distance(c, m)


def _dist_C(c, m):
    return pair_shanten(c)


def _dist_D(c, m):
    return _pair_baotou_distance(c)


DIST_FN = {"A": _dist_A, "B": _dist_B, "C": _dist_C, "D": _dist_D}


def _win_standard_fn(c14, m):
    return win_standard(c14, m)


def _win_pairs_fn(c14, m):
    return seven_pairs(c14) is not None


WIN_FN = {"A": _win_standard_fn, "B": _win_standard_fn, "C": _win_pairs_fn, "D": _win_pairs_fn}


def _advance_A(c, m):
    return ukeire(c, m)


def _advance_B(c, m):
    d = to_baotou_distance(c, m)
    if d == 1:
        return baotou_waits(c, m)
    return _advance_tiles(c, m, to_baotou_distance)


def _advance_C(c, m):
    return pair_ukeire(c)


def _advance_D(c, m):
    # _advance_tiles 以 dist_fn(counts, meld_groups) 两个参数调用；_pair_baotou_distance
    # 只收 counts，所以传两参数的包装 _dist_D。
    return _advance_tiles(c, m, _dist_D)


ADVANCE_FN = {"A": _advance_A, "B": _advance_B, "C": _advance_C, "D": _advance_D}


def feasible_routes(counts13, meld_groups):
    """一个状态里哪些路线的距离函数有定义（B/D 需要手上有财神；C/D 需要门清，
    七对本身就要求 14 张全部来自个人手牌，副露之后不可能）。"""
    out = ["A"]
    if counts13[JOKER_IDX] >= 1 and to_baotou_distance(counts13, meld_groups) is not None:
        out.append("B")
    if meld_groups == 0:
        out.append("C")
        if counts13[JOKER_IDX] >= 1 and _pair_baotou_distance(counts13) is not None:
            out.append("D")
    return out


# ---------------------------------------------------------------------------
# 阶段 1：从对局文件里抽取候选状态（弃牌决策点，弃牌后的 13 张口径）。
# ---------------------------------------------------------------------------

def scan_states(path):
    """返回该文件里的候选状态列表：每项 (counts13, meld_groups, visible, wall)。
    只保留至少一条可行路线距离在 0..4 的状态（表只覆盖这个范围，其余状态
    对本工具无用，早点丢掉省内存）。"""
    out = []
    try:
        with open(path, encoding="utf-8") as source:
            game = json.load(source)
    except (OSError, ValueError):
        return out
    names = seat_names(game)
    if len(names) != 4:
        return out
    info = {r.get("round_no"): r for r in game.get("rounds") or []}
    for rnd in merge_rounds(game):
        meta = info.get(rnd["round_no"])
        if not meta or rnd.get("truncated") or not rnd.get("start_hands"):
            continue
        hands = [list(h) for h in rnd["start_hands"]]
        wall = 136 - sum(len(h) for h in hands)
        melds = [0, 0, 0, 0]
        seen = [0] * NSUITS   # 全桌可见：四家牌河 + 全部副露（不含手牌，下面单独加）
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
                        left = list(hand)
                        left.remove(tile)
                        counts13 = tuple(to_counts(left))
                        routes = feasible_routes(counts13, groups)
                        if any((DIST_FN[r](counts13, groups) or 99) <= 4 for r in routes):
                            own = to_counts(left)
                            # 可见 = 自己弃牌后的手牌 + 全桌牌河/副露（seen，不含自己手牌，
                            # 与 mj.tiles.visible_counts 同口径，只是这里手算避免重建
                            # discards/melds 结构体）。
                            visible = tuple(min(4, own[i] + seen[i]) for i in range(NSUITS))
                            out.append((counts13, groups, visible, wall))
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
        except (ValueError, KeyError, IndexError, TypeError):
            continue
    return out


# ---------------------------------------------------------------------------
# 阶段 2：分层抽样，保证每个 (路线, d) 组合都有样本。
# ---------------------------------------------------------------------------

def stratified_sample(all_states, target, seed):
    """all_states：[(counts13, meld, visible, wall)]。按每个状态里各可行路线的
    (route, d) 组合登记"需求"，配额用完前优先保留还没凑够的组合。"""
    rng = random.Random(seed)
    order = list(range(len(all_states)))
    rng.shuffle(order)
    per_bucket_target = max(10, target // (len(ROUTES) * 5))
    bucket_n = Counter()
    picked = []
    for i in order:
        counts13, meld, visible, wall = all_states[i]
        routes = feasible_routes(counts13, meld)
        needs = []
        for r in routes:
            d = DIST_FN[r](counts13, meld)
            if d is not None and 0 <= d <= 4:
                needs.append((r, d))
        if not needs:
            continue
        if any(bucket_n[key] < per_bucket_target for key in needs) or len(picked) < target:
            picked.append(all_states[i])
            for key in needs:
                bucket_n[key] += 1
        if len(picked) >= target and all(bucket_n[key] >= per_bucket_target for key in bucket_n):
            break
        if len(picked) >= target * 3:   # 保险：状态池很大但某些组合天然稀少时不要无限扫
            break
    return picked[:max(target, len(picked))]


# ---------------------------------------------------------------------------
# 阶段 3：对每个状态 x 可行路线做 K 次"坚持该路线"模拟。
# ---------------------------------------------------------------------------

TIE_EVAL_MAX = 4


def _connectivity(hand, i):
    """越小越该弃：财神 99；字牌 = 同种张数 x 2；数牌 = 自身张数 x 2 + 同花色 ±1 张数 x 2 + ±2 张数。"""
    if i == JOKER_IDX:
        return 99
    if i >= 27:
        return hand[i] * 2
    base = (i // 9) * 9
    score = hand[i] * 2
    for off, w in ((-1, 2), (1, 2), (-2, 1), (2, 1)):
        j = i + off
        if base <= j < base + 9:
            score += hand[j] * w
    return score


def _greedy_discard(hand, meld_groups, route, remaining_counts):
    """摸牌后 14 张，按"坚持 route"贪心弃 1 张：弃后距离最小；并列时弃后
    前进牌在剩余牌堆里的实际张数最大（用本局这次模拟里真实还没摸到的牌堆
    计数，比线上"假设都有 4 张"的静态活牌数更准，因为这里我们确切知道
    这次模拟已经摸走了哪些牌）。"""
    dist_fn = DIST_FN[route]
    adv_fn = ADVANCE_FN[route]
    present = [i for i in range(NSUITS) if hand[i] > 0]
    best_idx, best_dist, best_adv = None, None, -1.0
    tied = []
    for i in present:
        hand[i] -= 1
        d = dist_fn(tuple(hand), meld_groups)
        hand[i] += 1
        if d is None:
            continue
        if best_dist is None or d < best_dist:
            best_dist, tied = d, [i]
        elif d == best_dist:
            tied.append(i)
    if not tied:
        return present[0] if present else None
    if len(tied) == 1:
        return tied[0]
    # 提速：并列时先按"孤立度"便宜排序（与同花色 ±2 内的牌越少越孤立；财神永不优先弃），
    # 只对最孤立的 TIE_EVAL_MAX 张算完整前进牌（每次要 34 次向听计算）。距离 >=2 时
    # 前进牌多少对最终完成率影响很小，直接取最孤立的那张。
    tied.sort(key=lambda i: _connectivity(hand, i))
    if best_dist >= 2 or (route == "A" and best_dist == 1):
        return tied[0]
    if route == "A":
        # 标准路线听牌时：直接数"打完之后摸哪些牌能胡"（逐张 win_standard，约 2ms），
        # 不用 ukeire（约 14ms，是模拟耗时的 80%）。
        for i in tied[:TIE_EVAL_MAX]:
            hand[i] -= 1
            waits = 0
            for t in range(NSUITS):
                if remaining_counts[t] <= 0 or hand[t] >= 4:
                    continue
                hand[t] += 1
                if win_standard(tuple(hand), meld_groups):
                    waits += remaining_counts[t]
                hand[t] -= 1
            hand[i] += 1
            if waits > best_adv:
                best_adv, best_idx = waits, i
        return best_idx if best_idx is not None else tied[0]
    for i in tied[:TIE_EVAL_MAX]:
        hand[i] -= 1
        cand = tuple(hand)
        hand[i] += 1
        adv = sum(remaining_counts[t] for t in adv_fn(cand, meld_groups))
        if adv > best_adv:
            best_adv, best_idx = adv, i
    return best_idx if best_idx is not None else tied[0]


def simulate_state_route(counts13, meld_groups, visible, route, rollouts, seed):
    """返回长度 MAX_STEPS 的完成计数数组 comp[k-1] = 恰好第 k 步完成的次数
    （main() 里累加成 F(k) = 前 k 步完成次数之和 / rollouts）。"""
    pool_counts = [max(0, 4 - visible[i]) for i in range(NSUITS)]
    deck_template = []
    for i in range(NSUITS):
        deck_template.extend([i] * pool_counts[i])
    win_fn = WIN_FN[route]
    comp = [0] * MAX_STEPS
    rng = random.Random(seed)
    for trial in range(rollouts):
        deck = list(deck_template)
        rng.shuffle(deck)
        remaining_counts = list(pool_counts)
        hand = list(counts13)
        for step in range(MAX_STEPS):
            if not deck:
                break
            # B/D 是"爆头"路线：只有摸牌前的 13 张已经是爆头形（距离 0）时胡牌
            # 才算 B/D 完成（番按爆头算）。距离 >=1 时摸成的普通胡/普通七对是
            # A/C 的番，不能记成 B/D 完成，否则 B/D 的完成率被高估。
            pre_ok = route not in ("B", "D") or DIST_FN[route](tuple(hand), meld_groups) == 0
            draw_idx = deck.pop()
            remaining_counts[draw_idx] -= 1
            hand[draw_idx] += 1
            if pre_ok and win_fn(tuple(hand), meld_groups):
                comp[step] += 1
                break
            discard_idx = _greedy_discard(hand, meld_groups, route, remaining_counts)
            if discard_idx is None:
                break
            hand[discard_idx] -= 1
    return comp


def _simulate_one(args):
    counts13, meld_groups, visible, route, rollouts, seed = args
    comp = simulate_state_route(counts13, meld_groups, visible, route, rollouts, seed)
    return (counts13, meld_groups, visible, route), comp


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def _scan_path(path):
    return path, scan_states(path)


def _save_cache(results, code_ver, rollouts):
    os.makedirs(os.path.dirname(CACHE_PATH), exist_ok=True)
    tmp = CACHE_PATH + ".tmp"
    with open(tmp, "wb") as out:
        pickle.dump({"ver": code_ver, "rollouts": rollouts, "results": results}, out,
                    protocol=pickle.HIGHEST_PROTOCOL)
    os.replace(tmp, CACHE_PATH)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--states", type=int, default=4000)
    ap.add_argument("--rollouts", type=int, default=24)
    ap.add_argument("--jobs", type=int, default=8)
    ap.add_argument("--seed", type=int, default=20260928)
    ap.add_argument("--limit-files", type=int, default=None, help="只扫前 N 个文件（先 profiling）")
    ap.add_argument("--scan-files", type=int, default=400,
                    help="从拟合文件里随机取 N 个扫描候选状态（全量 2500+ 个要扫 10 分钟、产出 60 万状态，"
                         "而只需要几千个；400 个约 10 万状态，足够分层抽样）。0 = 全部")
    ap.add_argument("--no-cache", action="store_true")
    args = ap.parse_args()

    all_files = discover_files(limit=args.limit_files)
    fit_files = [f for f in all_files if not _is_holdout(f)]
    print("拟合文件 %d 个（留出 %d 个给 C1b）" % (len(fit_files), len(all_files) - len(fit_files)))
    if args.scan_files and len(fit_files) > args.scan_files:
        fit_files = sorted(random.Random(args.seed).sample(fit_files, args.scan_files))
        print("本次扫描其中 %d 个（--scan-files）" % len(fit_files))

    t0 = time.time()
    all_states = []
    with Pool(args.jobs) as pool:
        for done, (path, states) in enumerate(pool.imap_unordered(_scan_path, fit_files, chunksize=8), 1):
            all_states.extend(states)
            if done % 200 == 0 or done == len(fit_files):
                print("  扫描 %d / %d 个文件，累计候选状态 %d" % (done, len(fit_files), len(all_states)),
                     flush=True)
    print("候选状态池 %d 个，用时 %.1fs" % (len(all_states), time.time() - t0))

    sampled = stratified_sample(all_states, args.states, args.seed)
    print("分层抽样得到 %d 个状态，开始模拟（--rollouts %d --jobs %d）" % (
        len(sampled), args.rollouts, args.jobs))

    with open(os.path.abspath(__file__), "rb") as own:
        code_ver = hashlib.md5(own.read()).hexdigest()
    cache = {}
    if not args.no_cache:
        try:
            with open(CACHE_PATH, "rb") as source:
                saved = pickle.load(source)
            if saved.get("ver") == code_ver and saved.get("rollouts") == args.rollouts:
                cache = saved.get("results") or {}
        except (OSError, ValueError, EOFError, pickle.UnpicklingError, AttributeError):
            cache = {}

    tasks = []
    rng = random.Random(args.seed)
    for counts13, meld_groups, visible, wall in sampled:
        for route in feasible_routes(counts13, meld_groups):
            d = DIST_FN[route](counts13, meld_groups)
            if d is None or not (0 <= d <= 4):
                continue
            cache_key = (counts13, meld_groups, visible, route)
            if cache_key in cache:
                continue
            tasks.append((counts13, meld_groups, visible, route, args.rollouts, rng.randint(0, 2 ** 31)))
    print("需要新模拟 %d 个 (状态,路线) 组合（缓存命中 %d 个）" % (len(tasks), len(cache)))

    results = dict(cache)
    t1 = time.time()
    if tasks:
        with Pool(args.jobs) as pool:
            for done, (key, comp) in enumerate(pool.imap_unordered(_simulate_one, tasks, chunksize=4), 1):
                results[key] = comp
                if not args.no_cache and done % 1000 == 0:
                    _save_cache(results, code_ver, args.rollouts)   # 中途崩溃也不丢已完成的模拟
                if done % 100 == 0 or done == len(tasks):
                    spent = time.time() - t1
                    eta = spent / done * (len(tasks) - done) / 60.0
                    print("  模拟 %d / %d，已用 %.1f 分钟，预计还需 %.1f 分钟" % (
                        done, len(tasks), spent / 60.0, eta), flush=True)
    if not args.no_cache:
        _save_cache(results, code_ver, args.rollouts)

    # ---- 聚合成表：键 = (路线, d, 活牌分档, 财神数, 副露组数) ----
    # live 用 mj.route_ev._routes() 的原始 p_rate/w_rate（与产线、在线查表同一口径）。
    raw_cells = defaultdict(lambda: [0] * MAX_STEPS)   # key -> 累计完成计数（未除样本数）
    cell_n = Counter()
    theory_cache = {}   # (route, d) -> 抽样理论对照用的一条代表性 F(4)/F(8)/F(12)（首次遇到即用）
    for (counts13, meld_groups, visible, route), comp in results.items():
        # rev_override_b_enabled=0：B 路线 d==2 时 _routes() 用粗糙的 std_rate
        # 兜底（P3 关闭时的产线口径）。在线查表用的 weights 是调用方当时的真实
        # 权重，若那时 P3 也打开，B/d==2 的 live 值会不同、可能查不到这张表里
        # 的格子——退回理论估算，是可接受的降级，不是正确性问题（B/d==2 只是
        # 四条路线里的一小格）。这里固定用 P3 关闭的口径建表，覆盖最常见的
        # "先只开 rev_route_cal_enabled" 场景。
        route_defs = _routes(counts13, meld_groups, visible, (route,), {})
        if not route_defs:
            continue
        letter, d, p_rate, w_rate, _fan = route_defs[0]
        if d is None or not (0 <= d <= 4):
            continue
        live = p_rate if d >= 1 else (w_rate if w_rate is not None else 999)
        jokers = min(counts13[JOKER_IDX], 2)
        melds_b = min(meld_groups, 2)
        key = route_cal_key(route, d, live, jokers, melds_b)
        acc = raw_cells[key]
        for k in range(MAX_STEPS):
            acc[k] += comp[k]
        cell_n[key] += args.rollouts
        theory_cache.setdefault((route, d), (p_rate, w_rate))

    # 按 副露 -> 财神 -> 活牌分档 的顺序逐级丢维度聚合；每一级凡是样本量够
    # （>=MIN_CELL_N）就单独写一条表项（键里被丢的维度用 "*"），与
    # mj.route_ev.route_cal_backoff_keys 的查找顺序完全对应——在线查表按
    # 那个顺序依次试键，第一个命中最细的就用，不在线重新聚合。full 级
    # （最细）即使样本不够也照常写（n 记真实样本数），让在线回退链的最后一步
    # "R:d:*:*:*"（L3）永远存在、兜底一定命中。
    def _parse(key):
        route, d, live_b, jokers, melds_b = key.split(":")
        return route, int(d), int(live_b), int(jokers), int(melds_b)

    l1 = defaultdict(lambda: [0] * MAX_STEPS)   # 丢 melds
    l1_n = Counter()
    l2 = defaultdict(lambda: [0] * MAX_STEPS)   # 丢 melds + jokers
    l2_n = Counter()
    l3 = defaultdict(lambda: [0] * MAX_STEPS)   # 丢 melds + jokers + live_bucket
    l3_n = Counter()
    for key, acc in raw_cells.items():
        route, d, live_b, jokers, melds_b = _parse(key)
        n = cell_n[key]
        for k in range(MAX_STEPS):
            l1[(route, d, live_b, jokers)][k] += acc[k]
            l2[(route, d, live_b)][k] += acc[k]
            l3[(route, d)][k] += acc[k]
        l1_n[(route, d, live_b, jokers)] += n
        l2_n[(route, d, live_b)] += n
        l3_n[(route, d)] += n

    def _to_curve(acc, n):
        cum, run = [], 0.0
        for k in range(MAX_STEPS):
            run += acc[k] / n if n else 0.0
            cum.append(min(1.0, run))
        return cum

    cells_out = {}
    for key, acc in raw_cells.items():
        n = cell_n[key]
        cells_out[key] = {"F": _to_curve(acc, n), "n": n, "backoff": "full"}
    for (route, d, live_b, jokers), acc in l1.items():
        n = l1_n[(route, d, live_b, jokers)]
        if n >= MIN_CELL_N:
            key = "%s:%d:%d:%d:*" % (route, d, live_b, jokers)
            cells_out[key] = {"F": _to_curve(acc, n), "n": n, "backoff": "drop_melds"}
    for (route, d, live_b), acc in l2.items():
        n = l2_n[(route, d, live_b)]
        if n >= MIN_CELL_N:
            key = "%s:%d:%d:*:*" % (route, d, live_b)
            cells_out[key] = {"F": _to_curve(acc, n), "n": n, "backoff": "drop_jokers"}
    for (route, d), acc in l3.items():
        n = l3_n[(route, d)]
        key = "%s:%d:*:*:*" % (route, d)   # 最粗一级：样本再少也写，是回退链的兜底
        cells_out[key] = {"F": _to_curve(acc, n), "n": n, "backoff": "drop_live_bucket"}

    table = {"cells": cells_out,
            "meta": {"states": len(sampled), "tasks": len(tasks), "cache_hits": len(cache),
                    "rollouts": args.rollouts, "min_cell_n": MIN_CELL_N,
                    "backoff_order": ["melds", "jokers", "live_bucket"]}}
    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(table, f, ensure_ascii=False, indent=2)
    print("\n已写 %s（%d 个格子）" % (OUT_PATH, len(cells_out)))

    # ---- 对照：理论估算（不含 survive）的 F(4)/F(8)/F(12) vs 模拟值 ----
    print("\n=== 理论估算 vs 模拟：每个 (路线, d) 的 F(4)/F(8)/F(12) ===")
    print("%-4s %-3s %10s %10s %10s | %10s %10s %10s" % (
        "路线", "d", "理论F(4)", "理论F(8)", "理论F(12)", "模拟F(4)", "模拟F(8)", "模拟F(12)"))
    seen_rd = set()
    for key in sorted(raw_cells):   # 只看全细键（数字键，不含 "*" 回退项），保证 melds_b 是整数
        route, d, live_b, jokers, melds_b = _parse(key)
        if (route, d) in seen_rd:
            continue
        seen_rd.add((route, d))
        cell = cells_out[key]
        p_rate, w_rate = theory_cache.get((route, d), (None, None))
        # 理论：p 固定不变的几何分布近似，不含 survive（survive=1 全程）。
        universe_guess = 136 - 13 - 3 * melds_b   # 粗估，仅用于这张对照表
        p = (p_rate / universe_guess) if (d and d >= 1 and p_rate) else 0.0
        q = 1.0 if w_rate is None else min(1.0, (w_rate or 0.0) / max(universe_guess, 1))
        dist = {d: 1.0}
        theory_cum = []
        run = 0.0
        for _step in range(12):
            nxt = {}
            for remaining, prob in dist.items():
                if remaining == 0:
                    run += prob * q
                    stay = prob * (1.0 - q)
                    if stay:
                        nxt[0] = nxt.get(0, 0.0) + stay
                else:
                    adv = prob * p
                    stay = prob * (1.0 - p)
                    if adv:
                        nxt[remaining - 1] = nxt.get(remaining - 1, 0.0) + adv
                    if stay:
                        nxt[remaining] = nxt.get(remaining, 0.0) + stay
            dist = nxt
            theory_cum.append(min(1.0, run))
        t4, t8, t12 = theory_cum[3], theory_cum[7], theory_cum[11]
        f = cell["F"]
        print("%-4s %-3d %9.1f%% %9.1f%% %9.1f%% | %9.1f%% %9.1f%% %9.1f%%" % (
            route, d, 100 * t4, 100 * t8, 100 * t12, 100 * f[3], 100 * f[7], 100 * f[11]))


if __name__ == "__main__":
    main()
