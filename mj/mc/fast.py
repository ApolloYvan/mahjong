"""阶段二 §1：MC 推演要用的"离胡距离"/"是否胡了"快速查询。

设计取舍（如实记录，不是照抄任务书字面）：任务书原话是"查表判胡与离胡
距离...每门 9 位计数为 key...惰性缓存"。``mj/shanten.py`` 已经是这样一套
实现——``shanten()``/``pair_shanten()`` 都是纯计数向量上的组合搜索 +
``functools.lru_cache``（分别 1<<22、1<<19 项），是**全场生产 bot 实时
决策路径**在用的同一份代码，已经被 3 秒出牌预算逼着做过性能审计（见
``mj/shanten.py::route_shanten`` 的 2026-09-22 注释：0 财神手牌 ~10μs 量级，
2 财神手牌最坏 ~11ms）。再写一套独立的"每门 9 位分府计数"引擎，等于对同
一个组合搜索问题维护两份实现——`tools/mc_check.py equiv` 要求的"跟
mj.rules/mj.shanten 100 万手零不一致"在这种设计下反而是**同一份代码跟自己
比**，测不出真实分歧，且平白多一个可能跟生产代码口径漂移的地方。

所以这个模块**不是**独立的组合搜索实现，是 ``mj.shanten``/``mj.rules`` 之上
面向 MC 场景的薄封装：只加"标准三对二取更小"这一层 MC 特有的组合逻辑（生产
代码的 ``route_shanten`` 对七对路线有一个额外的"手上要有真实雏形"门槛，是
弃牌分层专用的启发式，不是纯距离，MC 需要的是没有门槛的纯 min，这里不能
直接复用 ``route_shanten``）。``tools/mc_check.py equiv`` 因此重点验证的是
``mj/mc/rollout.py``/``determinize.py`` 这两层真正新写的状态机代码跟
``mj.sim.engine`` 是否一致，不是这个文件本身（这个文件的正确性由
``mj.shanten``/``mj.rules`` 自身的既有测试和线上验证担保）。
"""
from ..rules import _wins_any, baotou
from ..shanten import pair_shanten, shanten
from ..tiles import JOKER_IDX


def hu_distance(counts13, meld_groups=0):
    """离胡距离：标准胡向听与七对向听取较小者（七对只在 meld_groups==0
    时参与，跟 ``mj.rules.seven_pairs``/``pair_shanten`` 的口径一致——
    副露过就不可能七对）。"""
    std = shanten(counts13, meld_groups)
    if meld_groups:
        return std
    return min(std, pair_shanten(counts13))


def is_hu(counts14, meld_groups=0):
    """``counts14``：摸牌/吃碰杠补牌之后的 14 张（或副露占位后手牌+这张）
    完整计数向量。直接复用 ``mj.rules._wins_any``（跟 ``mj.sim.engine``/
    ``mj.rules.evaluate`` 内部判胡是同一个函数，不是另起一套）。"""
    return bool(_wins_any(counts14, meld_groups))


def is_baotou(counts13, meld_groups=0):
    return bool(baotou(counts13, meld_groups))


def has_joker(counts13):
    return counts13[JOKER_IDX] > 0
