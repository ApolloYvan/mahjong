"""基础向听计算。用于初版策略，不替代最终番型 EV 搜索。"""
from functools import lru_cache

from .tiles import JOKER, JOKER_IDX, NSUITS, is_suited, rank, to_counts


def _remove(c, i, n=1):
    values = list(c)
    values[i] -= n
    return tuple(values)


@lru_cache(maxsize=1 << 22)
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


@lru_cache(maxsize=1 << 19)
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


@lru_cache(maxsize=1 << 19)
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


def pair_route_allowed(counts, meld_groups=0):
    """七对路线是否有资格参与弃牌分层。

    2026-09-22 实战审计：旧口径 route_shanten = min(std, pair) 会让七对路线在
    "持平"甚至"仅微弱领先"时就接管门清弃牌分层。真实 1672 局数据显示，一局中
    七对路线领跑 >=6 手的 121 局，胜率 1.7%、分/局 -5.63；而从未走七对路线的
    589 局胜率 25.1%、分/局 -0.05（与随机基线持平）。收益端：七对只值 fan 2，
    我们 343 次胡牌中七对仅 13 次（3.8%），与全场对手持平——付出速度代价却无
    超额完成率。故把门槛提高为"手上已经有真实的七对雏形，且七对路线严格更快"。

    回滚：把本函数改成 `return meld_groups == 0` 即完全恢复旧行为。
    """
    if meld_groups:
        return False            # 副露永久关闭七对（既有语义，不变）
    real_pairs = sum(1 for i, n in enumerate(counts)
                     if n >= 2 and i != JOKER_IDX)   # 白是万能牌，不算真实对子
    if real_pairs < 5:
        return False
    return pair_shanten(counts) < shanten(counts, meld_groups)   # 必须严格更快


@lru_cache(maxsize=1 << 19)
def _route_shanten_cached(counts, meld_groups):
    std = shanten(counts, meld_groups)
    if meld_groups:
        return std
    if not pair_route_allowed(counts, meld_groups):
        return std
    return min(std, pair_shanten(counts))


def route_shanten(counts, meld_groups=0):
    """双路线最快向听（不计算进张，供弃牌分层筛选用）。

    2026-09-22 性能审计：门清弃牌需要对 14 张候选各评估一次 combined_route，
    2 张财神的手牌单次 ~11ms，是 0 财神手牌的 ~1200 倍，直接导致响应窗口超时。
    这里在 _search/_ukeire_cached 之上再加一层缓存（同一 (counts, meld_groups)
    在 choose_discard 的分层与 _discard_score 排序两处各算一次，是最直接的重复
    计算），纯粹是缓存，不改变任何返回值——返回值锁定见 tests/test_shanten_perf.py。
    """
    return _route_shanten_cached(tuple(counts), meld_groups)


@lru_cache(maxsize=1 << 19)
def _combined_route_cached(counts, meld_groups):
    current = _route_shanten_cached(counts, meld_groups)
    if current > 2:
        return current, ()
    if meld_groups:
        return current, tuple(ukeire(counts, meld_groups))
    if not pair_route_allowed(counts, meld_groups):
        return current, tuple(ukeire(counts, meld_groups))
    std = shanten(counts, meld_groups)
    pair = pair_shanten(counts)
    if pair < std:
        return pair, tuple(pair_ukeire(counts))
    std_waits = ukeire(counts, meld_groups)
    if pair == std:
        return pair, tuple(sorted(set(std_waits) | set(pair_ukeire(counts))))
    return std, tuple(std_waits)


def combined_route(counts, meld_groups=0):
    """标准与七对双路线并行评估：返回 (向听, 进张列表)。
    取更近的路线定向听；同向听时进张取两路线并集（真两侧听）。
    深向听（>2）不计算进张——下游进张权重仅在 ≤2 向听生效。"""
    current, waits = _combined_route_cached(tuple(counts), meld_groups)
    return current, list(waits)


def _warmup():
    """进程启动预热：在真实决策窗口之前，把常见的财神分支（0~4 张财神 x 若干
    典型形状）跑进 _search/_ukeire_cached 的缓存，避免首次命中的冷启动成本落在
    真实响应窗口里。纯只读预热，不改变任何函数的返回值——只是提前算好。"""
    base_shapes = [
        ["1w", "2w", "3w", "5b", "6b", "7b", "9t", "9t", "东", "南", "西", "中", "发"],
        ["1w", "1w", "2w", "3w", "4w", "5b", "6b", "7b", "9t", "9t", "东", "南", "西"],
        ["1w", "2w", "3w", "4w", "5w", "6w", "7b", "8b", "9b", "1t", "2t", "3t", "东"],
        ["1w", "1w", "2w", "2w", "3w", "3w", "5b", "5b", "6b", "6b", "9t", "9t", "东"],
    ]
    for shape in base_shapes:
        for jokers in range(5):
            tiles = shape[: max(0, 13 - jokers)] + [JOKER] * jokers
            counts = tuple(to_counts(tiles)) if len(tiles) == 13 else None
            if counts is None:
                continue
            for meld_groups in (0, 1):
                try:
                    combined_route(counts, meld_groups)
                except Exception:
                    continue


try:
    _warmup()
except Exception:
    pass
