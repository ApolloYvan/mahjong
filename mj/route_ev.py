"""统一账本 v2：速度 / 大牌 / 爆头 / 财神 / 财飘 / 胡牌时机 / 吃碰的路线期望值。

2026-09-28 首版（``rule_route_ev_enabled``）：门清 + 持财神时 4 条路线（A 标准 /
B 标准爆头 / C 七对 / D 七对·爆头）的期望账，只在 C/D 明显更优时改写弃牌、
否决吃碰。

2026-09-28 追加（docs/IMPL_UNIFIED_EV_V2.md，取代 docs/IMPL_ROUTE_EV_LOSS.md）：
在同一份账本上扩展 P1~P5，每项独立开关、默认关闭：
  P1 rev_loss_enabled      账本加入"别家先自摸"的付出项，改写/否决判据改为差值门槛。
  P2 rev_speed_enabled     全部手牌的一向听/听牌前瞻（不限门清/财神），只在同向听
                           tier 内换牌，不改变向听。
  P3 rev_override_b_enabled 门清持财神范围内，最优路线为 B（标准爆头）时也允许
                           改写/否决；B 路线距离 2 用前瞻近似替代粗糙的 std_rate。
  P4 rev_piao_enabled / rev_decline_enabled  财飘续飘、非爆头弃胡转爆头改用按账
                           决策，替代原有固定阈值（``mj/hu_strategy.py``）。
  P5 rev_claim_enabled     吃碰前整体比较"吃碰前" vs "吃碰后打最优一张"的账。

2026-09-28 再追加（docs/IMPL_TABLE_EV.md，统一账本 v3，本轮只做 T0/T1/T3）：
  T1 rev_opp_pay_enabled     付出项从"庄/闲付出平均数"换成按"谁是庄"加权的
                             真实付分（闲家时庄胡付 8×番、另一闲胡只付 1×番，
                             8 倍之差原先被平均数抹掉），fan_by_role 取
                             tools/hazard_fit.py 产出的 models/hazard_table.json。
  T3 rev_dealer_value_enabled 自己胡牌额外计入 V(局号)（胡者接庄，
                             tools/dealer_value.py 产出的 models/dealer_value.json，
                             第 8 局起恒为 0）。只进入按账分支，旧阈值逻辑不受影响。
两个开关默认关闭；缺表/缺局号一律等价于关闭，不报错、不影响默认行为。

2026-09-28 再追加（docs/IMPL_ROUTE_CAL.md，统一账本 v4-C1，路线概率校准）：
  C1 rev_route_cal_enabled   "走完的概率"这一侧原先靠理论估算（p 按当前状态
                             固定不变、七对摸财神不算推进），route_ev_check
                             的校准显示七对系被系统性低估。改用
                             tools/route_sim_fit.py 离线模拟"坚持走某条路线"
                             得到的完成曲线 F(k)（models/route_cal.json）替换
                             理论 DP；查不到/缺表一律回落理论估算。

所有新增逻辑都吞掉异常回落原逻辑；开关全关时行为与本文件首版完全一致。
"""
import json
import os
import time

from .discard_features import baotou_waits
from .joker_ev import to_baotou_distance
from .shanten import pair_shanten, pair_ukeire, shanten, ukeire
from .tiles import JOKER, JOKER_IDX, NSUITS, to_counts

CAL_PATH = "models/rollout_cal.json"
_CAL_CACHE = None


def _load_cal():
    global _CAL_CACHE
    if _CAL_CACHE is not None:
        return _CAL_CACHE
    try:
        with open(CAL_PATH, encoding="utf-8") as source:
            _CAL_CACHE = json.load(source)
    except (OSError, ValueError, TypeError):
        _CAL_CACHE = {}
    return _CAL_CACHE


def _live_count(indices, visible):
    """活牌张数：indices 里每种牌还剩几张没见过，加总。visible=None 时假设全活
    （调用方未提供可见张数，退化为乐观估计，不影响开关关闭时的任何行为）。"""
    if not indices:
        return 0.0
    if visible is None:
        return float(len(indices) * 4)
    return float(sum(max(0, 4 - visible[i]) for i in indices))


def _estimate_turn0(wall):
    """本家本局已摸次数的粗估：由墙尾消耗反推（发牌 13x4+1=53 张，
    之后每 4 张对应全桌一轮），仅用于给 hazard 表选一个索引，不要求精确。"""
    drawn_total = max(0, 136 - 53 - wall)
    return drawn_total // 4


def _survival_series(turn0, n_draws, weights):
    """每一次本家摸牌之前，别家 3 家都没有先自摸的概率。"""
    cal = _load_cal()
    hazard = cal.get("hazard") or {}
    default = weights.get("rev_hazard_default", 0.05)
    out = []
    for k in range(n_draws):
        idx = str(min(turn0 + k + 1, 19))
        h = hazard.get(idx, default)
        out.append((1.0 - h) ** 3)
    return out


def _single_step_survive(wall, weights):
    """本家下一次摸牌之前，别家都没有先自摸的概率（P4 用于单步飘/转爆头决策）。"""
    turn0 = _estimate_turn0(wall)
    series = _survival_series(turn0, 1, weights)
    return series[0] if series else 1.0


# --------------------------------------------------------------------------- T1
# 分对手付分（开关 rev_opp_pay_enabled，docs/IMPL_TABLE_EV.md）：现有账本把
# "被别人先胡"的付出用平均数（rev_loss_dealer/idle）近似，而闲家时庄家胡我
# 付 8×番、另一闲家胡我只付 1×番，两者差 8 倍，被平均数抹掉了。本轮三家的
# 自摸概率 h 仍取 rollout_cal 的同一个值（动态值是下一轮 T2 的范围），故三
# 家先胡的概率相等，加权平均付分只取决于"谁是庄"——公式见 _opp_pay_weighted。
# ---------------------------------------------------------------------------
HAZARD_TABLE_PATH = "models/hazard_table.json"
_HAZARD_TABLE_CACHE = None


def _load_hazard_table():
    global _HAZARD_TABLE_CACHE
    if _HAZARD_TABLE_CACHE is not None:
        return _HAZARD_TABLE_CACHE
    try:
        with open(HAZARD_TABLE_PATH, encoding="utf-8") as source:
            _HAZARD_TABLE_CACHE = json.load(source)
    except (OSError, ValueError, TypeError):
        _HAZARD_TABLE_CACHE = {}
    return _HAZARD_TABLE_CACHE


def _fan_by_role(role, weights):
    table = _load_hazard_table()
    fan_map = table.get("fan_by_role") if isinstance(table.get("fan_by_role"), dict) else {}
    val = fan_map.get(role)
    return float(val) if isinstance(val, (int, float)) else weights.get("rev_fan_default", 1.3)


def _opp_pay_weighted(ctx, weights):
    """付出的加权平均：我是庄时三个对手都是闲家（我付 8xfan_idle）；我不是庄时
    3 家里恰好 1 个是庄家（我付 8xfan_dealer）、2 个是闲家（我付 1xfan_idle）——
    本轮 h 三家相同，先胡概率均等，直接按人数取平均。``tools/hazard_fit.py``
    产出的 ``models/hazard_table.json`` 缺失时用 rev_fan_default（默认 1.3）。"""
    fan_dealer = _fan_by_role("dealer", weights)
    fan_idle = _fan_by_role("idle", weights)
    if bool((ctx or {}).get("dealer")):
        return 8.0 * fan_idle
    return (8.0 * fan_dealer + 1.0 * fan_idle + 1.0 * fan_idle) / 3.0


# --------------------------------------------------------------------------- T3
# 庄位价值（开关 rev_dealer_value_enabled，docs/IMPL_TABLE_EV.md）：胡者接庄，
# 自己每次胡牌都额外值 V(局号)（``tools/dealer_value.py`` 产出，第 8 局起
# 恒为 0）。缺局号或缺表 => V=0，等价于关闭，不影响任何默认行为。
# ---------------------------------------------------------------------------
DEALER_VALUE_PATH = "models/dealer_value.json"
_DEALER_VALUE_CACHE = None


def _load_dealer_value():
    global _DEALER_VALUE_CACHE
    if _DEALER_VALUE_CACHE is not None:
        return _DEALER_VALUE_CACHE
    try:
        with open(DEALER_VALUE_PATH, encoding="utf-8") as source:
            _DEALER_VALUE_CACHE = json.load(source)
    except (OSError, ValueError, TypeError):
        _DEALER_VALUE_CACHE = {}
    return _DEALER_VALUE_CACHE


def dealer_value(round_no, weights):
    """自己这一刻胡牌额外值多少（因为胡者接庄）。round_no 缺失、表缺失/损坏、
    或该局号没有值 => 0.0（等价于关闭，不影响任何默认行为）。"""
    if not weights.get("rev_dealer_value_enabled", 0):
        return 0.0
    if round_no is None:
        return 0.0
    table = _load_dealer_value()
    values = table.get("V") if isinstance(table.get("V"), dict) else None
    if not values:
        return 0.0
    val = values.get(str(round_no), values.get(round_no))
    return float(val) if isinstance(val, (int, float)) else 0.0


# --------------------------------------------------------------------------- C1
# 路线概率校准（开关 rev_route_cal_enabled，docs/IMPL_ROUTE_CAL.md）：用
# tools/route_sim_fit.py 离线模拟"坚持走某条路线"得到的完成曲线 F(k)（k 步
# 内走完的概率）替换理论 DP 里"p 固定不变"的近似。查不到/表缺失/开关关闭
# 一律回落 _win_and_lost，行为与关闭完全一致。
# ---------------------------------------------------------------------------
ROUTE_CAL_PATH = "models/route_cal.json"
_ROUTE_CAL_CACHE = None


def _load_route_cal():
    global _ROUTE_CAL_CACHE
    if _ROUTE_CAL_CACHE is not None:
        return _ROUTE_CAL_CACHE
    try:
        with open(ROUTE_CAL_PATH, encoding="utf-8") as source:
            _ROUTE_CAL_CACHE = json.load(source)
    except (OSError, ValueError, TypeError):
        _ROUTE_CAL_CACHE = {}
    return _ROUTE_CAL_CACHE


def route_cal_live_bucket(n):
    """活牌分档：0-2/3-5/6-8/9-12/13-16/17+，与 tools/route_sim_fit.py 聚合
    表时用的分档一致（改一边要同步改另一边，否则键对不上）。"""
    if n <= 2:
        return 0
    if n <= 5:
        return 1
    if n <= 8:
        return 2
    if n <= 12:
        return 3
    if n <= 16:
        return 4
    return 5


def route_cal_key(route, d, live, jokers, meld_groups):
    return "%s:%d:%d:%d:%d" % (route, d, route_cal_live_bucket(live), min(jokers, 2), min(meld_groups, 2))


def route_cal_backoff_keys(route, d, live, jokers, meld_groups):
    """在线查表的回退链：完整键 -> 丢副露 -> 丢副露+财神 -> 丢副露+财神+活牌
    分档，与 tools/route_sim_fit.py 离线聚合时"样本 <30 的格子按 副露 ->
    财神 -> 活牌分档 顺序逐级回退"用的是同一个顺序、同一组键——那边按这个
    顺序把每一级"有效样本量够"的聚合结果各自写进表里（用 ``*`` 代表被丢弃
    的维度），这里按同一顺序依次查，第一个命中就用，不在线重新聚合。"""
    live_b = route_cal_live_bucket(live)
    j = min(jokers, 2)
    m = min(meld_groups, 2)
    return [
        "%s:%d:%d:%d:%d" % (route, d, live_b, j, m),
        "%s:%d:%d:%d:*" % (route, d, live_b, j),
        "%s:%d:%d:*:*" % (route, d, live_b),
        "%s:%d:*:*:*" % (route, d),
    ]


def route_cal_curve(route, d, live, jokers, meld_groups, weights):
    """查表得到完成曲线 F（长度 18，F[k-1] = k 步内走完的概率）。按
    ``route_cal_backoff_keys`` 的顺序依次查，命中最细的一级就返回；开关
    关闭、d 超出 0..4、表缺失/损坏、或回退链上所有键都查不到，一律返回
    None——调用方据此回落理论 DP（``_win_and_lost``）。回退链固定 4 级、
    每级一次字典查找，保持在线查表路径零额外开销（不做任何运行时聚合）。"""
    if not weights.get("rev_route_cal_enabled", 0):
        return None
    if d is None or d < 0 or d > 4:
        return None
    table = _load_route_cal()
    cells = table.get("cells")
    if not isinstance(cells, dict) or not cells:
        return None
    live_value = live if live is not None else 999
    for key in route_cal_backoff_keys(route, d, live_value, jokers, meld_groups):
        cell = cells.get(key)
        if isinstance(cell, dict) and isinstance(cell.get("F"), list) and cell["F"]:
            return cell["F"]
    return None


def _route_cal_win_and_lost(curve, n_draws, survive, pay_per_step=None):
    """用完成曲线 F(k) 替换 DP：f(k)=F(k)-F(k-1)，S(k)=前 k 步 survive 连乘，
    win=Σf(k)S(k)，lost=Σ(1-F(k-1))S(k-1)(1-s_k)，lost_pay 同理按付分加权。
    只累加到 n_draws 为止；win+lost+剩余滞留质量=1（与 ``_win_and_lost`` 同一
    套守恒关系，纯代数展开可证，测试里用具体数字核对）。"""
    win = 0.0
    lost = 0.0
    lost_pay = 0.0
    s_cum = 1.0     # 前 i 步 survive 连乘（不含第 i+1 步）
    prev_f = 0.0    # F(i)
    last_curve = curve[-1] if curve else 0.0
    for i in range(n_draws):
        s_i = survive[i] if i < len(survive) else 1.0
        pay = pay_per_step[i] if pay_per_step and i < len(pay_per_step) else 0.0
        cur_f = curve[i] if i < len(curve) else last_curve
        not_done = max(0.0, 1.0 - prev_f)
        exit_mass = not_done * s_cum * (1.0 - s_i)
        lost += exit_mass
        lost_pay += exit_mass * pay
        step_complete = max(0.0, cur_f - prev_f)
        win += step_complete * s_cum * s_i
        s_cum *= s_i
        prev_f = cur_f
    return win, lost, lost_pay


def _win_and_lost(d, p, q, n_draws, survive, pay_per_step=None):
    """d 步内走完、且别家没有先自摸的概率(win)，"别家先自摸"的概率质量(lost)，
    以及按每步付分加权累加的 lost_pay（T1，``rev_opp_pay_enabled``）。
    p：距离 d>=1 时每次摸牌前进一步的概率；q：距离 0 时每次摸牌胡牌的概率。
    ``pay_per_step``：与 ``survive`` 等长的每步付分（不提供则 lost_pay 恒为 0，
    调用方据此判断是否使用 lost_pay，不使用时零开销）。
    纯确定性 DP，状态数 = n_draws x (d+1)，量级极小。win + lost + 剩余滞留质量 = 1。"""
    if d is None or d < 0:
        return 0.0, 0.0, 0.0
    dist = {d: 1.0}
    win = 0.0
    lost = 0.0
    lost_pay = 0.0
    for step in range(n_draws):
        s = survive[step] if step < len(survive) else 1.0
        pay = pay_per_step[step] if pay_per_step and step < len(pay_per_step) else 0.0
        nxt = {}
        for remaining, prob in dist.items():
            exit_mass = prob * (1.0 - s)
            lost += exit_mass
            lost_pay += exit_mass * pay
            prob *= s
            if prob <= 0.0:
                continue
            if remaining == 0:
                win += prob * q
                stay = prob * (1.0 - q)
                if stay:
                    nxt[0] = nxt.get(0, 0.0) + stay
            else:
                adv = prob * p
                stay = prob * (1.0 - p)
                if adv:
                    nxt[remaining - 1] = nxt.get(remaining - 1, 0.0) + adv
                if stay:
                    nxt[remaining] = nxt.get(remaining, 0.0) + stay
        dist = nxt
    return win, lost, lost_pay


def _win_probability(d, p, q, n_draws, survive):
    win, _lost, _lost_pay = _win_and_lost(d, p, q, n_draws, survive)
    return win


def _pair_baotou_distance(counts13):
    """D 路线（七对·爆头）距离：去掉 1 张财神（留作可自由配对的将）后，
    剩余牌用真实对子 + 财神百搭凑够 6 个对子还差几个。

    与 ``mj.shanten._pair_score``（``pair_shanten`` 的内核）同构，只是财神
    数减 1——多出的财神（``flex``）既可以把某个单张配成对，也可以互相配成
    一对，与 2 张以上财神时 ``pair_shanten`` 的处理方式完全一致。手上不足
    1 张财神时该路线不成立，返回 None。"""
    real = list(counts13)
    jokers = real[JOKER_IDX]
    if jokers < 1:
        return None
    real[JOKER_IDX] = 0
    flex = jokers - 1
    pairs = sum(n // 2 for n in real)
    singles = sum(n % 2 for n in real)
    used = min(singles, flex)
    pairs += used
    flex -= used
    pairs += flex // 2
    return max(0, 6 - pairs)


def _pair_baotou_ukeire(counts13):
    """D 路线的前进牌：摸入后 ``_pair_baotou_distance`` 下降的牌（含财神）。"""
    current = _pair_baotou_distance(counts13)
    if current is None:
        return []
    out = []
    for i in range(NSUITS):
        if i == JOKER_IDX or counts13[i] >= 4:
            continue
        values = list(counts13)
        values[i] += 1
        if _pair_baotou_distance(tuple(values)) < current:
            out.append(i)
    return out


def _advance_tiles(counts13, meld_groups, dist_fn):
    """通用"前进牌"枚举：摸入后 dist_fn(counts, meld_groups) 严格下降的牌种下标。"""
    current = dist_fn(counts13, meld_groups)
    if current is None:
        return []
    out = []
    for i in range(NSUITS):
        if counts13[i] >= 4:
            continue
        values = list(counts13)
        values[i] += 1
        nxt = dist_fn(tuple(values), meld_groups)
        if nxt is not None and nxt < current:
            out.append(i)
    return out


def _universe(counts13, meld_groups, visible):
    """未见牌数 U = 136 - 已见。visible（34 维，含自己手牌）提供时直接用；
    否则退化为「只有自己手牌 + 自己副露可见」的粗估（visible=None 的调用方
    通常是测试或没有追踪牌河的场景，只影响概率的绝对值，不影响相对大小）。"""
    if visible is not None:
        return float(136 - sum(visible))
    return float(136 - sum(counts13) - 3 * meld_groups)


def _routes(counts13, meld_groups, visible, routes, weights=None):
    weights = weights or {}
    std_d = shanten(counts13, meld_groups)
    std_waits = ukeire(counts13, meld_groups)
    std_rate = _live_count(std_waits, visible)
    out = []
    if "A" in routes:
        out.append(("A", std_d, std_rate, std_rate, 1))
    if "B" in routes:
        bt_d = to_baotou_distance(counts13, meld_groups)
        if bt_d is not None:
            if bt_d == 1:
                bt_rate = _live_count(baotou_waits(counts13, meld_groups), visible)
            elif bt_d == 2 and weights.get("rev_override_b_enabled", 0):
                # P3：距离 2 用真实"能让 to_baotou_distance 下降"的活牌数，
                # 替代粗糙的 std_rate（标准向听的进张速率，与爆头路线的推进
                # 速率并不是一回事，只是历史上没算过 2 步时的专属进张）。
                advancing = _advance_tiles(counts13, meld_groups, to_baotou_distance)
                adv_rate = _live_count(advancing, visible)
                bt_rate = adv_rate if adv_rate else std_rate
            elif bt_d >= 2:
                bt_rate = std_rate
            else:
                bt_rate = None    # d==0：进张速率用不到，胜率走「摸啥都胡」分支
            out.append(("B", bt_d, bt_rate, None, 2))
    if "C" in routes:
        pair_d = pair_shanten(counts13)
        pair_rate = _live_count(pair_ukeire(counts13), visible)
        out.append(("C", pair_d, pair_rate, pair_rate, 2))
    if "D" in routes:
        pbt_d = _pair_baotou_distance(counts13)
        if pbt_d is not None:
            pbt_rate = _live_count(_pair_baotou_ukeire(counts13), visible)
            out.append(("D", pbt_d, pbt_rate, None, 4))
    return out


def _pay_out_term(ctx, weights, dealer, single_step_pay=None):
    """T1：付出项单步权重（否则回落 P1 的 rev_loss_dealer/idle 平均数）。
    ``single_step_pay`` 为 None 时按 ``ctx`` 现算，供只想传 dealer 布尔值的
    调用方（如 ``mj.hu_strategy``）复用。"""
    if weights.get("rev_opp_pay_enabled", 0):
        return _opp_pay_weighted(ctx if ctx is not None else {"dealer": dealer}, weights)
    return weights.get("rev_loss_dealer" if dealer else "rev_loss_idle", 10.33 if dealer else 4.73)


def _piao_bonus(counts13, meld_groups, ctx, weights):
    """P4 财神保留：持 2+ 财神、已经听牌/爆头（B/D 路线 d==0）时，额外计入
    「飘一次」的期望增益（与 hu_strategy 里 P4 飘的账同一套公式，只取正值，
    不会为了鼓励飘而倒贴），让弃牌时倾向保留能飘的第二张财神。只在
    rev_piao_enabled 打开时生效，默认不介入。

    T3（``rev_dealer_value_enabled``）：当场胡与飘两侧都加上 V(局号)（胡者
    接庄的额外期望）；T1（``rev_opp_pay_enabled``）：付出项从平均数换成按
    「谁是庄」加权的付分。两者默认关闭时公式与首版逐一致。"""
    if not weights.get("rev_piao_enabled", 0):
        return 0.0
    if counts13[JOKER_IDX] < 2:
        return 0.0
    dealer = bool(ctx.get("dealer"))
    payout = weights.get("rev_payout_dealer", 24) if dealer else weights.get("rev_payout_idle", 10)
    wall = ctx.get("wall", 60)
    s = _single_step_survive(wall, weights)
    pay = _pay_out_term(ctx, weights, dealer)
    v = dealer_value(ctx.get("round_no"), weights)
    base_fan = 1.0
    direct_value = base_fan * payout + v
    piao_value = s * (base_fan * 2 * payout + v) - (1.0 - s) * pay
    return max(0.0, piao_value - direct_value)


def best_route(counts13, meld_groups, ctx, weights, visible=None, routes=("A", "B", "C", "D")):
    """对给定的 13 张口径手牌，返回 (最优路线期望值, 路线字母)。

    P1（``rev_loss_enabled``）：账本额外扣掉「别家先自摸」的付出项
    （``lost x rev_loss_dealer/idle``）；关闭时与首版完全一致
    （只有 win x fan x payout）。

    异常一律吞掉返回 (0.0, None)——任何一步算错都不能影响线上出牌，调用方
    据此原样回落到既有评分体。"""
    try:
        weights = weights or {}
        ctx = ctx or {}
        wall = ctx.get("wall", 60)
        dealer = bool(ctx.get("dealer"))
        n_draws = max(0, wall - 20) // 4 + 1
        turn0 = _estimate_turn0(wall)
        survive = _survival_series(turn0, n_draws, weights)
        universe = _universe(counts13, meld_groups, visible)
        if universe <= 0:
            return 0.0, None
        payout = weights.get("rev_payout_dealer", 24) if dealer else weights.get("rev_payout_idle", 10)
        loss_enabled = weights.get("rev_loss_enabled", 0)
        opp_pay_enabled = weights.get("rev_opp_pay_enabled", 0)
        loss_weight = weights.get("rev_loss_dealer" if dealer else "rev_loss_idle",
                                  10.33 if dealer else 4.73)
        pay_per_step = [_opp_pay_weighted(ctx, weights)] * n_draws if opp_pay_enabled else None
        v = dealer_value(ctx.get("round_no"), weights)
        jokers_ct = counts13[JOKER_IDX]
        best_val, best_letter = None, None
        for letter, d, p_rate, w_rate, fan in _routes(counts13, meld_groups, visible, routes, weights):
            p = (p_rate / universe) if p_rate else 0.0
            q = 1.0 if w_rate is None else min(1.0, w_rate / universe)
            live_for_lookup = p_rate if d and d >= 1 else w_rate
            curve = route_cal_curve(letter, d, live_for_lookup, jokers_ct, meld_groups, weights)
            if curve is not None:
                win, lost, lost_pay = _route_cal_win_and_lost(curve, n_draws, survive, pay_per_step)
            else:
                win, lost, lost_pay = _win_and_lost(d, p, q, n_draws, survive, pay_per_step)
            value = win * (fan * payout + v)
            if opp_pay_enabled:
                value -= lost_pay
            elif loss_enabled:
                value -= lost * loss_weight
            if letter in ("B", "D") and d == 0:
                value += _piao_bonus(counts13, meld_groups, ctx, weights)
            if best_val is None or value > best_val:
                best_val, best_letter = value, letter
        if best_val is None:
            return 0.0, None
        return best_val, best_letter
    except Exception:
        return 0.0, None


def route_win_probabilities(counts13, meld_groups, ctx, weights, visible=None, routes=("A", "B", "C", "D")):
    """与 ``best_route`` 同一套计算路径（含 C1 查表/理论 DP 的切换），但返回
    每条路线的胜率（``win``，不折算番/收入/付出）——供离线校准
    （``tools/route_cal_check.py``）比较"预测胡率 vs 实际胡率"，避免另写一份
    容易与 ``best_route`` 逐渐drift 的重复实现。异常吞掉返回 {}。"""
    try:
        weights = weights or {}
        ctx = ctx or {}
        wall = ctx.get("wall", 60)
        n_draws = max(0, wall - 20) // 4 + 1
        turn0 = _estimate_turn0(wall)
        survive = _survival_series(turn0, n_draws, weights)
        universe = _universe(counts13, meld_groups, visible)
        if universe <= 0:
            return {}
        jokers_ct = counts13[JOKER_IDX]
        out = {}
        for letter, d, p_rate, w_rate, fan in _routes(counts13, meld_groups, visible, routes, weights):
            p = (p_rate / universe) if p_rate else 0.0
            q = 1.0 if w_rate is None else min(1.0, w_rate / universe)
            live_for_lookup = p_rate if d and d >= 1 else w_rate
            curve = route_cal_curve(letter, d, live_for_lookup, jokers_ct, meld_groups, weights)
            if curve is not None:
                win, _lost, _lost_pay = _route_cal_win_and_lost(curve, n_draws, survive)
            else:
                win, _lost, _lost_pay = _win_and_lost(d, p, q, n_draws, survive)
            out[letter] = win
        return out
    except Exception:
        return {}


def _passes_margin(new_value, old_value, weights):
    """P1：改写/否决所需的门槛。``rev_loss_enabled`` 打开时改用差值门槛
    （新 - 旧 >= max(rev_margin_abs, rev_margin x |旧|)），避免账本里出现
    负值/小分母时旧的比例门槛失真；关闭时保留首版的比例门槛，行为不变。"""
    margin = weights.get("rev_margin", 0.10)
    if weights.get("rev_loss_enabled", 0):
        margin_abs = weights.get("rev_margin_abs", 0.5)
        return (new_value - old_value) >= max(margin_abs, margin * abs(old_value))
    # 严格大于：两边相等（比如都是 0）时不改写——否则按比例门槛 0 >= 0 会成立，等于随便换一张。
    return new_value > old_value and new_value >= old_value * (1.0 + margin)


def _veto_worse(before_value, after_value, weights):
    """否决所需的门槛（``_passes_margin`` 的对称版本，"变差"方向）。"""
    margin = weights.get("rev_margin", 0.10)
    if weights.get("rev_loss_enabled", 0):
        margin_abs = weights.get("rev_margin_abs", 0.5)
        return (before_value - after_value) >= max(margin_abs, margin * abs(before_value))
    return after_value < before_value * (1.0 - margin)


def applies_discard(tiles, meld_groups, chain_count, piao, rules, weights):
    """弃牌路口的适用范围：门清 + 手上有财神 + 不在财飘链上 + 不是抓打圈。"""
    if not weights.get("rule_route_ev_enabled", 0):
        return False
    if meld_groups != 0:
        return False
    if tiles.count(JOKER) < 1:
        return False
    if chain_count or piao:
        return False
    ctx = (rules or {}).get("_ctx") or {}
    if ctx.get("catch_play"):
        return False
    return True


def _pair_rich(counts13_list, meld_groups=0, weights=None):
    """便宜的预筛：候选里至少有一个 七对·爆头 距离 ≤2 或 七对 向听 ≤1，才值得算完整账。
    完整账里标准进张（shanten.ukeire）每个候选约 20 ms，14 个候选 200~450 ms（2026-09-28 实测），
    而对子不够的手七对系本来就不可能胜出——先筛掉，线上绝大多数持财神手零额外开销。

    P3（``rev_override_b_enabled``）打开时，B 路线（标准爆头）也参与改写，
    而标准爆头形通常"面子齐、对子少"，与七对系正相反——只按七对系富裕度
    预筛会把这些手直接筛掉，B 路线永远评估不到。故打开该开关时，
    ``to_baotou_distance <= 2`` 也算一种"值得算完整账"的信号。"""
    weights = weights or {}
    check_b = bool(weights.get("rev_override_b_enabled", 0))
    for c in counts13_list:
        d = _pair_baotou_distance(c)
        if (d is not None and d <= 2) or pair_shanten(c) <= 1:
            return True
        if check_b:
            bt_d = to_baotou_distance(c, meld_groups)
            if bt_d is not None and bt_d <= 2:
                return True
    return False


def _route_letters(weights):
    return ("B", "C", "D") if weights.get("rev_override_b_enabled", 0) else ("C", "D")


def maybe_override_discard(tiles, meld_groups, chain_count, piao, rules, weights, visible, original_fn):
    """叠加逻辑：范围满足时对所有候选弃牌算 route EV，只有最优路线是七对系
    （C/D；``rev_override_b_enabled`` 打开时含 B）且比原逻辑的选择高出门槛
    才改写；否则返回 None，调用方原样调用 ``original_fn()``。``original_fn``
    是原有选牌逻辑的零参闭包，只在范围满足时才会被调用一次，不复制任何
    打分代码。

    ``rev_override_b_enabled``（P3）打开后 ``_pair_rich`` 的预筛门槛变宽
    （标准爆头形通常对子少，走 B 路线的手牌远比七对系多），本函数的候选
    循环因此可能对更多手牌完整跑一遍——用 ``rev_time_budget_ms`` 同样的
    预算兜底，超预算立即放弃、回落 ``original_fn()``，不影响开关关闭或
    候选数不多时的既有行为（首版 ``rule_route_ev_enabled`` 的随机对比测试
    仍然逐一致，见 tests/test_route_ev.py）。"""
    deadline = time.perf_counter() + weights.get("rev_time_budget_ms", 150) / 1000.0
    try:
        if not applies_discard(tiles, meld_groups, chain_count, piao, rules, weights):
            return None
        cands = []
        for tile in sorted(set(tiles)):
            remaining = list(tiles)
            remaining.remove(tile)
            cands.append(tuple(to_counts(remaining)))
        if not _pair_rich(cands, meld_groups, weights):
            return None
        ctx = (rules or {}).get("_ctx") or {}
        best_tile, best_value, best_letter = None, None, None
        for tile in sorted(set(tiles)):
            _check_deadline(deadline)
            remaining = list(tiles)
            remaining.remove(tile)
            counts = to_counts(remaining)
            value, letter = best_route(counts, meld_groups, ctx, weights, visible)
            if best_value is None or value > best_value:
                best_value, best_tile, best_letter = value, tile, letter
        if best_letter not in _route_letters(weights) or best_tile is None:
            return None
        original_tile = original_fn()
        if original_tile is None or original_tile == best_tile:
            return None
        remaining = list(tiles)
        remaining.remove(original_tile)
        orig_value, _ = best_route(to_counts(remaining), meld_groups, ctx, weights, visible)
        if _passes_margin(best_value, orig_value, weights):
            return best_tile
        return None
    except Exception:
        return None


def claim_route_veto(hand, take, ctx, weights, visible=None):
    """吃/碰是否会葬送七对系（或 B，见 ``rev_override_b_enabled``）路线的期望
    值：门清 + 有财神时，若当前最优路线是 C/D（或 B），碰/吃固定副露 1 组、
    七对路线永久作废（``shanten.pair_route_allowed`` 的既有语义），只能退回
    标准/标准爆头（A/B）；退回后的最优 A/B 期望值若比当前期望值还低超过
    门槛，就放弃这次碰/吃。

    只做「否决」，不改变原有门禁顺序——调用方（``responses.choose_peng`` /
    ``choose_chi``）在最前面调用，True 时直接返回 None，不再往下走。"""
    try:
        if not weights.get("rule_route_ev_enabled", 0):
            return False
        if JOKER not in hand or len(hand) != 13:     # 只管门清（13 张）；有副露时七对路线本来就不存在
            return False
        if not _pair_rich([tuple(to_counts(hand))], 0, weights):
            return False
        before_value, before_route = best_route(to_counts(hand), 0, ctx, weights, visible)
        if before_route not in _route_letters(weights):
            return False
        left = list(hand)
        for t in take:
            left.remove(t)
        best_after = None
        for discard in sorted(set(left)):
            rest = list(left)
            rest.remove(discard)
            value, _ = best_route(to_counts(rest), 1, ctx, weights, visible, routes=("A", "B"))
            if best_after is None or value > best_after:
                best_after = value
        if best_after is None:
            return False
        return _veto_worse(before_value, best_after, weights)
    except Exception:
        return False


# --------------------------------------------------------------------------- P2
# 速度前瞻（开关 rev_speed_enabled）：全部手牌，不限门清/财神；只在同向听
# tier 内换牌，绝不改变向听。适用：chain=0、piao=0、非抓打圈、原逻辑同向听
# 层 tier 的向听 <= 1。
# ---------------------------------------------------------------------------

class _BudgetExceeded(Exception):
    """P2 内部信号：单次决策新增逻辑合计耗时超过 rev_time_budget_ms，
    立即放弃整个前瞻并回落原逻辑（不使用任何已算出的部分结果）。"""


def _check_deadline(deadline):
    if deadline is not None and time.perf_counter() > deadline:
        raise _BudgetExceeded()


def _tenpai_wait_rate(counts13, meld_groups, visible):
    """听牌（向听 0）态：返回 (听口活牌数, 是否爆头)。爆头时调用方用
    universe 代替活牌数（摸啥都胡）。"""
    from .rules import baotou as _baotou
    if _baotou(counts13, meld_groups):
        return None, True
    waits = ukeire(counts13, meld_groups)
    return _live_count(waits, visible), False


def _speed_candidate_value(counts13, meld_groups, ctx, weights, visible, deadline=None):
    """P2 核心：听牌/一向听的前瞻 EV。

    听牌（d==0）：q = 听口活牌数 / U（爆头则 q=1，fan=2）。
    一向听（d==1）：对每个有效牌 t 模拟摸入 -> 在保持听牌的弃牌里选听口
    活牌数最大的 -> 得 W_t；p = Σn_t / U，听牌后的
    q = (Σ n_t·W_t / Σ n_t) / U（若某个 t 摸入后能打成爆头听，W_t = U）。
    含 P1 输钱项（``rev_loss_enabled`` 打开时）。

    d==1 分支是双重循环（进张种数 x 34 种候选弃牌），每次 ``shanten`` 调用
    对多财神的手可达数毫秒量级——``deadline`` 提供时在内层循环里逐次检查，
    超预算立即抛 ``_BudgetExceeded``（调用方吞掉，等价于放弃这个候选的前瞻、
    整体回落原逻辑），不能等整个双重循环跑完才发现超时。异常/超预算返回
    0.0（调用方据此不会选中这个候选，等价于放弃前瞻、回落原逻辑的排序）。
    """
    try:
        _check_deadline(deadline)
        d = shanten(counts13, meld_groups)
        universe = _universe(counts13, meld_groups, visible)
        if universe <= 0:
            return 0.0
        if d == 0:
            w_rate, is_bt = _tenpai_wait_rate(counts13, meld_groups, visible)
            q = 1.0 if is_bt else min(1.0, (w_rate or 0.0) / universe)
            fan = 2 if is_bt else 1
            p = 0.0
        elif d == 1:
            waits = ukeire(counts13, meld_groups)
            total_n = 0.0
            weighted_w = 0.0
            for idx in waits:
                n_t = _live_count([idx], visible)
                if n_t <= 0:
                    continue
                values = list(counts13)
                values[idx] += 1
                best_w = 0.0
                for out_idx in range(NSUITS):
                    _check_deadline(deadline)
                    if values[out_idx] <= 0:
                        continue
                    cand = list(values)
                    cand[out_idx] -= 1
                    cand = tuple(cand)
                    if shanten(cand, meld_groups) != 0:
                        continue
                    w_rate, is_bt = _tenpai_wait_rate(cand, meld_groups, visible)
                    w_val = universe if is_bt else (w_rate or 0.0)
                    if w_val > best_w:
                        best_w = w_val
                total_n += n_t
                weighted_w += n_t * best_w
            if total_n <= 0:
                return 0.0
            p = total_n / universe
            q = min(1.0, (weighted_w / total_n) / universe)
            fan = 1
        else:
            return 0.0
        dealer = bool((ctx or {}).get("dealer"))
        wall = (ctx or {}).get("wall", 60)
        n_draws = max(0, wall - 20) // 4 + 1
        turn0 = _estimate_turn0(wall)
        survive = _survival_series(turn0, n_draws, weights)
        opp_pay_enabled = weights.get("rev_opp_pay_enabled", 0)
        pay_per_step = [_opp_pay_weighted(ctx or {}, weights)] * n_draws if opp_pay_enabled else None
        # P2 不查校准表：P2 的 p/q 来自逐张前瞻（q = 进张后最佳听口宽度），正是它区分
        # "进张多但听牌后卡张" vs "进张少但两面" 的依据；校准表只按前进牌张数分档，
        # 会把这些候选压成同一条曲线，等于废掉 P2。理论模型的已知偏差（q 用当前前进
        # 牌数代替听口）只出在 _routes() 里，P2 没有这个问题。
        win, lost, lost_pay = _win_and_lost(d, p, q, n_draws, survive, pay_per_step)
        payout = weights.get("rev_payout_dealer", 24) if dealer else weights.get("rev_payout_idle", 10)
        v = dealer_value((ctx or {}).get("round_no"), weights)
        value = win * (fan * payout + v)
        if opp_pay_enabled:
            value -= lost_pay
        elif weights.get("rev_loss_enabled", 0):
            loss_weight = weights.get("rev_loss_dealer" if dealer else "rev_loss_idle",
                                      10.33 if dealer else 4.73)
            value -= lost * loss_weight
        return value
    except _BudgetExceeded:
        raise
    except Exception:
        return 0.0


def applies_speed(chain_count, piao, rules, weights, tiles=None):
    if not weights.get("rev_speed_enabled", 0):
        return False
    # 持财神手默认不走 P2：P2 只按 1 番/爆头 2 番估值，看不到财神计划与七对系，
    # 持财神手已由 route EV（C/D/B）和拟合打分的爆头项负责。rev_speed_joker=1 才放开。
    if tiles is not None and JOKER in tiles and not weights.get("rev_speed_joker", 0):
        return False
    if chain_count or piao:
        return False
    ctx = (rules or {}).get("_ctx") or {}
    if ctx.get("catch_play"):
        return False
    return True


def maybe_override_speed(tiles, meld_groups, chain_count, piao, rules, weights, visible, original_fn):
    """P2 接入点：两个弃牌入口在现有 route EV（C/D）叠加之后、原逻辑之前调用。
    只在同向听 tier 内换牌，不改变向听；超时间预算或异常一律回落 None。"""
    t0 = time.perf_counter()
    budget = weights.get("rev_time_budget_ms", 150) / 1000.0
    deadline = t0 + budget
    try:
        if not applies_speed(chain_count, piao, rules, weights, tiles):
            return None
        from .shanten import route_shanten
        unique = sorted(set(tiles))
        currents = {}
        for tile in unique:
            _check_deadline(deadline)
            remaining = list(tiles)
            remaining.remove(tile)
            currents[tile] = route_shanten(to_counts(remaining), meld_groups)
        best_s = min(currents.values())
        if best_s > 1:
            return None
        tier = [tile for tile in unique if currents[tile] == best_s]
        if len(tier) <= 1:
            return None
        ctx = (rules or {}).get("_ctx") or {}

        def _immediate(tile):
            remaining = list(tiles)
            remaining.remove(tile)
            return _live_count(ukeire(to_counts(remaining), meld_groups), visible)

        topk = int(weights.get("rev_speed_topk", 4))
        ranked = sorted(tier, key=_immediate, reverse=True)[:max(1, topk)]
        best_tile, best_value = None, None
        for tile in ranked:
            _check_deadline(deadline)
            remaining = list(tiles)
            remaining.remove(tile)
            value = _speed_candidate_value(to_counts(remaining), meld_groups, ctx, weights, visible, deadline)
            if best_value is None or value > best_value:
                best_value, best_tile = value, tile
        if best_tile is None:
            return None
        original_tile = original_fn()
        if original_tile is None or original_tile == best_tile:
            return None
        _check_deadline(deadline)
        remaining = list(tiles)
        remaining.remove(original_tile)
        orig_value = _speed_candidate_value(to_counts(remaining), meld_groups, ctx, weights, visible, deadline)
        if _passes_margin(best_value, orig_value, weights):
            return best_tile
        return None
    except _BudgetExceeded:
        return None
    except Exception:
        return None


# --------------------------------------------------------------------------- P5
# 吃碰按账（开关 rev_claim_enabled）：比较"不吃碰"当前手的最优 EV 与
# "吃碰后打出最优一张"的最优 EV（副露+1，七对路线作废，含 P2 前瞻与 P1
# 输钱项）。返回 True=按账接受、False=按账否决、None=账本没有明确意见，
# 调用方回落原逻辑（``claim_route_veto`` 等）。
# ---------------------------------------------------------------------------

def claim_ev_decision(hand, take, ctx, weights, visible=None, meld_groups_before=0):
    t0 = time.perf_counter()
    deadline = t0 + weights.get("rev_time_budget_ms", 150) / 1000.0
    try:
        if not weights.get("rev_claim_enabled", 0):
            return None
        before_routes = ("A", "B", "C", "D") if meld_groups_before == 0 else ("A", "B")
        before_val, _ = best_route(to_counts(hand), meld_groups_before, ctx, weights, visible,
                                   routes=before_routes)
        if weights.get("rev_speed_enabled", 0):
            # 与吃碰后一侧对称：两边都取 max(路线账, 速度前瞻)，否则只有吃碰后一侧有前瞻加成，偏向接受吃碰。
            before_val = max(before_val, _speed_candidate_value(to_counts(hand), meld_groups_before, ctx, weights,
                                                                visible, deadline))
        left = list(hand)
        for t in take:
            left.remove(t)
        after_meld = meld_groups_before + 1
        best_after = None
        for discard in sorted(set(left)):
            _check_deadline(deadline)
            rest = list(left)
            rest.remove(discard)
            counts = to_counts(rest)
            value, _ = best_route(counts, after_meld, ctx, weights, visible, routes=("A", "B"))
            if weights.get("rev_speed_enabled", 0):
                speed_val = _speed_candidate_value(counts, after_meld, ctx, weights, visible, deadline)
                if speed_val > value:
                    value = speed_val
            if best_after is None or value > best_after:
                best_after = value
        if best_after is None:
            return False
        if _passes_margin(best_after, before_val, weights):
            return True
        if _veto_worse(before_val, best_after, weights):
            return False
        return None
    except Exception:
        return None
