"""白板角色枚举：J 张白按「全熔面子求速 / 留作爆头弹性」逐方案评估取最优。

方案 keep：留 keep 张白不参与面子/搭子搜索（作将与爆头弹性），其余白照常熔搭。
枚举 keep ∈ {1, J}（keep=主评分的全速方案，由调用方并入取 max）。
方案值 = -向听×速度权重 + 留白溢价（沿用实战调校的 baotou_* 分层，
s8-s10 实验证伪档已剔除；进张项不参与——与旧爆头分层一致）。
"""
from functools import lru_cache

from .rules import evaluate
from .shanten import shanten
from .tiles import JOKER_IDX, NSUITS, TILE_INDEX, is_suited, rank


def _counts_without(counts, removed):
    values = list(counts)
    values[TILE_INDEX["白"]] -= removed
    return tuple(values)


def _counts_without(counts, removed):
    values = list(counts)
    values[TILE_INDEX["白"]] -= removed
    return tuple(values)


@lru_cache(maxsize=1 << 16)
def hand_all_wait(c, meld_groups=0):
    """平台口径爆头：13 张等效手里留白，摸任意合法牌均胡（34 张逐一判定）。
    c 为面子外的等效手牌（13-3m 张），副露组数由 meld_groups 传入。"""
    for name, idx in TILE_INDEX.items():
        if c[idx] >= 4:
            continue
        try:
            if not evaluate(c, idx, meld_groups=meld_groups):
                return False
        except (KeyError, TypeError, ValueError):
            return False
    return True


@lru_cache(maxsize=1 << 16)
def hand_wait_cover(c, meld_groups=0):
    """听口覆盖率：34 种牌里摸到能胡的有几张，返回 (命中, 参与判定的总数)。

    与 hand_all_wait 同口径（同样跳过 c[idx] >= 4 的牌），命中 == 总数 就是爆头，
    所以这是 hand_all_wait 的连续化版本，不是另一套判据。
    """
    hit = total = 0
    for _name, idx in TILE_INDEX.items():
        if c[idx] >= 4:
            continue
        total += 1
        try:
            if evaluate(c, idx, meld_groups=meld_groups):
                hit += 1
        except (KeyError, TypeError, ValueError):
            continue
    return hit, total


@lru_cache(maxsize=1 << 17)
def _best_plan(c, meld_groups, std_current, w_speed, dealer_hint,
               all_wait, no_slow, slow1, slow1_dealer, gap_base, gap_step,
               wait_power):
    best = None
    jokers = c[TILE_INDEX["白"]]
    for keep in sorted({1, jokers}):
        values = list(c)
        values[TILE_INDEX["白"]] -= keep
        s_active = shanten(tuple(values), meld_groups)
        gap = s_active - std_current
        if s_active == 0:
            # 2026-09-24：原式是 `s_active == 0 and hand_all_wait(...)` → 9000，
            # 否则落进 gap==0 档的 1500。也就是**听口 33 张和听口 3 张拿一样的分**，
            # 只有凑满 34 张才跳到 9000——全有全无，中间没有任何梯度。
            #
            # 两条早就量到、但一直没连起来的实测正好卡在这个断崖上：
            #   * baotou_all_wait=9000 只在 14/3383 = 0.41% 的候选里触发过（近乎死权重）
            #   * 吃碰后听口：对手 14.13 → 30.46，我们 14.11 → 22.82
            # 对手把听口推到 30/34、我们停在 23/34，而在评分里这两者差别为零，
            # 没有任何力量推着往 34 走。这就是 tools/joker_playbook.py 里
            # 「2张财神 + 门清」那一格 歪比巴卜 80% vs 我们 2% 的机制——纯手牌构造，
            # 不靠吃碰，只靠把听口捏宽。
            #
            # 改成按覆盖率在 [no_slow, all_wait] 之间插值：命中==总数 时**精确等于**
            # 原来的 all_wait，命中 0 时**精确等于**原来的 no_slow。数值范围一点没变，
            # 只是把断崖改成斜坡。wait_power 取 2（凸）：低覆盖率时几乎维持现状，
            # 只在接近满听时才明显加力，避免把"随便一个宽听"也抬成大牌。
            # 回滚：把 baotou_wait_power 设成一个极大值即可退化回全有全无。
            hit, total = hand_wait_cover(c, meld_groups)
            if total and hit == total:
                premium = all_wait
            else:
                frac = (hit / float(total)) if total else 0.0
                premium = no_slow + (all_wait - no_slow) * (frac ** wait_power)
        elif gap == 0:
            premium = no_slow
        elif gap == 1:
            premium = slow1_dealer if (dealer_hint or jokers >= 2) else slow1
        else:
            # 2026-09-22 反推16局真实"荣耀回放"里的胡大牌记录：赢家在拿到
            # 白之后, 常态性容忍 gap(=留白比全速慢的步数) 到 2~4 步、
            # 极端到 6 步仍不熔牌求速(110个持白回合里 gap>=2 占53%)。旧版
            # gap>=2 一律 premium=0, 导致 -gap*w_speed 的速度惩罚(每步
            # 1万分)必然压垮任何爆头溢价(旧封顶9000)——这不是阈值/剪枝
            # 的问题, 是分层公式本身缺失 gap>=2 这一段。按 gap 线性外推,
            # 让 gap=2 起大致追平速度惩罚, 之后每多一步再小幅衰减(体现
            # 越赌越远确定性越低)。若实战净分转差, 把 gap_base 调回 0
            # 即可完整回滚到旧的"gap>=2 直接放弃"口径。
            premium = gap_base + (gap - 2) * gap_step
        value = -s_active * w_speed + premium
        if best is None or value > best:
            best = value
    return best


def joker_plan_value(counts, meld_groups, std_current, w_speed, rules=None, weights=None):
    """留白最优方案的 EV（-向听×速度权重 + 留白溢价）。
    与主评分同尺度，调用方与全速方案取 max。无白或链中返回 None。"""
    jokers = counts[TILE_INDEX["白"]]
    if not jokers or (rules or {}).get("chain_active"):
        return None
    weights = weights or {}
    # 剪枝：全留白比全速慢超过 slack 步时，任何 keep 方案都追不回速度差。
    # 2026-09-22 反推16局真实爆头大牌回放：赢家持白回合里 gap 最深到过
    # 6步，默认 slack 提到 6 以覆盖观测到的真实分布上限（原为 1，本次
    # 会话内先松到 2，两次都不够）。若实战净分转差，调回 1 即可回滚。
    slack = weights.get("baotou_hold_slack", 6)
    hold = shanten(_counts_without(counts, jokers), meld_groups)
    if hold > std_current + slack and hold > 0:
        return None
    return _best_plan(tuple(counts), meld_groups, std_current, w_speed,
                      # dealer_slow1_enabled（默认 1 = 现行为）：0 = 庄家不使用 slow1_dealer 溢价（消融用）
                      bool((rules or {}).get("dealer_hint")) and bool(weights.get("dealer_slow1_enabled", 1)),
                      weights.get("baotou_all_wait", 3000),
                      weights.get("baotou_no_slow", 1500),
                      weights.get("baotou_slow1", 3000),
                      weights.get("baotou_slow1_dealer", 9000),
                      weights.get("baotou_gap_base", 20000),
                      weights.get("baotou_gap_step", 9000),
                      weights.get("baotou_wait_power", 2))


# ---------------------------------------------------------------------------
# 2026-09-25 收敛任务 S7：单财神爆头距离——与 tools/mining_common.py::to_baotou
# 完全同构的移植（那边是离线分析专用的只读 stdlib 实现，这里搬进生产代码供
# mj/ev.py 的共享 helper 调用；两份实现刻意保持算法一致，不做任何改动，
# 修改一边需要同步检查另一边，见 docs/experiments/OFFLINE_REPORT.md
# 「五问结论」S7）。
#
# 去掉 1 张财神（留作将）后，其余牌全部做成完整面子还差几步；没有财神时
# 返回 None（爆头结构不成立）。
# ---------------------------------------------------------------------------

def _btd_remove(c, i, n=1):
    v = list(c)
    v[i] -= n
    return tuple(v)


@lru_cache(maxsize=1 << 20)
def _btd_search(c, wild, i, melds, taatsu, need):
    while i < NSUITS and c[i] == 0:
        i += 1
    if i >= NSUITS:
        melds = min(melds, need)
        remaining = need - melds
        add_melds = min(wild // 3, remaining)
        melds += add_melds
        wild_left = wild - add_melds * 3
        remaining = need - melds
        taatsu = min(taatsu + wild_left // 2, remaining)
        return 2 * (need - melds) - taatsu
    n = c[i]
    best = _btd_search(_btd_remove(c, i, n), wild, i, melds, taatsu, need)
    if n >= 3:
        best = min(best, _btd_search(_btd_remove(c, i, 3), wild, i, melds + 1, taatsu, need))
    if n >= 2:
        best = min(best, _btd_search(_btd_remove(c, i, 2), wild, i, melds, taatsu + 1, need))
        if wild >= 1:
            best = min(best, _btd_search(_btd_remove(c, i, 2), wild - 1, i, melds + 1, taatsu, need))
    elif n == 1 and wild >= 1:
        best = min(best, _btd_search(_btd_remove(c, i, 1), wild - 1, i, melds, taatsu + 1, need))
    if wild >= 2:
        best = min(best, _btd_search(_btd_remove(c, i, 1), wild - 2, i, melds + 1, taatsu, need))
    if is_suited(i):
        r = rank(i)
        if r <= 7:
            need_w = (c[i + 1] == 0) + (c[i + 2] == 0)
            if need_w <= wild:
                v = list(c)
                v[i] -= 1
                if c[i + 1]:
                    v[i + 1] -= 1
                if c[i + 2]:
                    v[i + 2] -= 1
                best = min(best, _btd_search(tuple(v), wild - need_w, i, melds + 1, taatsu, need))
        if r <= 8:
            need_w = 0 if c[i + 1] else 1
            if need_w <= wild:
                v = list(c)
                v[i] -= 1
                if c[i + 1]:
                    v[i + 1] -= 1
                best = min(best, _btd_search(tuple(v), wild - need_w, i, melds, taatsu + 1, need))
        if r <= 7:
            need_w = 0 if c[i + 2] else 1
            if need_w <= wild:
                v = list(c)
                v[i] -= 1
                if c[i + 2]:
                    v[i + 2] -= 1
                best = min(best, _btd_search(tuple(v), wild - need_w, i, melds, taatsu + 1, need))
    return best


def to_baotou_distance(counts13, meld_groups=0):
    jokers = counts13[JOKER_IDX]
    if not jokers:
        return None
    real = list(counts13)
    real[JOKER_IDX] = 0
    wild = jokers - 1
    need = 4 - meld_groups
    if need <= 0:
        return 0
    return _btd_search(tuple(real), wild, 0, 0, 0, need)


def menqing_baotou_bonus(counts, meld_groups, current_shanten, weights=None):
    """S7（默认关闭 ``rule_menqing_baotou_enabled``；2026-09-25 第二次修订，
    从"仅财神==1、向听==1"扩展为按财神数分格、覆盖向听 1~2 两档，见
    docs/experiments/OFFLINE_REPORT.md「五问结论」S7 追加）：**门清**
    （``meld_groups==0``）、候选弃牌之后向听在 {1,2} 这两档时，同向听候选
    之间优先选能把 ``to_baotou_distance`` 压得更小的那张——只在
    门清+向听1~2 这个范围生效，不是全局爆头权重，也不管有副露的手（有
    副露的手不在这次验证范围内，行为不变）。

    数据依据（用 ``s3_baotou.csv``（``shanten_before``/``jokers_before``/
    ``melds``）联表 ``decisions.csv`` 的真实 ``to_baotou`` 值；完整数字见
    docs/experiments/OFFLINE_REPORT.md「五问结论」S7 追加）：门清、向听
    1~2、按财神数（1 / 2+）分格，弃完之后达到该向听档能达到的最好
    ``to_baotou`` 档位（向听1→<=2，向听2→<=3，语料里逐档实测出来的，不是
    假设）的比例——

        财神数  top执行率  ours执行率  bottom执行率  Δ(y_score) 95%CI          top10一致
        1       52.2%     48.8%      47.6%        +2.10[1.60,2.55]         10/10
        2+      80.3%     75.9%      74.4%         +3.03[1.52,4.26]         6/10

    两格都通过 CI 下界>0 + 70/30 房间留出复现的门槛（财神==1 那格
    train 2.12/test 2.08；财神>=2 那格 train 2.31/test 4.37），财神==1
    那格的跨人一致性（10/10）比财神>=2 那格（6/10，两个负方向的人样本都
    很小）扎实得多，但两格都保留（都通过了预先约定的统计门槛，不因为
    一致性数字好看程度不同而挑一个）。

    **不做的事**：手上 1 张财神、已经听牌（shanten==0）但不是爆头形的
    局面——这一格离线核实几乎是全体玩家的天花板行为（top/ours/bottom
    转爆头率都 >=99%，n_people 太薄无法算方向），不成立，没有对应实现，
    如实标注为"不显著"，不强行做成规则；有副露（meld_groups>=1）的手
    也不在本条覆盖范围内。

    只在 (门清, 候选弃牌后向听 in {1,2}, 财神>=1) 这个范围生效，刻意避免
    ``baotou_slow1`` 那次全局调高爆头权重导致实战胜率从 28.4% 掉到 24.1%
    的教训重演。返回一个可以直接加到主评分上的分值（负的
    to_baotou_distance × 权重），不满足条件返回 0。
    """
    weights = weights or {}
    if not weights.get("rule_menqing_baotou_enabled", 0):
        return 0
    if meld_groups != 0:
        return 0
    if current_shanten not in (1, 2):
        return 0
    if counts[JOKER_IDX] < 1:
        return 0
    distance = to_baotou_distance(counts, meld_groups)
    if distance is None:
        return 0
    return -distance * weights.get("menqing_baotou_weight", 200)
