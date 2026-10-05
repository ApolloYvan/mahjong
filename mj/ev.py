"""胡牌路线 EV：比较普通胡、七对子、爆头与动作链潜力。"""
from .fit import load_weights
from .joker_ev import joker_plan_value, menqing_baotou_bonus
from .rules import seven_pairs
from .shanten import combined_route, pair_route_allowed, pair_shanten, pair_ukeire, route_shanten, shanten
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
    """门前七对路线：越接近七对听牌，保留价值越高（大胡×2起步）。

    两个调用点（本文件 route_value 与 mj.strategy._discard_score）都要求七对
    路线已经解锁（pair_route_allowed）——在这里一次性收口，不在两个调用点各补
    一次判据，避免"两份平行评分体"改一处漏一处。"""
    if not pair_route_allowed(counts, meld_groups):
        return 0
    weights = weights or {}
    return max(0, 3 - pair_shanten(counts)) * weights.get("pair_route", 600)


def discard_position_bonus(discard, weights=None):
    """数牌的弃牌位置价值：高手的弃牌顺序是 字牌 → 幺九 → 二八 → 中张。

    2026-09-24 反推 8466 个高手弃牌点（tools/policy_diff.py，歪比巴卜 +2.36 /
    两脚离地 +2.12）：**我们对「幺九 / 二八 / 中张」完全没有偏好项**，数牌里
    1万 和 5万 在两份评分体里都一模一样，同向听时的取舍实际按 TILE_INDEX 顺序
    随机决定。结果：

                   分歧时他打了什么              我们的弃牌构成
      歪比巴卜      中张35% 幺九28% 字牌19%      中张52% 二八23% 幺九21%
      两脚离地      中张35% 幺九31% 字牌17%

    高手打幺九 28~31%、我们只打 21%；我们打中张 52%、高手 35%。而且分歧按向听
    单调下降（3向 42~56% → 听牌 21~23%），差距全在手牌成型之前的方向选择。

    **收口在这里的理由**：mj.ev.route_value 与 mj.strategy._discard_score 是两份
    平行评分体（生产按 use_route 二选一：庄家/财飘链走前者，闲家常态走后者），
    2026-09-24 我只改了 route_value，而 policy_diff 测的是 _discard_score，
    于是一致率一字未动——正是 pair_route_bonus 注释里警告的"改一处漏一处"。
    两边都调用本函数，不要各写一份。

    量级刻意取小（250/100/−250），相对 b_shanten=10000 只够当**同向听时的平局
    裁决**，不会为了打幺九牺牲向听。回滚：三个权重全设 0。
    """
    idx = TILE_INDEX[discard]
    if idx >= 27:
        return 0
    weights = weights or {}
    rank = idx % 9 + 1
    if rank in (1, 9):
        return weights.get("discard_terminal", 250)
    if rank in (2, 8):
        return weights.get("discard_28", 100)
    return -weights.get("keep_middle", 250)


def std_pair_bonus(counts, meld_groups, current, ukeire_weight, weights):
    """普通胡路线的对子价值（开关 rule_std_pair_enabled，默认关）。

    2026-09-25 tools/policy_diff.py --jokers 0（四位高手 38743 个无财神弃牌点）：
    两向听的分歧点上，四人一致地少要 0.7~1.1 种进张、多留 0.2 个对子；而我们的
    对子加分只在七对路线解锁时生效，普通胡路线里对子一文不值。
    只在无财神、弃后 ≥2 向听时生效；对子数不封顶（实测封顶 2 对时与高手一致率
    0 变化，不封顶 55.2%→58.2%，30 个高手对局文件的 2 向听以上弃牌点 n=648）。
    std_pair_ratio = 一个对子折合几种进张（1.5 与 3 一致率相同，取小的）。"""
    if not weights.get("rule_std_pair_enabled", 0) or current < 2 or counts[TILE_INDEX["白"]]:
        return 0
    pairs = sum(1 for i, n in enumerate(counts) if n >= 2 and i != TILE_INDEX["白"])
    return pairs * weights.get("std_pair_ratio", 1.5) * ukeire_weight


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
    value += menqing_baotou_bonus(counts, meld_groups, current, weights)
    if current <= 2:
        live = 0.0
        for idx in route_waits:
            if visible is None:
                live += 1.0
            else:
                rem = 4 - visible[idx]
                live += 1.0 if rem >= 2 else (0.5 if rem == 1 else 0.0)
        value += live * weights["ukeire"]
    value += std_pair_bonus(counts, meld_groups, current, weights["ukeire"], weights)
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
    value += discard_position_bonus(discard, weights)
    value -= (rules or {}).get("_defense", {}).get(discard, 0)
    if meld_groups == 0 and pair_route_allowed(counts, meld_groups):
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
        # 2026-09-23：原式是 weights["piao"] * piao——而 piao 这个字段服务端从不
        # 下发。400622 个实盘 god 块里只有 baotou/chain_count/catch_play/
        # god_discarder_seat 四个键，piao 在快照任何位置都没出现过、恒为 0。
        # 于是「鼓励续飘」的奖励项恒等于 0，弃财神只剩 discard_joker_soft=-800
        # 的净惩罚，链上永远选择断链：实测 chain=1 时打白比打普通牌低 600 分，
        # 要到 chain>=3 才翻转——而链到 3 已经封顶（_piao_candidate 返回 None），
        # 即这个奖励项从未在真实决策里生效过。
        # 证据：玄武-2346（连续两周周榜第一）a_a01a4f04a8ea_r1_b8_t0 第 2 局
        # 打出 32 番 +768 分，靠的是三次财飘；其中第 2 次（吃 4w5w6w 把财神从
        # 4w5w白 里解放出来后弃白）我们的 choose_action 复现成了「打 1t」断链。
        # 取 max(piao, chain_count)：若将来服务端下发 piao 则口径不变，当前用
        # chain_count 兜底。回滚：把 max(piao, chain_count) 改回 piao。
        piao_credit = max(piao, chain_count)
        value += (weights["piao"] * piao_credit if discard == "白"
                  else -weights["chain"] * chain_count)
    if discard == "白":
        value += weights.get("discard_joker_soft", -800)
    if (rules or {}).get("YouCaiBiKao") and counts[TILE_INDEX["白"]]:
        value += weights["must_kaoxiang"] if discard != "白" else 0
    return value


def choose_route_discard(tiles, meld_groups=0, chain_count=0, piao=0, rules=None, visible=None):
    if not tiles:
        return None
    weights = load_weights()
    merged = {**weights, **((rules or {}).get("_weights") or {})}

    def _original():
        unique = sorted(set(tiles))
        currents = {}
        for tile in unique:
            remaining = list(tiles)
            remaining.remove(tile)
            counts = to_counts(remaining)
            currents[tile] = route_shanten(counts, meld_groups)
        best = min(currents.values())
        tier = [tile for tile in unique if currents[tile] == best]
        from . import discard_features, nn_discard   # 延迟导入：两者都依赖本模块
        if nn_discard.applies(chain_count, piao, merged):
            pick = nn_discard.choose(tier, tiles, meld_groups, visible, merged, rules)
            if pick is not None:
                return pick
        # 庄家、手上无财神：rule_fitted_discard_dealer_d0_enabled=0 时不走拟合打分，回到原 route_value。
        # tools/dealer_study.py（2026-09-27）：拟合打分接管庄家后，庄/起手无财神 胜率 19.1%（以前 2938 局）
        # → 15.6%（当前 410 局），同期闲家 15.6% → 17.4%、高手庄家 21.2%；庄/1白 当前反而好于高手，不动。
        dealer_d0_old = (bool((rules or {}).get("dealer_hint")) and "白" not in tiles
                         and not merged.get("rule_fitted_discard_dealer_d0_enabled", 1))
        if discard_features.applies(tiles, chain_count, piao, merged) and not dealer_d0_old:
            return max(tier, key=lambda tile: discard_features.score(tiles, tile, meld_groups, visible, merged,
                                                                     rules))
        if discard_features.applies_joker(tiles, chain_count, piao, merged):
            return max(tier, key=lambda tile: discard_features.score_joker(tiles, tile, meld_groups, visible,
                                                                           merged, rules))
        return max(tier, key=lambda tile: route_value(tiles, tile, meld_groups, chain_count, piao, rules, weights,
                                                      visible))

    # 2026-09-28：route EV（mj/route_ev.py，开关 rule_route_ev_enabled，
    # 默认关闭）在算 tier 之前叠加，与 mj.strategy.choose_discard 用同一个
    # 共享 helper，避免两份平行评分体"改一处漏一处"（见 pair_route_bonus 的
    # 收口说明）。延迟导入避免 ev<->discard_features<->route_ev 的循环导入
    # （route_ev 依赖 discard_features，discard_features 依赖本文件顶层的
    # pair_route_bonus，本文件若在顶层导入 route_ev 会在模块初始化中途形成
    # 循环，见本次改动的排查记录）。
    # _original() 本身不便宜；两个叠加逻辑与最终回落最多各调用一次，用惰性
    # 缓存合并成最多一次真实计算（同 mj.strategy.choose_discard，2026-09-28
    # 性能审计）。
    _cache = {}

    def _cached_original():
        if "v" not in _cache:
            _cache["v"] = _original()
        return _cache["v"]

    from . import route_ev
    override = route_ev.maybe_override_discard(tiles, meld_groups, chain_count, piao, rules, merged, visible,
                                               _cached_original)
    if override is not None:
        return override
    # 2026-09-28：P2 速度前瞻，同 mj.strategy.choose_discard，两个入口共用
    # 同一个 mj.route_ev.maybe_override_speed，避免两份平行评分体各写一份。
    speed_override = route_ev.maybe_override_speed(tiles, meld_groups, chain_count, piao, rules, merged,
                                                    visible, _cached_original)
    if speed_override is not None:
        return speed_override
    return _cached_original()
