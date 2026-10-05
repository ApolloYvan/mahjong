"""无财神弃牌的特征化评分：线上评分与 tools/discard_fit.py 的拟合共用同一份特征定义。

开关 rule_fitted_discard_enabled（默认关）打开后，手上没有财神、不在财飘链上的弃牌，
在同向听那一层里改用 Σ fd_<特征> × 特征值 打分（再扣 _defense）。

defaults(weights) 是现行 _discard_score 在无财神手上的等价权重（以打中张为 0 点，
随 b_honor / discard_terminal / keep_middle 等现行权重联动）。所以开关打开但不写
fd_* 权重时，闲家选牌与现行一致（tests/test_discard_features.py 锁定）。
拟合出新权重后，只需把 fd_* 写进 models/weights.json。
"""
from .ev import pair_route_bonus
from .shanten import combined_route, pair_route_allowed, ukeire
from .tiles import JOKER_IDX, TILE_INDEX, to_counts

FD_FEATURES = ("uke", "uke3", "pair_far", "pair_near", "hon_iso", "hon_pair", "hon_trip",
               "term", "e28", "isolated", "seen", "pair_route")

def defaults(weights):
    """现行 _discard_score 在无财神手上的等价 fd_* 权重（以打中张为 0 点），随现行权重联动。"""
    w = weights
    mid = -w.get("keep_middle", 250)
    honor = w["b_honor"]
    return {
        "uke": w["b_ukeire"],                   # 弃后 ≤2 向听：听口/进张活度
        "uke3": w.get("b_ukeire3", 0),          # 弃后 3 向听：进张活度
        "pair_far": (w.get("std_pair_ratio", 1.5) * w["b_ukeire"]
                     if w.get("rule_std_pair_enabled") else 0),   # 弃后 ≥2 向听：非财神对子数
        "pair_near": 0,                         # 弃后 ≤1 向听：非财神对子数
        "hon_iso": honor - mid,                 # 打孤字
        "hon_pair": -honor - mid,               # 拆字牌对子
        "hon_trip": -3 * honor - mid,           # 拆字牌刻子
        "term": w.get("discard_terminal", 250) - mid,   # 打幺九数牌
        "e28": w.get("discard_28", 100) - mid,          # 打二八数牌
        "isolated": 0,      # 弃后手里孤张数（单张、同花色 ±2 内无邻牌；字牌单张）
        "seen": 0,          # 打出的这张牌场上（别家牌河+副露）已见几张
        "pair_route": 100,  # 七对路线整块分（现行值 /100）
    }


def wait_live_score(waits, visible):
    """进张活度：剩 ≥2 张记 1，剩 1 张记 0.5，亮光死听记 0。visible=None 退化为种数。"""
    if visible is None:
        return float(len(waits))
    live = 0.0
    for idx in waits:
        rem = 4 - visible[idx]
        live += 1.0 if rem >= 2 else (0.5 if rem == 1 else 0.0)
    return live


def pair_route_block(remaining, counts, discard, meld_groups, weights):
    """_discard_score 里七对路线那一整块（调用方已确认 pair_route_allowed）。"""
    pair_count = sum(1 for tile in set(remaining) if remaining.count(tile) >= 2)
    value = pair_count * weights["b_pair"]
    progress = sum(min(2, remaining.count(tile)) for tile in set(remaining))
    if progress >= 10:
        value += weights["b_progress"]
        # 豪华门票: ≥5 对的门清手里, 三张同种是豪华七对(×4)的彩票,
        # 拆掉第三张 = 丢票; 仅在 ≥5 对时保值 (用户口径: <5 对不走七对)
        if pair_count >= 5 and remaining.count(discard) == 2:
            value -= weights.get("baohua_ticket", 800)
    value += pair_route_bonus(counts, meld_groups, weights)
    return value


def _isolated(counts):
    n = 0
    for i, c in enumerate(counts):
        if c != 1 or i == JOKER_IDX:
            continue
        if i >= 27:
            n += 1
            continue
        base, r = i - i % 9, i % 9
        if not any(counts[base + r + d] for d in (-2, -1, 1, 2) if 0 <= r + d < 9):
            n += 1
    return n


def features(tiles, discard, meld_groups, visible, weights):
    remaining = list(tiles)
    remaining.remove(discard)
    counts = tuple(to_counts(remaining))
    current, waits = combined_route(counts, meld_groups)
    f = dict.fromkeys(FD_FEATURES, 0.0)
    if current <= 2:
        f["uke"] = wait_live_score(waits, visible)
    elif current == 3:
        f["uke3"] = wait_live_score(ukeire(counts, meld_groups), visible)
    pairs = sum(1 for i, n in enumerate(counts) if n >= 2 and i != JOKER_IDX)
    f["pair_far" if current >= 2 else "pair_near"] = pairs
    idx = TILE_INDEX[discard]
    if idx >= 27:
        left = counts[idx]
        f["hon_iso" if left == 0 else ("hon_pair" if left == 1 else "hon_trip")] = 1
    else:
        rank = idx % 9 + 1
        if rank in (1, 9):
            f["term"] = 1
        elif rank in (2, 8):
            f["e28"] = 1
    f["isolated"] = _isolated(counts)
    if visible is not None:
        f["seen"] = visible[idx] - tiles.count(discard)
    if meld_groups == 0 and pair_route_allowed(counts, meld_groups):
        f["pair_route"] = pair_route_block(remaining, counts, discard, meld_groups, weights) / 100.0
    return f


def score(tiles, discard, meld_groups, visible, weights, rules=None):
    f = features(tiles, discard, meld_groups, visible, weights)
    base = defaults(weights)
    total = sum(weights.get("fd_" + k, base[k]) * v for k, v in f.items())
    return total - (rules or {}).get("_defense", {}).get(discard, 0)


def applies(tiles, chain_count, piao, weights):
    return bool(weights.get("rule_fitted_discard_enabled", 0)) and "白" not in tiles \
        and not chain_count and not piao


# ---------------------------------------------------------------- 持财神手（开关 rule_fitted_discard_joker_enabled）
#
# 与无财神手分开拟合（权重前缀 fj_），特征 = 上面 12 项 + 下面 5 项爆头相关项。
# 现行持财神评分里有 joker_plan_value 取 max 的非线性，没有"缺省等价权重"，
# 所以**只有 weights 里存在 fj_uke（即写入了拟合结果）时才生效**，单开开关不改变任何行为。
J_FEATURES = FD_FEATURES + ("bt_after", "bt_dist", "plan_gain", "mq_bt", "disc_joker", "bt_uke")
EXTRACT_ALL = False     # tools/discard_fit.py 提取时置 True


def baotou_waits(counts13, meld_groups):
    """离爆头听差一步（to_baotou_distance==1）时，摸到后能打出爆头听的牌种；否则空。
    爆头版的「进张」：同样差一步，能接的牌越多，下一手成爆头的机会越大。"""
    from .joker_ev import to_baotou_distance
    if to_baotou_distance(counts13, meld_groups) != 1:
        return []
    out = []
    for t in range(len(counts13)):
        if t == JOKER_IDX or counts13[t] >= 4:
            continue
        c = list(counts13)
        c[t] += 1
        for x in range(len(c)):
            if x == JOKER_IDX or not c[x] or x == t:
                continue
            c[x] -= 1
            hit = to_baotou_distance(tuple(c), meld_groups) == 0
            c[x] += 1
            if hit:
                out.append(t)
                break
    return out


def features_joker(tiles, discard, meld_groups, visible, weights, rules=None, want_bt_uke=False):
    from .joker_ev import joker_plan_value, menqing_baotou_bonus, to_baotou_distance
    from .rules import baotou
    from .shanten import shanten
    f = features(tiles, discard, meld_groups, visible, weights)
    remaining = list(tiles)
    remaining.remove(discard)
    counts = tuple(to_counts(remaining))
    current, waits = combined_route(counts, meld_groups)
    f["bt_after"] = float(current == 0 and baotou(counts, meld_groups))
    dist = to_baotou_distance(counts, meld_groups)
    f["bt_dist"] = 9.0 if dist is None else float(dist)
    plan = joker_plan_value(counts, meld_groups, shanten(counts, meld_groups), weights["b_shanten"], rules, weights)
    base = -current * weights["b_shanten"] + f["uke"] * weights["b_ukeire"]
    f["plan_gain"] = max(0.0, plan - base) / 100.0 if plan is not None else 0.0
    f["mq_bt"] = menqing_baotou_bonus(counts, meld_groups, current, weights) / 100.0
    f["disc_joker"] = 0.0
    if discard == "白":
        f["hon_iso"] = f["hon_pair"] = f["hon_trip"] = 0.0
        f["disc_joker"] = 1.0
    f["bt_uke"] = 0.0
    if want_bt_uke or EXTRACT_ALL or weights.get("fj_bt_uke"):     # 线上：没拟合出这项权重就不算，省时
        f["bt_uke"] = wait_live_score(baotou_waits(counts, meld_groups), visible)
    return f


def score_joker(tiles, discard, meld_groups, visible, weights, rules=None):
    f = features_joker(tiles, discard, meld_groups, visible, weights, rules)
    total = sum(weights.get("fj_" + k, 0) * v for k, v in f.items())
    return total - (rules or {}).get("_defense", {}).get(discard, 0)


def applies_joker(tiles, chain_count, piao, weights):
    return bool(weights.get("rule_fitted_discard_joker_enabled", 0)) and "fj_uke" in weights \
        and "白" in tiles and not chain_count and not piao
