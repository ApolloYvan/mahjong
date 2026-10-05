"""防守读牌 v2：自摸制下我方弃牌不能点炮，但会喂对手吃碰加速。

逐对手 need 模型：副露 = 需求信号（吃 +3/组，碰 +2/组），牌河 = 放弃信号
（弃该花色 −1/张）；冲刺对手（副露 ≥2 组）危险度加倍。
邻接喂牌：弃牌与冲刺对手已暴露的搭子同花色且相距 ≤2 → 喂牌概率最高。
字牌：对手自己弃过的字不会再碰（安全）；没弃过才有碰风险。
危险度 = 120×需求压力(≤4) + 60×剩余张 + 邻接/字牌加成，供弃牌评分扣减。
"""
from .tiles import TILE_INDEX


def _suit(idx):
    return idx // 9 if idx < 27 else 3


def _at(seq, index):
    if isinstance(seq, list) and 0 <= index < len(seq) and isinstance(seq[index], list):
        return seq[index]
    return []


def _meld_weight(kind):
    return 3 if kind == "chi" else 2


def opp_stats(index, rivers, melds):
    """单对手需求画像：花色 need、暴露牌集合、已弃字牌集合、副露组数。"""
    river = _at(rivers, index)
    groups = _at(melds, index)
    meld_suit = [0, 0, 0]
    exposed = []
    for group in groups:
        tiles = group.get("tiles", []) if isinstance(group, dict) else []
        kind = group.get("kind", "")
        weight = _meld_weight(kind)
        suits_touched = set()
        for name in tiles:
            idx = TILE_INDEX.get(name)
            if idx is None:
                continue
            exposed.append(idx)
            if idx < 27:
                suits_touched.add(idx // 9)
        for s in suits_touched:
            meld_suit[s] += weight
    river_suit = [0, 0, 0]
    river_honors = set()
    for name in river:
        idx = TILE_INDEX.get(name)
        if idx is None:
            continue
        if idx < 27:
            river_suit[idx // 9] += 1
        else:
            river_honors.add(idx)
    need = [max(0, meld_suit[s] - river_suit[s]) for s in range(3)]
    return {
        "meld_groups": len(groups),
        "need": need,
        "exposed": exposed,
        "river_honors": river_honors,
    }


def rivers_count(rivers, name):
    total = 0
    for river in rivers:
        if isinstance(river, list):
            total += river.count(name)
    return total


def melds_count(melds, name):
    total = 0
    if isinstance(melds, list):
        for groups in melds:
            if not isinstance(groups, list):
                continue
            for group in groups:
                tiles = group.get("tiles", []) if isinstance(group, dict) else []
                total += tiles.count(name)
    return total


def penalties(snapshot):
    """每张牌的弃出危险度（≤900），供弃牌评分扣减。"""
    seat = snapshot.get("seat", -1)
    melds = snapshot.get("melds") or []
    rivers = snapshot.get("discards") or []
    if not (isinstance(melds, list) and len(melds) == 4):
        return {}
    wall = snapshot.get("wall_remaining", 99)
    if wall > 45:
        return {}
    opponents = []
    for index in range(4):
        if index == seat:
            continue
        stats = opp_stats(index, rivers, melds)
        stats["sprint"] = 2 if stats["meld_groups"] >= 2 else (1 if stats["meld_groups"] else 0)
        opponents.append(stats)
    if not any(opp["sprint"] >= 2 for opp in opponents):
        return {}
    hand = snapshot.get("my_hand") or []
    result = {}
    for name, idx in TILE_INDEX.items():
        visible = hand.count(name) + rivers_count(rivers, name) + melds_count(melds, name)
        remaining = max(0, 4 - visible)
        if remaining <= 1:
            continue
        danger = 60 * (remaining - 1)
        suit = _suit(idx)
        for opp in opponents:
            sprint = opp["sprint"]
            if not sprint:
                continue
            if idx < 27:
                pressure = min(opp["need"][suit], 4)
                if pressure:
                    danger += 120 * pressure * sprint
                if any(e // 9 == suit and abs(e - idx) <= 2 for e in opp["exposed"]):
                    danger += 120 if sprint >= 2 else 60
            elif idx not in opp["river_honors"] and idx not in opp["exposed"]:
                danger += 120 if sprint >= 2 else 60
        if danger > 60 * (remaining - 1):
            result[name] = min(danger, 900)
    return result
