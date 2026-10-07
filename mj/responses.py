"""响应窗口的吃碰安全门禁。

吃/碰后必须立即弃一张；因此不能只比较「拿走两张牌」的中间态。生产策略
统一模拟声明后的所有合法弃牌，以最优后继的双路线向听/有效进张作硬性止损。
财神、抓打圈、两摊吃上限和墙尾规则由原有路径继续负责。
"""
from .fit import load_weights
from .melds import can_chi
from .rules import baotou
from .shanten import combined_route, pair_route_allowed, route_shanten, shanten
from .tiles import ALL_TILES, INDEX_TILE, TILE_INDEX, to_counts


# 2026-09-24：吃碰的后继弃牌与吃口选择，在同向听时优先"弃完能做成爆头"的那一支。
#
# 依据（tools/joker_playbook.py，按 手上财神数 × 副露组数 拆胡牌那一刻的爆头率）：
#
#              1白0露  1白1露  1白2露  2白0露  2白1露  2白2露
#   歪比巴卜      17%    16%    45%    80%    79%    81%
#   腾蛇-0638    33%    30%    54%     -     77%    62%
#   我们          2%     1%    19%     2%    12%    39%
#
# 每一格都是倍数级落后，而且**所有人的爆头率都随副露数单调上升**——吃碰是造爆头
# 的手段，不是它的对立面。而我们原来的判据 (after_shanten, -after_ukeire) 对爆头
# 零要求：同向听同听口时选哪一张纯看 TILE_INDEX 顺序，能做成爆头的那一支经常被
# 无差别地丢掉。
#
# 为什么值这个改动：爆头是 ×2 番，而 tools/joker4_funnel.py 证明我们番数差距的
# **100%** 来自"胡牌里爆头占比"——我们 13.4%、对手 25.1%，非爆头的手两边番数
# 完全一样（都≈1.0）。番/胡 1.17→1.253（当前胜率下的前4线）只需要把这个占比
# 做到 21.1%，仍低于对手现状。
#
# 只在 after_shanten 相同时生效，绝不为了爆头牺牲向听——爆头本身蕴含听牌
# (after_shanten == 0)，所以 baotou() 只在听牌候选上调用，开销有界。
# 回滚：把 PREFER_BAOTOU_SUCCESSOR 设成 False。
# 2026-09-24 赛前回滚：11 新房 252 次胡牌，爆头占比 13.4% → 9.1%（z≈2.4，
# 方向为负）。本开关只控制**排序偏好**，不影响听牌分支的 after_baotou 判据。
PREFER_BAOTOU_SUCCESSOR = False


def _hand(snapshot):
    return snapshot.get("my_hand") or []


def _discarded(snapshot):
    return snapshot.get("window_tile") or snapshot.get("last_discard") or snapshot.get("discarded_tile")


def _melds(snapshot):
    """我方副露组：兼容 4 座位列表与 {seat: groups} 字典两种快照形态。"""
    melds = snapshot.get("melds") or []
    seat = snapshot.get("seat", -1)
    if isinstance(melds, dict):
        return melds.get(str(seat), melds.get(seat, []))
    if isinstance(melds, list) and 0 <= seat < len(melds) and isinstance(melds[seat], list):
        return melds[seat]
    return []


def _meld_groups(snapshot):
    return len(_melds(snapshot))


def gang_hurts_shape(hand, tile, kind, meld_groups):
    """杠了要拆牌型：杠后向听 > 不杠向听（都按 13 张口径、补牌之前）。
    hand：暗杠/补杠为摸牌后含摸到那张的手牌；明杠为现有手牌（不含别家打出的那张）。"""
    if kind == "ming":
        before = route_shanten(tuple(to_counts(hand)), meld_groups)
    else:
        before = 9
        for t in set(hand):
            rest = list(hand)
            rest.remove(t)
            before = min(before, route_shanten(tuple(to_counts(rest)), meld_groups))
    left = list(hand)
    for _ in range({"an": 4, "bu": 1, "ming": 3}[kind]):
        left.remove(tile)
    after = route_shanten(tuple(to_counts(left)), meld_groups + (0 if kind == "bu" else 1))
    return after > before


def _gang_gate(hand, tile, kind, meld_groups):
    return not (load_weights().get("rule_gang_no_hurt_enabled", 0)
                and gang_hurts_shape(hand, tile, kind, meld_groups))


def choose_gang(snapshot):
    """暗杠门禁（P0 修复：牌数语义纠正）。

    生产快照的 ``my_hand`` **包含** ``drawn_tile`` 本身（摸牌后手牌是
    "原有 N 张 + 摸到的这 1 张"）。因此判断"是否凑齐 4 张暗杠"必须要求
    ``hand.count(drawn) >= 4``，而不是 ``>= 3``——旧版 ``>=3`` 把"手牌里
    含摸到的这张共 3 张"（即摸牌前只有 2 张，根本不够暗杠）误判为"摸牌前
    已有 3 张 + 摸到 1 张 = 4 张"，导致对生产环境提交非法暗杠，被
    409（``cannot gang <tile>``）拒绝（真实拒绝案例见
    ``docs/refactor/LIVE_TEST_001.md`` §3、``tests/test_responses.py``
    的 ``GangHandCountFixtureTests``）。
    """
    if snapshot.get("wall_remaining", 99) <= 20:
        return None
    hand = _hand(snapshot)
    drawn = snapshot.get("drawn_tile")
    groups = _meld_groups(snapshot)
    if drawn and drawn != "白" and hand.count(drawn) >= 4 and _gang_gate(hand, drawn, "an", groups):
        return {"action": "gang", "tile": drawn}
    for tile in ALL_TILES:
        if tile != "白" and hand.count(tile) == 4 and _gang_gate(hand, tile, "an", groups):
            return {"action": "gang", "tile": tile}
    for meld in _melds(snapshot):
        tiles = meld.get("tiles", []) if isinstance(meld, dict) else []
        if tiles and len(tiles) == 3 and len(set(tiles)) == 1 and hand.count(tiles[0]) >= 1 \
                and _gang_gate(hand, tiles[0], "bu", groups):
            return {"action": "gang", "tile": tiles[0]}
    return None


def _route_ev_ctx(snapshot):
    return {"wall": snapshot.get("wall_remaining", 60),
            "dealer": snapshot.get("dealer") == snapshot.get("seat"),
            "round_no": snapshot.get("round_no")}


def _route_ev_visible(snapshot, hand):
    try:
        from .tiles import visible_counts
        return visible_counts(hand, snapshot.get("discards") or [], snapshot.get("melds") or [])
    except Exception:
        return None


def _route_ev_vetoed(snapshot, hand, take):
    """吃/碰是否会葬送七对系路线的期望值（mj/route_ev.py，开关
    rule_route_ev_enabled，默认关闭）。只在函数最前面否决，不改变原有门禁
    顺序；范围外或开关关闭时零开销。"""
    if _meld_groups(snapshot) != 0:
        return False
    from . import route_ev
    return route_ev.claim_route_veto(hand, take, _route_ev_ctx(snapshot), load_weights(),
                                     _route_ev_visible(snapshot, hand))


def _route_ev_claim_decision(snapshot, hand, take):
    """P5（开关 rev_claim_enabled，默认关闭；docs/IMPL_UNIFIED_EV_V2.md）：
    吃碰最前面整体比较"不吃碰"与"吃碰后打最优一张"两笔账，返回
    True=按账接受、False=按账否决、None=账本没有明确意见（调用方回落原有
    门禁，含 ``claim_route_veto``）。"""
    weights = load_weights()
    if not weights.get("rev_claim_enabled", 0):
        return None
    from . import route_ev
    return route_ev.claim_ev_decision(hand, take, _route_ev_ctx(snapshot), weights,
                                      _route_ev_visible(snapshot, hand), _meld_groups(snapshot))


def choose_peng(snapshot):
    tile = _discarded(snapshot)
    hand = _hand(snapshot)
    if not tile or tile == "白" or hand.count(tile) < 2:
        return None
    if snapshot.get("wall_remaining", 99) <= 4 and snapshot.get("chain_count", 0) == 0:
        return None
    if _dealer_flat_peng_veto(snapshot, hand, tile):
        return None
    claim_decision = _route_ev_claim_decision(snapshot, hand, (tile, tile))
    if claim_decision is True:
        return {"action": "peng", "tile": tile}
    if claim_decision is False:
        return None
    if _route_ev_vetoed(snapshot, hand, (tile, tile)):
        return None
    fitted = _fitted_claim_scores(snapshot, [(tile, tile)])
    if fitted is not None:
        return {"action": "peng", "tile": tile} if fitted and fitted[0][0] > 0 else None
    if not claim_assessment(snapshot, (tile, tile))["allowed"]:
        return None
    return {"action": "peng", "tile": tile}


def _dealer_flat_peng_veto(snapshot, hand, tile):
    """开关 dealer_joker_flat_peng_veto（默认 0）：做庄且手上有财神时，不碰「碰完向听不变、也不成爆头」的牌。
    2026-10-07 tools/role_diff.py（起手 1 财神）：这类碰 强手做庄接受 64%，我们 80%；
    强手做庄时到过爆头听 +3.1pp、爆头占胡 +3.1pp，我们反而 −4.4 / −12.9。"""
    if not load_weights().get("dealer_joker_flat_peng_veto", 0):
        return False
    if snapshot.get("dealer") != snapshot.get("seat") or "白" not in hand:
        return False
    a = claim_assessment(snapshot, (tile, tile), weights={})
    if a.get("after_shanten") is None or a.get("before_shanten") is None:
        return False
    return a["after_shanten"] >= a["before_shanten"] and not a.get("after_baotou")


def _fitted_claim_scores(snapshot, takes):
    """开关 rule_fitted_claim_enabled 且已写入 fc_* 权重时，返回 [(得分, take)]（按得分降序，
    无合法后继的选项剔除）；否则返回 None，调用方走原有门禁。"""
    weights = load_weights()
    from . import claim_features
    if not claim_features.applies(weights):
        return None
    hand = _hand(snapshot)
    meld_groups = _meld_groups(snapshot)
    chi_existing = sum(1 for m in _melds(snapshot) if isinstance(m, dict) and m.get("kind") == "chi")
    is_dealer = snapshot.get("dealer") == snapshot.get("seat")
    out = []
    for take in takes:
        f = claim_features.option_features(hand, take, meld_groups, chi_existing,
                                           snapshot.get("wall_remaining", 99), is_dealer)
        if f is not None:
            out.append((claim_features.score(f, weights), take))
    return sorted(out, key=lambda x: -x[0])


def response_gang_take(snapshot):
    """response_peng 窗口的最小明杠门禁（接入指南：response_peng 窗口允许
    明杠）。仅在以下条件全部满足时才提交 gang，且明杠优先于 peng；任一
    条件不满足则返回 None，调用方按原有 peng/pass 逻辑继续：

    - phase 必须是 response_peng 且 seat 在 responding_seats 中；
    - window_tile 不是财神（财神不可被杠，官方硬规则）；
    - 手牌中至少 3 张 window_tile（+ 出牌者打出的这 1 张 = 4 张明杠）；
    - wall_remaining>20（墙尾禁杠，口径与 responses.choose_gang()/
      bot._gang_bomb_choice() 统一）；
    - 响应者自身不是抓打圈受限方（god.catch_play 且
      god_discarder_seat != 己方座位时禁止任何声明，含明杠）；
    - 不改变吃碰止损的适用范围：本函数仍是杠的既有最小门禁，不把吃碰
      的后继弃牌规则错误套用到杠。
    """
    if snapshot.get("phase") != "response_peng":
        return None
    seat = snapshot.get("seat", -1)
    if seat not in (snapshot.get("responding_seats") or []):
        return None
    tile = _discarded(snapshot)
    if not tile or tile == "白":
        return None
    hand = _hand(snapshot)
    if hand.count(tile) < 3:
        return None
    if snapshot.get("wall_remaining", 99) <= 20:
        return None
    god = snapshot.get("god") or {}
    if god.get("catch_play") and god.get("god_discarder_seat") != seat:
        return None
    if not _gang_gate(hand, tile, "ming", _meld_groups(snapshot)):
        return None
    return {"action": "gang", "tile": tile}


def claim_assessment(snapshot, take, weights=None):
    """模拟一次 chi/peng 及其强制后继弃牌，返回可审计的门禁结果。

    ``take`` 是从手中消耗的两张牌（弃牌窗口的第三张不在 ``my_hand``）。
    对每一种后继弃牌都计算 ``combined_route``；同向听时有效进张更多者胜。
    这既防止把中间 11 张手牌误当作可停留状态，也让离线复盘能重用与生产
    完全相同的判定。
    """
    hand = list(_hand(snapshot))
    meld_groups = _meld_groups(snapshot)
    before_counts = to_counts(hand)
    before_shanten, before_ukeire = combined_route(before_counts, meld_groups)
    result = {
        "allowed": False, "reason": "no_legal_successor_discard",
        "before_shanten": before_shanten, "before_ukeire": len(before_ukeire),
        "after_shanten": None, "after_ukeire": None, "discard": None,
    }
    try:
        for tile in take:
            hand.remove(tile)
    except ValueError:
        result["reason"] = "claim_tiles_not_in_hand"
        return result

    successors = []
    for discard in sorted(set(hand), key=lambda name: TILE_INDEX.get(name, 99)):
        left = list(hand)
        left.remove(discard)
        left_counts = to_counts(left)
        after_shanten, after_ukeire = combined_route(left_counts, meld_groups + 1)
        after_bt = bool(after_shanten == 0 and baotou(left_counts, meld_groups + 1))
        rank = (0 if (PREFER_BAOTOU_SUCCESSOR and after_bt) else 1)
        successors.append((after_shanten, rank, -len(after_ukeire),
                           discard, len(after_ukeire), after_bt))
    if not successors:
        return result
    after_shanten, _bt_rank, _neg, discard, after_ukeire, after_baotou = min(successors)
    result.update(after_shanten=after_shanten, after_ukeire=after_ukeire, discard=discard,
                  after_baotou=after_baotou)

    if after_shanten > before_shanten:
        result["reason"] = "route_shanten_worsens"
        return result

    # 2026-09-23 实测：已听牌时的吃碰，旧判据只要求「向听不变差」，对听口宽窄
    # 零要求——听口从 12 张砍到 3 张也算「不变差」，于是我们 80.7% 的听牌后吃
    # 机会全部放行，对手只吃 23.7%（n=1220 vs 5361，z=+44.9，全数据集最大的
    # 一条行为差异，且是主动决策不是超时）。
    #
    # 但方向不是「少吃」：实测吃碰后我们听口 14.11→22.82 张、爆头率 2.1%→13.2%，
    # 对手 14.13→30.46 张、1.2%→22.8%。吃碰会让爆头**更容易**（手牌变短，
    # 「摸任意牌都胡」更好凑）。差别在于对手**挑着吃**——同样的起点，他们每口
    # 的听口增益是我们的近两倍。
    #
    # 所以这里要求：听牌状态下的吃碰，必须做成爆头，或让听口严格加宽。
    # 原本已是爆头的手，其听口已接近最大，加宽条件自然不满足 → 被拒，
    # 正好保住爆头（爆头是 ×2 番，而番/胡 +0.1 值 +0.272 分/局，比胜率 +1pp 还多；
    # 我们持财神时爆头率 18.7%，高手 30~36%）。
    #
    # 回滚：把 tenpai_claim_margin 设成一个很大的负数即可完全恢复旧行为。
    #
    # 2026-09-24 收敛任务 S2（tools/five_questions_s2s3.py，完整数字见
    # docs/experiments/OFFLINE_REPORT.md「五问结论」S2）：上面这段
    # margin=1 的判据是用"吃+碰合并统计"的口径校准的（对手 23.7% vs
    # 我们 80.7%）。这次把 claim.csv 里 72174 个真实响应窗口按 chi/peng_gang
    # 分开重新统计，方向在两类里并不一样——
    #
    #   听牌态窗口       我们接受率   高手接受率     z / p
    #   chi             49.2%       48.1%         基本持平，无缺口
    #   peng_gang       65.0%       80.6%         z=6.90, p≈5e-12（高手更愿意接）
    #
    # 换句话说：margin=1 修的是 chi 那部分的"来者不拒"，方向没错；但同一个
    # margin 也套用在 peng/gang 上，而 peng/gang 的真实缺口方向恰好相反——
    # 我们比高手更保守。同人内部 Δ(y_score)（accept vs pass，房间聚类自助
    # CI，70/30 房间留出复现）：
    #   Δ=5.86 [4.01,7.39]（train 5.95[4.20,7.53] / test 5.52[1.06,8.67]）
    # 覆盖率（我方 peng/gang 听牌态窗口占全部我方 claim 决策点）8.13%——
    # 本轮五问里覆盖率最高的一格，预期收益 Δ×覆盖率 ≈ 0.476，是本轮最大的
    # 单条收益来源。跨人一致性 66/81=81.5%。
    #
    # 因为这与"3天前刚做过的修复"方向部分相反，只新增一个默认关闭的窄口径
    # 开关（只放松 peng/gang，不动 chi 的 margin），不直接改 margin 默认值，
    # 留给实战 A/B 复核（见 mj/fit.py 里本开关的注释）。
    if before_shanten == 0:
        weights = weights or load_weights()
        margin = weights.get("tenpai_claim_margin", 1)
        is_peng_or_gang = len(set(take)) == 1   # peng/gang 传入的 take 恒为两张同种牌
        relax = bool(is_peng_or_gang and weights.get("rule_tenpai_peng_gang_relax_enabled", 0))
        effective_margin = 0 if relax else margin
        if after_baotou:
            result.update(allowed=True, reason="tenpai_claim_reaches_baotou")
        elif after_ukeire >= len(before_ukeire) + effective_margin:
            result.update(allowed=True, reason=("tenpai_claim_peng_gang_relaxed" if relax
                                                else "tenpai_claim_widens_wait"))
        else:
            result["reason"] = "tenpai_claim_no_gain"
        return result

    # 2026-09-24 收敛任务 S6（tools/five_questions_s2s3.py 的姊妹分析，完整
    # 数字见 docs/experiments/OFFLINE_REPORT.md「五问结论」S6）：非听牌态、
    # 向听不变的 chi，我们接受率远高于高手——
    #
    #   非听牌+向听不变 chi   我们接受率   高手接受率
    #   首副露(meld_groups=0)     70.5%       21.9%
    #   已有副露(meld_groups>=1)  88.0%       23.9%
    #
    # 而"能改善向听"的 chi/peng 方向相反（高手接受率反而更高：chi 90.6%
    # vs 81.2%，peng/gang 96.9% vs 89.7%），说明差距specifically 在"向听
    # 不变"这一档，不是笼统的"我们太爱吃碰"。同人内部 Δ(y_score)（accept
    # vs pass，房间聚类自助 CI，70/30 房间留出复现）：
    #   Δ=-1.28 [-1.87,-0.60]（train -1.23[-1.97,-0.35] / test -1.43[-2.29,-0.44]，
    #   即"接受"比"跳过"平均倒扣 1.28 分，pass 更优）
    # 覆盖率（我方非听牌 chi 决策点里，向听不变的比例）15.4%。
    #
    # 只针对 chi，不针对碰/杠（碰/杠向听不变时高手接受率仍然不低，且
    # `non_worsening_open_hand`/中性判据本来就不区分吃碰——这里显式区分）：
    # `take` 是两张不同牌（吃）还是两张同种牌（碰/杠）用
    # ``len(set(take)) == 2`` 判断，与 S2 里 `is_peng_or_gang` 判断同构。
    #
    # 默认关闭；打开后：非听牌态的 chi，若不能让向听严格下降，一律 pass
    # （不管是首副露还是已有副露，覆盖 non_worsening_open_hand 与
    # first_meld_neutral_no_pair_route 两个分支）。
    weights = weights or load_weights()
    is_chi = len(set(take)) == 2   # 吃传入的 take 恒为两张不同牌（组成顺子）
    reject_neutral_chi = bool(is_chi and weights.get("rule_reject_neutral_chi_enabled", 0)
                              and after_shanten == before_shanten)

    if meld_groups:
        if reject_neutral_chi:
            result["reason"] = "neutral_chi_rejected"
            return result
        result.update(allowed=True, reason="non_worsening_open_hand")
        return result

    # 2026-09-22 实战试验：首副露门槛从"必须严格改善向听"放宽成"不变差
    # 即可"（与已有副露时的标准 non_worsening_open_hand 看齐）——真实房间
    # 复盘（a_fa39a4187da2）显示，122次手中确有对子的场合里 92次弃权，
    # 其中42次纯粹因为"向听持平"被拒（并非真的是坏碰），怀疑是胡牌频率
    # 偏低的一个可量化原因。唯一保留的护栏：向听持平时若手上已存在有效
    # 七对路线（pair_route_effective），仍然要求严格改善——副露会永久
    # 关闭七对，不能拿这个路线换一个中性平胡中间态（历史教训，见下方
    # test_pair_route_hand_first_claim_rejected/
    # test_non_improving_first_claim_rejected 两个锁定测试）。若几场
    # 实战后净分转差，把 after_shanten == before_shanten 这个分支删掉、
    # 只保留 first_meld_improves 即可完整回滚到原有"必须改善"口径。
    standard_before = shanten(before_counts, 0)
    pair_route_effective = pair_route_allowed(before_counts, 0)
    if after_shanten < before_shanten:
        result.update(allowed=True, reason="first_meld_improves")
    elif reject_neutral_chi:
        result["reason"] = "neutral_chi_rejected"
    elif (after_shanten == before_shanten and after_ukeire >= len(before_ukeire)
          and not pair_route_effective):
        result.update(allowed=True, reason="first_meld_neutral_no_pair_route")
    else:
        result["reason"] = "first_meld_requires_improvement"
    return result


def choose_chi(snapshot):
    tile = _discarded(snapshot)
    hand = _hand(snapshot)
    if not tile or tile == "白":
        return None
    if snapshot.get("wall_remaining", 99) <= 4 and snapshot.get("chain_count", 0) == 0:
        return None
    index = TILE_INDEX.get(tile)
    if index is None or index >= 27:
        return None
    candidates = []
    for offsets in ((-2, -1), (-1, 1), (1, 2)):
        indexes = [index + offset for offset in offsets]
        if min(indexes) < 0 or max(indexes) >= 27:
            continue
        if any(i // 9 != index // 9 for i in indexes):
            continue
        names = [INDEX_TILE[i] for i in indexes]
        if all(hand.count(name) for name in names):
            candidates.append(names)
    existing_chi = [m for m in _melds(snapshot) if isinstance(m, dict) and m.get("kind") == "chi"]
    if not candidates or not can_chi(existing_chi):
        return None
    decisions = {tuple(names): _route_ev_claim_decision(snapshot, hand, tuple(names)) for names in candidates}
    forced = [names for names in candidates if decisions[tuple(names)] is True]
    if forced:
        best_forced = None
        for names in forced:
            assessment = claim_assessment(snapshot, names)
            key = (assessment.get("after_shanten", 9), -assessment.get("after_ukeire", 0))
            if best_forced is None or key < best_forced[0]:
                best_forced = (key, names)
        return {"action": "chi", "tile": tile, "tiles": best_forced[1]}
    candidates = [names for names in candidates if decisions[tuple(names)] is not False]
    if not candidates:
        return None
    candidates = [names for names in candidates if not _route_ev_vetoed(snapshot, hand, tuple(names))]
    if not candidates:
        return None
    fitted = _fitted_claim_scores(snapshot, [tuple(names) for names in candidates])
    if fitted is not None:
        if fitted and fitted[0][0] > 0:
            return {"action": "chi", "tile": tile, "tiles": list(fitted[0][1])}
        return None
    meld_groups = _meld_groups(snapshot)
    best = None
    for names in candidates:
        assessment = claim_assessment(snapshot, names)
        if not assessment["allowed"]:
            continue
        # 同向听时，能做成爆头的那一口优先（爆头蕴含 after_shanten == 0，
        # 所以这个次级键不会越过向听去抢排序）
        key = (assessment["after_shanten"],
               0 if (PREFER_BAOTOU_SUCCESSOR and assessment.get("after_baotou")) else 1,
               -assessment["after_ukeire"])
        if best is None or key < best[0]:
            best = (key, names)
    if best is None:
        return None
    return {"action": "chi", "tile": tile, "tiles": best[1]}
