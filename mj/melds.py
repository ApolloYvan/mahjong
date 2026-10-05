"""副露状态与手牌结构校验。"""


def meld_count(melds, kind=None):
    if kind is None:
        return len(melds)
    return sum(meld.get("kind") == kind for meld in melds)


def can_chi(melds):
    return meld_count(melds, "chi") < 2


def concealed_count(hand, melds):
    return len(hand)


def groups_needed(melds):
    return 4 - meld_count(melds)


def hand_shape_valid(hand, melds):
    expected = 14 - 3 * meld_count(melds)
    return len(hand) == expected and 0 <= groups_needed(melds) <= 4


def apply_peng(hand, melds, tile):
    if hand.count(tile) < 2:
        return False
    hand.remove(tile)
    hand.remove(tile)
    melds.append({"kind": "peng", "tiles": [tile] * 3})
    return True


def apply_chi(hand, melds, tiles, claimed):
    if not can_chi(melds) or len(tiles) != 2 or any(hand.count(tile) < 1 for tile in tiles):
        return False
    for tile in tiles:
        hand.remove(tile)
    melds.append({"kind": "chi", "tiles": tiles + [claimed]})
    return True


def apply_gang(hand, melds, tile):
    if hand.count(tile) < 4:
        return False
    for _ in range(4):
        hand.remove(tile)
    melds.append({"kind": "gang", "tiles": [tile] * 4})
    return True
