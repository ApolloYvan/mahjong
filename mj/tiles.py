"""牌码与手牌计数工具。牌码与平台 API 一致：1w-9w / 1b-9b / 1t-9t / 东南西北中发白。
白板 = 财神（百搭）。"""

JOKER = "白"

NUM_TILES = [f"{n}{s}" for s in ("w", "b", "t") for n in range(1, 10)]
HONOR_TILES = ["东", "南", "西", "北", "中", "发", "白"]
ALL_TILES = NUM_TILES + HONOR_TILES  # 34 种
TILE_INDEX = {t: i for i, t in enumerate(ALL_TILES)}
INDEX_TILE = {i: t for t, i in TILE_INDEX.items()}
NSUITS = 34
JOKER_IDX = TILE_INDEX[JOKER]


def is_suited(idx):
    return idx < 27


def rank(idx):
    return idx % 9 + 1 if is_suited(idx) else -1


def to_counts(tiles):
    c = [0] * NSUITS
    for t in tiles:
        c[TILE_INDEX[t]] += 1
    return tuple(c)


def visible_counts(hand, discards, melds):
    """全桌可见张数（34 维）：我方手牌 + 四家牌河 + 全家副露。
    discards/melds 兼容 None；melds 为 4 座位列表（每项为组列表）。"""
    c = [0] * NSUITS
    for t in hand:
        c[TILE_INDEX[t]] += 1
    for river in discards if isinstance(discards, list) else []:
        if isinstance(river, list):
            for t in river:
                idx = TILE_INDEX.get(t)
                if idx is not None:
                    c[idx] += 1
    for groups in melds if isinstance(melds, list) else []:
        if isinstance(groups, list):
            for group in groups:
                tiles = group.get("tiles", []) if isinstance(group, dict) else []
                for t in tiles:
                    idx = TILE_INDEX.get(t)
                    if idx is not None:
                        c[idx] += 1
    return c


def counts_to_tiles(counts):
    out = []
    for i, n in enumerate(counts):
        out.extend([INDEX_TILE[i]] * n)
    return out


def jokers_of(counts):
    return counts[JOKER_IDX]
