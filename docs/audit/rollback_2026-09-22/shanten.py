"""基础向听计算。用于初版策略，不替代最终番型 EV 搜索。"""
from functools import lru_cache

from .tiles import JOKER_IDX, NSUITS, is_suited, rank


def _remove(c, i, n=1):
    values = list(c)
    values[i] -= n
    return tuple(values)


@lru_cache(maxsize=1 << 20)
def _search(c, jokers, i=0, melds=0, pairs=0, taatsu=0):
    while i < NSUITS and c[i] == 0:
        i += 1
    if i >= NSUITS:
        melds = min(melds, 4)
        taatsu = min(taatsu, 4 - melds)
        if pairs == 0 and jokers >= 2:
            pairs = 1
        return 8 - melds * 2 - taatsu - min(pairs, 1)
    best = _search(_remove(c, i), jokers, i, melds, pairs, taatsu)
    n = c[i]
    if n >= 3:
        best = min(best, _search(_remove(c, i, 3), jokers, i, melds + 1, pairs, taatsu))
    elif n >= 2:
        best = min(best, _search(_remove(c, i, 2), jokers, i, melds, pairs + 1, taatsu))
        if jokers:
            best = min(best, _search(_remove(c, i, 2), jokers - 1, i, melds + 1, pairs, taatsu))
    elif n == 1 and jokers >= 1:
        best = min(best, _search(_remove(c, i), jokers - 1, i, melds, pairs + 1, taatsu))
        best = min(best, _search(_remove(c, i), jokers - 1, i, melds, pairs, taatsu + 1))
    if jokers >= 2:
        best = min(best, _search(_remove(c, i), jokers - 2, i, melds + 1, pairs, taatsu))
    if is_suited(i):
        r = rank(i)
        if r <= 7:
            need = (c[i + 1] == 0) + (c[i + 2] == 0)
            if need <= jokers:
                values = list(c)
                values[i] -= 1
                if c[i + 1]:
                    values[i + 1] -= 1
                if c[i + 2]:
                    values[i + 2] -= 1
                best = min(best, _search(tuple(values), jokers - need, i, melds + 1, pairs, taatsu))
        if r <= 8:
            need = 0 if c[i + 1] else 1
            if need <= jokers:
                values = list(c)
                values[i] -= 1
                if c[i + 1]:
                    values[i + 1] -= 1
                best = min(best, _search(tuple(values), jokers - need, i, melds, pairs, taatsu + 1))
        if r <= 7:
            need = 0 if c[i + 2] else 1
            if need <= jokers:
                values = list(c)
                values[i] -= 1
                if c[i + 2]:
                    values[i + 2] -= 1
                best = min(best, _search(tuple(values), jokers - need, i, melds, pairs, taatsu + 1))
    return best


def shanten(counts, meld_groups=0):
    real = list(counts)
    jokers = real[JOKER_IDX]
    real[JOKER_IDX] = 0
    return _search(tuple(real), jokers, melds=meld_groups)


def _pair_score(counts):
    real = list(counts)
    jokers = real[JOKER_IDX]
    real[JOKER_IDX] = 0
    pairs = sum(n // 2 for n in real)
    singles = sum(n % 2 for n in real)
    used = min(singles, jokers)
    pairs += used + (jokers - used) // 2
    return 6 - pairs


def pair_shanten(counts):
    """七对向听：与 rules.seven_pairs 判定一致——不要求七种异种，四张算两对
    （平台有三豪华七对，故必有同种多对）。财神可配单张成对或两张自配成对。"""
    return max(0, _pair_score(counts))


@lru_cache(maxsize=1 << 18)
def _ukeire_cached(c, meld_groups):
    jokers = c[JOKER_IDX]
    real = list(c)
    real[JOKER_IDX] = 0
    real = tuple(real)
    current = _search(real, jokers, melds=meld_groups)
    result = []
    for i in range(NSUITS):
        if i == JOKER_IDX:
            if jokers < 4 and _search(real, jokers + 1, melds=meld_groups) < current:
                result.append(i)
            continue
        if real[i] >= 4:
            continue
        values = list(real)
        values[i] += 1
        if _search(tuple(values), jokers, melds=meld_groups) < current:
            result.append(i)
    return tuple(result)


def ukeire(counts, meld_groups=0):
    return list(_ukeire_cached(tuple(counts), meld_groups))


@lru_cache(maxsize=1 << 18)
def _pair_ukeire_cached(c):
    current = _pair_score(c)
    result = []
    for i in range(NSUITS):
        if c[i] >= 4:
            continue
        values = list(c)
        values[i] += 1
        if _pair_score(tuple(values)) < current:
            result.append(i)
    return tuple(result)


def pair_ukeire(counts):
    """七对路线进张：摸入后 pair_shanten 下降的牌（含财神）。"""
    return list(_pair_ukeire_cached(tuple(counts)))


def route_shanten(counts, meld_groups=0):
    """双路线最快向听（不计算进张，供弃牌分层筛选用）。"""
    std = shanten(counts, meld_groups)
    if meld_groups:
        return std
    return min(std, pair_shanten(counts))


def combined_route(counts, meld_groups=0):
    """标准与七对双路线并行评估：返回 (向听, 进张列表)。
    取更近的路线定向听；同向听时进张取两路线并集（真两侧听）。
    深向听（>2）不计算进张——下游进张权重仅在 ≤2 向听生效。"""
    current = route_shanten(counts, meld_groups)
    if current > 2:
        return current, []
    if meld_groups:
        return current, ukeire(counts, meld_groups)
    std = shanten(counts, meld_groups)
    pair = pair_shanten(counts)
    if pair < std:
        return pair, pair_ukeire(counts)
    std_waits = ukeire(counts, meld_groups)
    if pair == std:
        return pair, sorted(set(std_waits) | set(pair_ukeire(counts)))
    return std, std_waits
