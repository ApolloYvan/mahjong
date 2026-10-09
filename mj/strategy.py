"""初版进攻策略：向听优先，进张次之，财神与链状态保留优先级高。"""
from . import discard_features, nn_discard, route_ev
from .discard_features import pair_route_block
from .discard_features import wait_live_score as _wait_live_score
from .ev import discard_position_bonus, std_pair_bonus
from .fit import load_weights
from .joker_ev import joker_plan_value, menqing_baotou_bonus
from .rules import seven_pairs
from .shanten import combined_route, pair_route_allowed, route_shanten, shanten, ukeire
from .tiles import JOKER, TILE_INDEX, to_counts, visible_counts


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
    score += menqing_baotou_bonus(counts, meld_groups, current, weights)
    score += std_pair_bonus(counts, meld_groups, current, weights["b_ukeire"], weights)
    deep = weights.get("b_ukeire3", 0)
    if deep and current == 3:
        score += _wait_live_score(ukeire(counts, meld_groups), visible) * deep
    if chain_count == 0 and piao == 0:
        plan = joker_plan_value(counts, meld_groups, std_current,
                                weights["b_shanten"], rules, weights)
        if plan is not None and plan > score:
            score = plan
    if meld_groups == 0 and pair_route_allowed(counts, meld_groups):
        score += pair_route_block(remaining, counts, discard, meld_groups, weights)
    idx = TILE_INDEX[discard]
    if idx >= 27:
        before = counts[idx]
        if before == 0:
            score += weights["b_honor"]
        elif before == 1:
            score -= weights["b_honor"]
        else:
            score -= weights["b_honor"] * 3
    else:
        # 见 mj.ev.discard_position_bonus 的证据与收口说明。这条路径是闲家常态
        # （bot.choose_discard 的 use_route=False 分支），占约 75% 的弃牌，
        # 也正是 tools/dealer_edge.py 测出"差距 74% 在闲位"的那条路径。
        score += discard_position_bonus(discard, weights)
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

    def _original():
        unique = sorted(set(tiles))
        keep = (rules or {}).get("_keep") or ()      # bu_defer：留着的碰牌第 4 张不进候选
        unique = [t for t in unique if t not in keep] or unique
        currents = {}
        for tile in unique:
            remaining = list(tiles)
            remaining.remove(tile)
            counts = to_counts(remaining)
            currents[tile] = route_shanten(counts, meld_groups)
        best = min(currents.values())
        tier = [tile for tile in unique if currents[tile] == best]
        if nn_discard.applies(chain_count, piao, weights):
            pick = nn_discard.choose(tier, tiles, meld_groups, visible, weights, rules)
            if pick is not None:
                return pick
        if discard_features.applies(tiles, chain_count, piao, weights):
            return max(tier, key=lambda tile: discard_features.score(tiles, tile, meld_groups, visible, weights,
                                                                     rules))
        if discard_features.applies_joker(tiles, chain_count, piao, weights):
            return max(tier, key=lambda tile: discard_features.score_joker(tiles, tile, meld_groups, visible,
                                                                           weights, rules))
        return max(tier, key=lambda tile: _discard_score(tiles, tile, meld_groups, chain_count, piao, rules,
                                                         weights, visible))

    # _original() 本身不便宜（对每个候选都要算一次 combined_route/打分）；两个
    # 叠加逻辑与最终回落最多各调用一次，用惰性缓存合并成最多一次真实计算，
    # 避免 P1~P5 全开时对多财神手牌重复算 2~3 遍（2026-09-28 性能审计）。
    _cache = {}

    def _cached_original():
        if "v" not in _cache:
            _cache["v"] = _original()
        return _cache["v"]

    # 2026-09-28：route EV（mj/route_ev.py，开关 rule_route_ev_enabled，
    # 默认关闭）在算 tier 之前叠加——七对候选可能根本不在标准同向听层里，
    # 排在 tier 计算之后就看不到。范围外或开关关闭时 applies_discard 立即
    # 返回 False，不产生任何额外开销；任何异常都在 route_ev 内部吞掉。
    override = route_ev.maybe_override_discard(tiles, meld_groups, chain_count, piao, rules, weights, visible,
                                               _cached_original)
    if override is not None and override not in ((rules or {}).get("_keep") or ()):
        return override
    # 2026-09-28：P2 速度前瞻（rule_route_ev_enabled 的叠加之后、原逻辑之前，
    # 见 mj/route_ev.py::maybe_override_speed）。全部手牌，不限门清/财神。
    speed_override = route_ev.maybe_override_speed(tiles, meld_groups, chain_count, piao, rules, weights,
                                                    visible, _cached_original)
    if speed_override is not None and speed_override not in ((rules or {}).get("_keep") or ()):
        return speed_override
    return _cached_original()
