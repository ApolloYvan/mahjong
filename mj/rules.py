"""平台麻将胡牌、七对、爆头与番数计算。"""
from functools import lru_cache

from .tiles import NSUITS, JOKER_IDX, is_suited, rank

BRANCH_FAN = {1: 2, 2: 4, 3: 8, 4: 16}
# 2026-09-24 用全量语料 round_ended 真实 detail 字段核实后改为服务端真实
# 文本（tools/fan_breakdown.py::sanity_check，11082 个胡牌事件零例外）：
# 真实标签是"七对"（不带"子"）、"豪华七对×1"/"豪华七对×2"（用 ×N 表示分支
# 档位，N=1 对应原来的"豪华七对"、N=2 对应原来的"双豪华七对"，fan 值不变）。
# "豪华七对×3"（对应 branch=4，16番）未在语料中实际出现过，按 ×1/×2 的
# 命名规律外推，如实标注：格式是外推的，fan 数值本身从建库起就没变过。
BRANCH_NAME = {1: "七对", 2: "豪华七对×1", 3: "豪华七对×2", 4: "豪华七对×3"}


def strip_joker(counts):
    real = list(counts)
    jokers = real[JOKER_IDX]
    real[JOKER_IDX] = 0
    return tuple(real), jokers


def _remove(c, i, n=1):
    values = list(c)
    values[i] -= n
    return tuple(values)


@lru_cache(maxsize=1 << 20)
def _melds(c, groups, jokers):
    """判断 c 能否拆成 groups 组面子，剩余财神补缺。"""
    i = next((i for i, n in enumerate(c) if n), NSUITS)
    if i == NSUITS:
        return jokers >= groups * 3
    if groups == 0:
        return False
    n = c[i]
    if n >= 3 and _melds(_remove(c, i, 3), groups - 1, jokers):
        return True
    if n >= 2 and jokers >= 1 and _melds(_remove(c, i, 2), groups - 1, jokers - 1):
        return True
    if n >= 1 and jokers >= 2 and _melds(_remove(c, i), groups - 1, jokers - 2):
        return True
    if is_suited(i) and rank(i) <= 7:
        need = (c[i + 1] == 0) + (c[i + 2] == 0)
        if need <= jokers:
            values = list(c)
            values[i] -= 1
            if c[i + 1]:
                values[i + 1] -= 1
            if c[i + 2]:
                values[i + 2] -= 1
            if _melds(tuple(values), groups - 1, jokers - need):
                return True
    if is_suited(i):
        # 财神补顺子头/中缺：顺子从 i-2 或 i-1 起头（起点可为空位）。
        #
        # 真实 bug（a_db9e594544b6_r1_b4_t0 第 6 局实锤，引擎判"平胡·爆头"
        # fan=2，服务端"平胡"fan=1）：这里只检查了 start 和 i 同花色
        # （``start // 9 == i // 9``），没检查 span 的另一端 ``start + 2``
        # 也在同一花色里——花色按 9 张一段连续排布（w:0-8/b:9-17/t:18-26），
        # 当 i 落在花色末尾两张（rank 8/9）时，start=i-1 或 i-2 仍在本花色，
        # 但 span=(start,start+1,start+2) 会跨到下一花色的第 1 张，被当成
        # "同花顺"误判（比如 i=9w(idx8)、start=7(8w)，span=(7,8,9)=
        # (8w,9w,1b)，1b 被当成顺子的第三张）。真实样本：手牌剩
        # 1b/9w + 2 张财神，_melds 把 (8w,9w,1b) 这种跨花色的"顺子"判定成
        # 合法面子，财神补了根本不存在的"8w"，让这手本该卡死（1b、9w 两张
        # 孤张，一张财神顶多配出一对，配不出面子）的牌被判定成万能听
        # （爆头）。修法：span 的最后一张也必须跟 i 同花色。
        for start in (i - 2, i - 1):
            if not is_suited(start) or start // 9 != i // 9 or (start + 2) // 9 != i // 9:
                continue
            span = (start, start + 1, start + 2)
            missing = sum(1 for k in span if c[k] == 0)
            if missing > jokers:
                continue
            values = list(c)
            for k in span:
                if values[k]:
                    values[k] -= 1
            if _melds(tuple(values), groups - 1, jokers - missing):
                return True
    return False


def win_standard(counts, meld_groups=0):
    need_groups = 4 - meld_groups
    real, jokers = strip_joker(counts)
    if sum(real) + jokers != need_groups * 3 + 2:
        return False
    for i, n in enumerate(real):
        for used, need in ((2, 0), (1, 1), (0, 2)):
            if n < used or need > jokers:
                continue
            if _melds(_remove(real, i, used), need_groups, jokers - need):
                return True
    if jokers >= 2 and _melds(real, need_groups, jokers - 2):
        return True
    return False


def seven_pairs(counts):
    """返回豪华组数；不是七对返回 None。"""
    real, jokers = strip_joker(counts)
    if sum(real) + jokers != 14:
        return None
    singles = sum(n & 1 for n in real)
    if singles > jokers or (jokers - singles) % 2:
        return None
    pairs = sum(n // 2 for n in real) + singles + (jokers - singles) // 2
    if pairs != 7:
        return None
    quads = sum(n // 4 for n in real)
    if singles == 0:
        quads += jokers // 4
    return min(quads, 3) + 1 if quads else 1


def _wins_any(counts, meld_groups):
    return win_standard(counts, meld_groups) or (
        meld_groups == 0 and seven_pairs(counts) is not None
    )


def baotou(counts13, meld_groups=0):
    """13 张摸任意合法牌均胡。"""
    for i in range(NSUITS):
        if counts13[i] >= 4:
            continue
        values = list(counts13)
        values[i] += 1
        if not _wins_any(tuple(values), meld_groups):
            return False
    return True


def evaluate(counts13, draw_idx, chain_count=0, piao=0, meld_groups=0,
             must_kaoxiang=False, gang_open=False):
    """番数计算。

    2026-09-24 correctness 修正（数据确认后修改，不是凭注释推断——见
    ``docs/experiments/OFFLINE_REPORT.md``「剩余差距」十一.6 节）：此前认为
    "``gang_open`` 已经计入 ``chain_count``，不应再独立 ×2"，依据是一条未经
    真实数据验证的"官网规则核验"说法。用全量语料 `round_ended` 的真实
    `detail`/`fan` 字段核对：单独出现"杠开"标签（不带任何财飘/连杠类标签，
    即 chain_count 为 0 的情形）时，真实 fan 依然是分支番×2——205 个真实
    样本（142 例 (平胡,杠开)=fan2、63 例 (平胡,杠开,爆头)=fan4），**零反例**。
    说明"杠开"在服务端是一个独立的番值来源，与 ``chain_count`` 是两套
    互不相关的机制，不存在"同一次杠被计两次"的问题——旧假设是错的。
    已改为无条件 ×2（不再依赖 ``chain_count`` 是否已经非零）。
    """
    values = list(counts13)
    values[draw_idx] += 1
    counts = tuple(values)
    standard = win_standard(counts, meld_groups)
    pairs = seven_pairs(counts) if meld_groups == 0 else None
    if not standard and pairs is None:
        return None
    is_baotou = baotou(counts13, meld_groups)
    if must_kaoxiang and counts[JOKER_IDX] and not is_baotou and not gang_open:
        return None
    details = []
    if pairs is not None:
        fan = BRANCH_FAN[pairs]
        details.append(BRANCH_NAME[pairs])
    else:
        fan = 1
        details.append("平胡")
    if gang_open:
        fan *= 2
        details.append("杠开")
    if chain_count:
        fan *= 2 ** chain_count
        details.append("动作链x%d" % chain_count)
    if counts[JOKER_IDX] + piao == 4:
        fan *= 2
        details.append("4个白板")
    if is_baotou:
        fan *= 2
        details.append("爆头")
    return {"hu": True, "baotou": is_baotou, "fan": fan,
            "detail": details, "quads": pairs or 0, "counts": counts}


def payout(fan, base=1, dealer=False):
    return base * fan * (24 if dealer else 10)


@lru_cache(maxsize=1 << 16)
def baotou_flex(counts13, meld_groups=0):
    """13 张全听（摸任意合法牌均胡）——手留白的爆头形态。"""
    return baotou(counts13, meld_groups)
