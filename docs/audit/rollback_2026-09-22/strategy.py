"""初版进攻策略：向听优先，进张次之，财神与链状态保留优先级高。"""
from .ev import pair_route_bonus
from .fit import load_weights
from .joker_ev import joker_plan_value
from .rules import seven_pairs
from .shanten import combined_route, route_shanten, shanten, ukeire
from .tiles import JOKER, TILE_INDEX, to_counts, visible_counts


def _wait_live_score(waits, visible):
    """进张活度：剩 ≥2 张记 1，剩 1 张记 0.5，亮光死听记 0。visible=None 退化为种数。"""
    if visible is None:
        return float(len(waits))
    live = 0.0
    for idx in waits:
        rem = 4 - visible[idx]
        live += 1.0 if rem >= 2 else (0.5 if rem == 1 else 0.0)
    return live


def _effective_weights(rules):
    custom = (rules or {}).get("_weights")
    merged = {**load_weights(), **custom} if custom else load_weights()
    return merged


def _effective_remaining(tiles, discard):
    remaining = list(tiles)
    remaining.remove(discard)
    return remaining


def _discard_score(tiles, discard, meld_groups=0, chain_count=0, piao=0, rules=None, weights=None,
                   visible=None):
    remaining = _effective_remaining(tiles, discard)
    counts = tuple(to_counts(remaining))
    weights = weights or _effective_weights(rules)
    std_current = shanten(counts, meld_groups)
    current, route_waits = combined_route(counts, meld_groups)
    draws = route_waits if current <= 2 else ()
    joker_count = counts[TILE_INDEX[JOKER]]
    score = -current * weights["b_shanten"] + _wait_live_score(draws, visible) * weights["b_ukeire"]
    deep = weights.get("b_ukeire3", 0)
    if deep and current == 3:
        score += _wait_live_score(ukeire(counts, meld_groups), visible) * deep
    if chain_count == 0 and piao == 0:
        plan = joker_plan_value(counts, meld_groups, std_current,
                                weights["b_shanten"], rules, weights)
        if plan is not None and plan > score:
            score = plan
    if meld_groups == 0:
        pair_count = sum(1 for tile in set(remaining) if remaining.count(tile) >= 2)
        score += pair_count * weights["b_pair"]
        seven_pairs_progress = sum(min(2, remaining.count(tile)) for tile in set(remaining))
        if seven_pairs_progress >= 10:
            score += weights["b_progress"]
            # 豪华门票: ≥5 对的门清手里, 三张同种是豪华七对(×4)的彩票,
            # 拆掉第三张 = 丢票; 仅在 ≥5 对时保值 (用户口径: <5 对不走七对)
            if pair_count >= 5 and remaining.count(discard) == 2:
                score -= weights.get("baohua_ticket", 800)
        score += pair_route_bonus(counts, meld_groups, weights)
    idx = TILE_INDEX[discard]
    if idx >= 27:
        before = counts[idx]
        if before == 0:
            score += weights["b_honor"]
        elif before == 1:
            score -= weights["b_honor"]
        else:
            score -= weights["b_honor"] * 3
    if discard == JOKER:
        score -= 800
    if chain_count and discard != JOKER:
        score -= chain_count * 250
    if chain_count and discard == JOKER:
        score += 600 * chain_count
    if piao and discard == JOKER:
        score += 800 * piao
    if (rules or {}).get("YouCaiBiKao") and joker_count:
        score -= 300 if discard == JOKER else 0
    score -= (rules or {}).get("_defense", {}).get(discard, 0)
    return score


def choose_discard(tiles, meld_groups=0, chain_count=0, piao=0, rules=None, visible=None):
    if not tiles:
        return None
    weights = _effective_weights(rules)
    unique = sorted(set(tiles))
    currents = {}
    for tile in unique:
        remaining = list(tiles)
        remaining.remove(tile)
        counts = to_counts(remaining)
        currents[tile] = route_shanten(counts, meld_groups)
    best = min(currents.values())
    tier = [tile for tile in unique if currents[tile] == best]
    return max(tier, key=lambda tile: _discard_score(tiles, tile, meld_groups, chain_count, piao, rules, weights,
                                                     visible))
