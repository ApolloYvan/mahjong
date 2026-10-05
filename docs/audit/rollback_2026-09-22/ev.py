"""胡牌路线 EV：比较普通胡、七对子、爆头与动作链潜力。"""
from .fit import load_weights
from .joker_ev import joker_plan_value
from .rules import seven_pairs
from .shanten import combined_route, pair_shanten, pair_ukeire, route_shanten, shanten
from .tiles import TILE_INDEX, to_counts


def hand_profile(tiles, meld_groups=0, chain_count=0, piao=0, rules=None):
    counts = to_counts(tiles)
    current, waits = combined_route(counts, meld_groups)
    return {
        "shanten": current,
        "ukeire": len(waits),
        "pair_shanten": pair_shanten(counts) if meld_groups == 0 else None,
        "pair_ukeire": len(pair_ukeire(counts)) if meld_groups == 0 else None,
        "joker": counts[TILE_INDEX["白"]],
        "pairs": seven_pairs(counts) if meld_groups == 0 else None,
        "meld_groups": meld_groups,
        "chain_count": chain_count,
        "piao": piao,
        "rules": rules or {},
    }


def pair_route_bonus(counts, meld_groups, weights=None):
    """门前七对路线：越接近七对听牌，保留价值越高（大胡×2起步）。"""
    if meld_groups:
        return 0
    weights = weights or {}
    return max(0, 3 - pair_shanten(counts)) * weights.get("pair_route", 600)


def route_value(tiles, discard, meld_groups=0, chain_count=0, piao=0, rules=None, weights=None,
                visible=None):
    """弃牌后的粗 EV：速度、进张、路线番值；仅供选择弃牌，不替代服务端判定。
    visible = 全桌可见张数（34 维），提供时进张按剩余张数加权。"""
    remaining = list(tiles)
    remaining.remove(discard)
    counts = to_counts(remaining)
    weights = weights or load_weights()
    std_current = shanten(counts, meld_groups)
    current, route_waits = combined_route(counts, meld_groups)
    value = -current * weights["shanten"]
    if current <= 2:
        live = 0.0
        for idx in route_waits:
            if visible is None:
                live += 1.0
            else:
                rem = 4 - visible[idx]
                live += 1.0 if rem >= 2 else (0.5 if rem == 1 else 0.0)
        value += live * weights["ukeire"]
    if chain_count == 0 and piao == 0:
        plan = joker_plan_value(counts, meld_groups, std_current,
                                weights["shanten"], rules, weights)
        if plan is not None and plan > value:
            value = plan
    idx = TILE_INDEX[discard]
    if idx >= 27:
        before = counts[idx]
        if before == 0:
            value += 300
        elif before == 1:
            value -= 300
        else:
            value -= 900
    value -= (rules or {}).get("_defense", {}).get(discard, 0)
    if meld_groups == 0:
        pairs = sum(1 for tile in set(remaining) if remaining.count(tile) >= 2)
        value += pairs * weights["pair"]
        if pairs >= 6:
            # 七对路线：对子 ≥6（含财神对）给强奖励，庄上博 ×2 起步的大番
            value += weights.get("seven_pairs_route", 400) * (pairs - 5)
        # 豪华门票: 对子 ≥5 的门清手里拆三张同种 = 丢豪华七对(×4)彩票
        if pairs >= 5 and remaining.count(discard) == 2:
            value -= weights.get("baohua_ticket", 800)
        value += pair_route_bonus(counts, meld_groups, weights)
    if chain_count:
        value += chain_count * weights["chain"]
        value += (weights["piao"] * piao if discard == "白" else -weights["chain"] * chain_count)
    if discard == "白":
        value += weights.get("discard_joker_soft", -800)
    if (rules or {}).get("YouCaiBiKao") and counts[TILE_INDEX["白"]]:
        value += weights["must_kaoxiang"] if discard != "白" else 0
    return value


def choose_route_discard(tiles, meld_groups=0, chain_count=0, piao=0, rules=None, visible=None):
    if not tiles:
        return None
    unique = sorted(set(tiles))
    weights = load_weights()
    currents = {}
    for tile in unique:
        remaining = list(tiles)
        remaining.remove(tile)
        counts = to_counts(remaining)
        currents[tile] = route_shanten(counts, meld_groups)
    best = min(currents.values())
    tier = [tile for tile in unique if currents[tile] == best]
    return max(tier, key=lambda tile: route_value(tiles, tile, meld_groups, chain_count, piao, rules, weights,
                                                  visible))
