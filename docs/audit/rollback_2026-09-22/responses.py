"""响应窗口的吃碰安全门禁。

吃/碰后必须立即弃一张；因此不能只比较「拿走两张牌」的中间态。生产策略
统一模拟声明后的所有合法弃牌，以最优后继的双路线向听/有效进张作硬性止损。
财神、抓打圈、两摊吃上限和墙尾规则由原有路径继续负责。
"""
from .melds import can_chi
from .shanten import combined_route, pair_shanten, shanten
from .tiles import ALL_TILES, INDEX_TILE, TILE_INDEX, to_counts


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
    if drawn and drawn != "白" and hand.count(drawn) >= 4:
        return {"action": "gang", "tile": drawn}
    for tile in ALL_TILES:
        if tile != "白" and hand.count(tile) == 4:
            return {"action": "gang", "tile": tile}
    for meld in _melds(snapshot):
        tiles = meld.get("tiles", []) if isinstance(meld, dict) else []
        if tiles and len(tiles) == 3 and len(set(tiles)) == 1 and hand.count(tiles[0]) >= 1:
            return {"action": "gang", "tile": tiles[0]}
    return None


def choose_peng(snapshot):
    tile = _discarded(snapshot)
    hand = _hand(snapshot)
    if not tile or tile == "白" or hand.count(tile) < 2:
        return None
    if snapshot.get("wall_remaining", 99) <= 4 and snapshot.get("chain_count", 0) == 0:
        return None
    if not claim_assessment(snapshot, (tile, tile))["allowed"]:
        return None
    return {"action": "peng", "tile": tile}


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
    return {"action": "gang", "tile": tile}


def claim_assessment(snapshot, take):
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
        after_shanten, after_ukeire = combined_route(to_counts(left), meld_groups + 1)
        successors.append((after_shanten, -len(after_ukeire), discard, len(after_ukeire)))
    if not successors:
        return result
    after_shanten, neg_ukeire, discard, after_ukeire = min(successors)
    result.update(after_shanten=after_shanten, after_ukeire=after_ukeire, discard=discard)

    if after_shanten > before_shanten:
        result["reason"] = "route_shanten_worsens"
        return result
    if meld_groups:
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
    pair_route_effective = pair_shanten(before_counts) <= standard_before
    if after_shanten < before_shanten:
        result.update(allowed=True, reason="first_meld_improves")
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
    meld_groups = _meld_groups(snapshot)
    best = None
    for names in candidates:
        assessment = claim_assessment(snapshot, names)
        if not assessment["allowed"]:
            continue
        key = (assessment["after_shanten"], -assessment["after_ukeire"])
        if best is None or key < best[0]:
            best = (key, names)
    if best is None:
        return None
    return {"action": "chi", "tile": tile, "tiles": best[1]}
