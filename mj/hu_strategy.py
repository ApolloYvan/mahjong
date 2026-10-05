"""爆头时的直接胡 / 财飘 EV 决策。"""
from .fit import load_weights
from .rules import baotou
from .shanten import combined_route
from .state import canonical_chain_count, canonical_piao
from .rules import payout as rules_payout
from .tiles import JOKER, TILE_INDEX, to_counts

# 2026-09-22 实战试验：success_floor 0.5→0.3、fail_cost 4.0→1.0，放宽
# 低倍数/低成功率边界下的续飘意愿（原 0.5/4.0 是 s47 证伪场后钉死的保守
# 值，见本文件历史）。high_fan_force_fan/high_fan_force_success 两个"已经
# 是大牌就不要冒险"的护栏值保持不动——test_high_value_baotou_white_hu
# 锁定了 fan=16 时哪怕续飘成功率 0.8 也必须直接胡，不能被这次调整破坏。
# 若几场实战后净分转差，直接把 success_floor/fail_cost 改回 0.5/4.0 即可
# 回滚，其余逻辑不受影响。
DEFAULT_PIAO_THRESHOLDS = {
    "success_floor": 0.3,          # piao_success_rate 低于此值强制直接胡
    # 2026-09-23：3 → 12。原值 3 意味着「七对+爆头」(4番) 就被判定为"已经够本
    # 不要赌"——而这一类手的天花板是 32 番。实证：算我求您了（318局 33.0% 胜率，
    # z=+3.30）a_d48ec48ce492 第 3 局在 direct_fan=4 时选择继续飘，最终
    # 七对/双财飘/4个白板/爆头 = 32 番 +768；我们旧口径在同一手强制直接胡，
    # 庄家只值 4×24=96，一局白丢 672 分。按实测单步 91.3% 算，fan=4 剩 3 步的
    # EV = 4×2³×0.9³ = 23.3，是直接胡的 5.8 倍。
    # 上界仍由 test_high_value_baotou_white_hu（fan=16 必须直接胡）钉住，
    # 12 落在 (4, 16] 内既放开 4~8 番的续飘、又保留真·大牌不冒险。
    # 回滚：改回 3。
    "high_fan_force_fan": 12,      # direct_fan 达到此值时默认视为"已经够本"
    "high_fan_force_success": 0.85,  # 除非成功率高于此值，否则达到 high_fan_force_fan 就强制直接胡
    "fail_cost": 1.0,              # piao 失败的固定 EV 惩罚
    "piao_held_joker": 1,          # 允许飘「手里原有」的财神（设 0 回滚到只飘刚摸到的）
    "baotou_piao_success": 0.9,    # 「飘完仍爆头」时的实测成功率下界（设 0 回滚到纯结构估计）
}


def _payout_factor(snapshot):
    dealer = snapshot.get("dealer")
    seat = snapshot.get("seat")
    is_dealer = dealer is not None and dealer == seat
    return rules_payout(1, base=1, dealer=is_dealer)  # 直接复用真实赔率(24/10)，不再自己猜8/1

def _opponent_meld_max(snapshot):
    seat = snapshot.get("seat", -1)
    melds = snapshot.get("melds") or []
    if not (isinstance(melds, list) and len(melds) == 4 and all(isinstance(m, list) for m in melds)):
        return 0
    return max((len(m) for index, m in enumerate(melds) if index != seat), default=0)


def _piao_success_rate(snapshot):
    """结构性估计：白不可被吃碰杠胡（弃白无被动风险）；风险来自对手冲刺、链加深与墙尾。"""
    risk = 0.15 * _opponent_meld_max(snapshot)
    risk += 0.08 * (snapshot.get("chain_count", 0) + snapshot.get("piao", 0))
    if snapshot.get("wall_remaining", 40) <= 30:
        risk += 0.2
    return max(0.05, 0.8 * (1.0 - risk))



def _meld_groups(snapshot):
    seat = snapshot.get("seat", -1)
    melds = snapshot.get("melds") or []
    if isinstance(melds, list) and len(melds) == 4 and isinstance(seat, int) and 0 <= seat < 4:
        return len(melds[seat])
    return 0


def _piao_candidate(snapshot, hu_result, chain, piao, thresholds):
    """能不能财飘、飘哪张——返回要打出的牌，不能飘则 None。

    2026-09-23 实测：**财飘的主流用法是打出手里原有的财神，而这里原先只在
    「刚摸到的那张就是财神」时才考虑。** 全平台 295 次弃财神里，「打之前手牌
    本来就能胡」的真财飘共 62 次，其中 **37 次飘的是手里原有的财神（60%）**，
    只有 25 次是刚摸到的。我们 88 次弃财神里 86 次是刚摸到的、手里原有的只有
    2 次——约 176 次机会被直接跳过，这正是我们 ≥4番收入只占 3.4%
    （两脚离地 27.8%、Astra-0 22.2%）的原因。

    判据只有一条，但它是硬的——**打掉这张财神之后必须仍然爆头**：

        A 打掉后仍爆头   n=46  最终胡牌 42 次 = 91.3%  胡时平均番 4.29
        B 打掉后不再爆头 n=16  最终胡牌  5 次 = 31.2%  胡时平均番 1.00

    A 的期望番 0.913×4.29 = 3.92，而立刻胡只有 2——接近翻倍，风险 8.7%，
    是近似套利而不是赌博。B 是拿确定的胡牌换窄听，纯亏。

    刚摸到的财神天然满足该判据（打掉它剩下的就是原来那副爆头牌），
    所以只对「手里原有」的情形显式验算。

    回滚：把 thresholds["piao_held_joker"] 设成 0，即恢复到只飘刚摸到的那张。
    """
    if not hu_result.get("baotou") or chain + piao >= 3:
        return None
    if snapshot.get("drawn_tile") == JOKER:
        return JOKER
    if not thresholds.get("piao_held_joker", 1):
        return None
    counts = hu_result.get("counts")
    if not counts or counts[TILE_INDEX[JOKER]] < 1:
        return None
    after = list(counts)
    after[TILE_INDEX[JOKER]] -= 1
    if not baotou(tuple(after), _meld_groups(snapshot)):
        return None
    return JOKER


def _decline_discard_choice(snapshot, hu_result):
    """非爆头弃胡后打哪张：不是 ``drawn_tile``，也不是财神——而且**必须**
    真的能转成爆头形，否则不弃胡（直接返回 None，调用方回落到 hu）。

    2026-09-24 收敛任务 S1（``tools/five_questions.py``，完整数字见
    ``docs/experiments/OFFLINE_REPORT.md``「五问结论」S1）：把全语料里真实
    发生的「非爆头可胡但弃胡」按弃牌分三类回看最终结果——

        (a) 打财神         n=2     y_score=-4.0     （几乎不发生，且赔本）
        (b) 原样弃掉摸到的牌 n=43    y_score=14.7   胡率 58.1%
        (c) 打别的牌、留住摸到的牌 n=1462  y_score=25.0  胡率 80.9%，
            91.2% 最终转成爆头

    (c) 对 (b) 的 y_hu 差异 z=3.70,p=0.0002，显著。真正赚分的是「把刚摸到的
    牌吸收进手牌，从原有的牌里挑一张最没用的扔掉」，不是机械地弃掉摸到的
    那张——旧实现（原样弃 ``drawn_tile``）恰好只覆盖了 (a)/(b) 两个更差的
    类别，从未真正走到 (c)。

    2026-09-24 correctness 修复：初版实现只按 (向听, -进张数) 挑"看起来
    最好"的非财神牌，**没有验证弃完之后是不是真的爆头**——也就是说旧版本
    可能在弃完之后仍然停留在非爆头形（既不是立刻确定的小胡，也没换到爆头
    的弹性），两头都不讨好。整个开关存在的意义就是"用一次确定的小胡去换
    一手爆头"，所以硬性要求：候选弃牌必须满足弃完之后
    ``rules.baotou(counts, meld_groups)`` 为真，不满足就不在候选范围内。
    若不存在这样的牌，返回 None（调用方回落到直接胡，不弃）。

    候选范围：手中除财神外的所有牌（含摸到的那张——若摸到的牌本身就是唯一
    能转爆头的那张，选它退化为类别 b，允许）。多张都能转爆头时，按
    ``combined_route`` 的 (向听, -进张数) 取最优（更快、听口更宽的优先）。
    """
    hand = list(snapshot.get("my_hand") or [])
    meld_groups = _meld_groups(snapshot)
    candidates = sorted(set(t for t in hand if t != JOKER))
    best_tile, best_key = None, None
    for cand in candidates:
        left = list(hand)
        left.remove(cand)
        counts_left = to_counts(left)
        if not baotou(counts_left, meld_groups):
            continue
        s, waits = combined_route(counts_left, meld_groups)
        key = (s, -len(waits))
        if best_key is None or key < best_key:
            best_key, best_tile = key, cand
    return best_tile


def _decline_small_hu_tile_ev(snapshot, hu_result, weights):
    """P4（开关 ``rev_decline_enabled``，默认关闭；docs/IMPL_UNIFIED_EV_V2.md）：
    非爆头弃胡转爆头改用按账决策，替代 ``_decline_small_hu_tile`` 原有的
    财神数x副露数格子表与"剩余<8张"墙尾闸门。

    弃哪张仍用 ``_decline_discard_choice``（该函数已保证候选弃牌弃完之后
    真的转成爆头形）；只改"值不值得弃"这一步的判据：
        当场胡 = f x payout
        转爆头 = P(下一次自己摸牌之前别家都没先自摸) x 2f x payout
                 − P(别家先自摸) x loss
    取大。爆头态摸任意牌都胡，所以只需要单步生存概率（与
    mj.route_ev._single_step_survive 同一套 hazard 表），不需要多步 DP。"""
    if hu_result.get("baotou"):
        return None
    if not weights.get("s1_dealer_enabled", 1) and snapshot.get("dealer") == snapshot.get("seat"):
        return None
    # 弃了之后至少还得轮到自己再摸一次（别家 3 张 + 自己 1 张），否则转爆头没有兑现机会，当场胡。
    if snapshot.get("wall_remaining", 40) - 20 < 4:
        return None
    tile = _decline_discard_choice(snapshot, hu_result)
    if tile is None:
        return None
    direct_fan = max(1, hu_result.get("fan", 1))
    from . import route_ev
    s = route_ev._single_step_survive(snapshot.get("wall_remaining", 40), weights)
    is_dealer = snapshot.get("dealer") == snapshot.get("seat")
    # T3（rev_dealer_value_enabled）：当场胡与转爆头两侧都加 V(局号)；
    # T1（rev_opp_pay_enabled）：付出项从平均数换成按"谁是庄"加权的付分。
    # 两者默认关闭时 v=0、pay=旧平均数，公式与首版 P4 逐一致。
    v = route_ev.dealer_value(snapshot.get("round_no"), weights)
    pay = route_ev._pay_out_term({"dealer": is_dealer}, weights, is_dealer)
    direct_value = direct_fan * _payout_factor(snapshot) + v
    transform_value = s * (2 * direct_fan * _payout_factor(snapshot) + v) - (1.0 - s) * pay
    return tile if transform_value > direct_value else None


def _decline_small_hu_tile(snapshot, hu_result, weights=None):
    """规则开关 ``rule_decline_joker_hold_enabled``（默认关闭）：非爆头可胡时，
    继续做大牌而不是立刻收掉小番平胡。

    2026-09-24 收敛任务 S1 重写触发条件（原口径「手上财神 >= 2 张」，见
    ``mj/fit.py::DEFAULT_WEIGHTS`` 里本键旁的完整注释与
    ``docs/experiments/OFFLINE_REPORT.md``「五问结论」S1）：全语料按
    （摸到的是否白板 × 手上财神数 × 是否庄 × 牌墙余量）分层后，「手上
    财神>=2」只是效应最强的一格，并不是唯一显著的一格——「手上财神==1」
    与「本次摸到白板（无论手上原有几张）」两格同样显著、且方向一致：

        条件                              top 弃胡率   我们弃胡率
        财神>=2（原口径）                   49.5%        3.3%
        财神>=2 或 本次摸到白板             41.8%        9.3%
        财神>=1 或 本次摸到白板（本次采用） 28.8%        4.9%

    三个候选条件的 Δ(y_score)（同人内部、房间聚类自助 CI）都显著为正且
    70/30 房间留出复现（**这一组数字是选触发条件时用的，口径是"只要真实
    弃胡了就算"，没有验证弃完之后是不是真的爆头，见下面的 correctness
    修正）：
        财神>=2                 Δ=13.39 [10.55,16.61]  覆盖率(我方) 0.51%
        财神>=2 或摸白           Δ=10.63 [ 9.06,12.26]  覆盖率(我方) 1.15%
        财神>=1 或摸白（本次）   Δ=10.54 [ 9.38,11.85]  覆盖率(我方) 2.23%
    预期收益 = Δ×覆盖率：0.068 / 0.122 / 0.235——「财神>=1 或摸白」在三者
    里 CI 最紧、覆盖率最高、预期收益最大，故采用它替换原口径（三个候选谁
    更好这个相对排序不受下面的 correctness 修正影响，因为修正是对所有
    候选同样生效的一个额外过滤条件，没有重新跑三选一）。

    2026-09-24 correctness 修正后的真实数字（``tools/s1_recheck_baotou.py``
    + ``_decline_discard_choice`` 要求的"必须存在能转爆头的非财神弃牌"这一
    条件套回 decisions.csv 重新计算，见 OFFLINE_REPORT.md「五问结论」S1）：
    旧口径统计的 1511 次真实弃胡里，**164 次（10.9%）打完之后根本没有转成
    爆头**——旧的 Δ/覆盖率把这些"弃了但没转成"的样本也算进了"弃胡"这一组，
    高估了收益。只保留"弃胡当下确实存在能转爆头的牌"这个更严格的人群
    （n=2290，远小于旧口径的 8581）重新算：
        Δ(y_score) = 7.23  95%CI[3.97,10.82]
        70/30 房间留出：train 7.22[3.06,11.69] / test 7.99[1.57,15.11]（复现）
        覆盖率(我方) = 0.585%（旧口径高估为 2.23%）
        预期收益 = Δ×覆盖率 ≈ 0.042（旧口径高估为 0.235，约 5.6 倍）
        top vs bottom 该情境弃胡率：88.97% vs 46.38%（z=10.58,p≈0，仍非常显著）
        真实弃胡里 1347/1347 落在该人群、其中 1334 次（99.0%）确实转成爆头
        （证明"存在可转爆头的牌"时，各技术层级的真实玩家几乎都能抓住它——
        这也是为什么函数改成"验证后再弃"而不是"猜哪张最好"就够了）。

    2026-09-24 收窄（第三次修订，用户反馈）：上面「财神>=1 或摸白」这个
    触发条件是在"S1 会触发"的全体人群上一次性算出来的，没有再往下细分
    "财神数 × 副露数"——细分之后发现这个条件其实**偏宽**：不是所有
    （有效财神数 × 副露数）格子里，弃胡都同时满足"高手弃胡率>=40%"和
    "Δ(y_score) 房间聚类自助 CI 下界>0"这两条硬门槛。用
    ``tools/s1_narrow.py`` 在 top cohort（10人）与「嘎达嘎达」
    （uid=u_45062bea0121，独立 100 场，单人对照）两个人群上分别按
    （有效财神数:1/2+ x 副露数:0/1/2+）分格重新核算（有效财神数 =
    ``hu_result["counts"][JOKER_IDX]``，即摸牌后手上财神数，与本函数下面
    实际读取的 ``counts`` 语义完全一致，不需要再单独判断"是否摸到白板"）：

        格子(财神,副露)  top n   top弃胡率  top Δ(CI)          gada n  gada弃胡率  gada Δ(CI)         KEEP
        (1,   0)         17     82.4%     Δ无法计算(n_people=0)  6      83.3%     15.2[-1.0,28.7]     否
        (1,   1)         45     97.8%     31.0[10.0,38.0]        6      66.7%     17.0[10.0,24.0]     **是**
        (1,   2+)        55     92.7%     13.25[0.0,25.6]        7      85.7%     19.3[10.0,31.0]     否(CI下界=0.0，不严格>0)
        (2+,  0)         54     83.3%     14.2[4.0,20.7]         9      100%      Δ无法计算(n_people=0) **是**
        (2+,  1)         69     95.7%     48.0[10.0,86.0]        10     90.0%     13.1[3.0,22.0]      **是**
        (2+,  2+)        45     80.0%     23.0[12.0,30.0]        8      75.0%     6.3[-6.0,27.5]      **是**

    以 top cohort 为准（10 人、全语料的"高手"参照组，本报告全程用它做
    主判据），保留 KEEP=是 的 4 格；嘎达嘎达作为独立单人对照——(1,1) 与
    (2+,1) 两格两个人群**都**通过，方向一致；(2+,0)/(2+,2+) 嘎达嘎达因
    单人样本太小（n_people=0 或 CI 过宽）**无法确认也无法反驳**，不是
    "证据相反"；(1,0)/(1,2+) 两个人群都没有通过，一致排除。**新触发条件
    等价于**：有效财神数>=2 时任意副露数都触发；有效财神数==1 时只有
    副露数==1 才触发（副露 0 或 2+ 都不触发）。完整数字、方法论、与
    top_policy_replay 重新核算的一致率见 OFFLINE_REPORT.md「五问结论」
    S1（收窄版）。

    只处理**非爆头**分支——爆头态的胡/飘取舍已经由 ``_piao_candidate`` 负责，
    不在这里重复判定（避免两套逻辑对同一情境给出冲突结论）。

    弃哪张牌：见 ``_decline_discard_choice``——不再机械地弃 ``drawn_tile``，
    且必须验证弃完之后真的转成爆头形，否则不弃（见该函数的 correctness
    修复说明）。

    墙尾闸门（correctness 修复第二条，收窄后仍保留）：
    ``wall_remaining - 20 < 8`` 时不弃胡，直接胡——留给爆头兑现的摸牌轮次
    太少（不到 8 张牌，即不到 2 巡），赌爆头的赔率已经不划算，与
    ``choose_hu_or_piao`` 里 ``draws_left < 4 * remaining_chain`` 那道
    墙尾硬闸是同一类护栏。

    返回要弃的牌；不满足条件（含开关关闭、墙尾过浅、不在保留的
    财神数×副露数格子里、没有能转爆头的牌）返回 None（调用方回落到直接胡）。
    """
    weights = weights if weights is not None else load_weights()
    if not weights.get("rule_decline_joker_hold_enabled", 0):
        return None
    # s1_dealer_enabled（默认 1 = 现行为）：0 = 庄家不触发 S1 弃胡（消融用）
    if not weights.get("s1_dealer_enabled", 1) and snapshot.get("dealer") == snapshot.get("seat"):
        return None
    if hu_result.get("baotou"):
        return None
    counts = hu_result.get("counts")
    if not counts:
        return None
    # E3（s1_ev_enabled，默认 0）：按期望值决定弃胡，替换下面的"财神数 x 副露数"格子表（见 mj/s1_variants.py）
    if weights.get("s1_ev_enabled", 0):
        from . import s1_variants
        return s1_variants.ev_choice(snapshot, hu_result, weights, _decline_discard_choice)
    effective_jokers = counts[TILE_INDEX[JOKER]]
    meld_groups = _meld_groups(snapshot)
    # 收窄后的触发格子（见上方 docstring 的分格核算表）：
    #   有效财神>=2：任意副露数都触发；有效财神==1：只有副露数==1 才触发。
    # rule_decline_single_joker_all_melds_enabled（默认关）：财神==1 时副露 0 / 2+
    # 也触发。上表这两格高手弃胡率 82.4% / 92.7%，被排除只是因为 CI 算不出或
    # 下界恰为 0；实战（2026-09-25，三批 47 房）S1 共触发 104 次、89% 转成自摸、
    # 比当场胡净多 +1073 分，其中财神==1/副露 1 这一格 10 次全胡、净 +169。
    single_all = weights.get("rule_decline_single_joker_all_melds_enabled", 0)
    # 2026-09-25：三批实战（batch_review S1 审计）里 1白/1露 这一格触发 7 次只胡 3 次、净 −86，
    # 其余各格胡率 80%~100%、全部净正。rule_decline_single_joker_one_meld_enabled=0 单独关掉这一格。
    one_meld = weights.get("rule_decline_single_joker_one_meld_enabled", 1)
    if effective_jokers == 1 and meld_groups == 1:
        triggers = bool(one_meld)
    else:
        triggers = effective_jokers >= 2 or (effective_jokers == 1 and single_all)
    if not triggers:
        return None
    # E1（s1_wall_relax_enabled，默认 0）：墙尾门槛 8 -> s1_wall_relax_min（默认 4）；关闭时 wall_min() 恒为 8
    from . import s1_variants
    if snapshot.get("wall_remaining", 40) - 20 < s1_variants.wall_min(weights):
        return None
    tile = _decline_discard_choice(snapshot, hu_result)
    # E2（s1_one_step_enabled，默认 0）：没有能直接转爆头的牌时，允许"差一步转爆头"的弃胡
    if tile is None and weights.get("s1_one_step_enabled", 0):
        tile = s1_variants.one_step_choice(snapshot, hu_result, weights, meld_groups)
    return tile


def choose_hu_or_piao(snapshot, hu_result, thresholds=None):
    if not hu_result:
        return None
    th = DEFAULT_PIAO_THRESHOLDS if thresholds is None else {**DEFAULT_PIAO_THRESHOLDS, **thresholds}
    drawn = snapshot.get("drawn_tile")
    # 2026-09-23：改用 state.canonical_* 规范读取。原先直接取顶层
    # snapshot["chain_count"]，而 state.py 明确规定 god.chain_count 才是规范源
    # （mj.bot.choose_discard 早已走 canonical_*）——两条路径口径不一致，
    # 在只带 god 块的快照上会把链次数读成 0，进而算错 remaining_chain。
    chain = canonical_chain_count(snapshot)
    piao = canonical_piao(snapshot)
    piao_tile = _piao_candidate(snapshot, hu_result, chain, piao, th)
    weights = load_weights()
    if piao_tile is None:
        decline_tile = None
        if weights.get("rev_decline_enabled", 0):
            try:
                decline_tile = _decline_small_hu_tile_ev(snapshot, hu_result, weights)
            except Exception:
                decline_tile = None
        else:
            decline_tile = _decline_small_hu_tile(snapshot, hu_result, weights)
        if decline_tile is not None:
            return {"action": "discard", "tile": decline_tile}
        return {"action": "hu", "tile": drawn or ""}
    if weights.get("rev_piao_enabled", 0):
        try:
            if snapshot.get("piao_force_hu"):
                return {"action": "hu", "tile": drawn}
            remaining_chain_gate = 3 - chain - piao
            draws_left_gate = snapshot.get("wall_remaining", 40) - 20
            if draws_left_gate < 4 * remaining_chain_gate:
                return {"action": "hu", "tile": drawn}
            from . import route_ev
            s = route_ev._single_step_survive(snapshot.get("wall_remaining", 40), weights)
            is_dealer = snapshot.get("dealer") == snapshot.get("seat")
            # T3：当场胡与飘两侧都加 V(局号)；T1：付出项换成按"谁是庄"加权的
            # 付分。两者默认关闭时 v=0、pay=旧平均数，与首版 P4 逐一致。
            v = route_ev.dealer_value(snapshot.get("round_no"), weights)
            pay = route_ev._pay_out_term({"dealer": is_dealer}, weights, is_dealer)
            direct_fan = max(1, hu_result.get("fan", 1))
            payout_factor = _payout_factor(snapshot)
            direct_value = direct_fan * payout_factor + v
            piao_value = s * (direct_fan * 2 * payout_factor + v) - (1.0 - s) * pay
            if piao_value > direct_value:
                return {"action": "discard", "tile": piao_tile}
            return {"action": "hu", "tile": drawn}
        except Exception:
            pass
    # 2026-09-23：成功率分两个总体，不能共用一个结构估计。
    # _piao_success_rate() 是纯结构猜测（0.8 × (1 - 对手明牌/已有链/墙尾风险)），
    # 在典型局面上只给到 ~0.43，直接触发 high_fan_force_* 护栏强制直接胡。
    # 但 _piao_candidate 已经保证了「打掉这张财神之后仍然爆头」——对这个总体
    # 我有直接实测：n=46，最终胡牌 42 次 = 91.3%，胡时平均 4.29 番
    # （对照组「打掉后不再爆头」n=16 只有 31.2%，所以门槛留在 _piao_candidate
    # 里，这里只负责给已经过门的那一类一个诚实的成功率）。
    # 取 max(结构估计, 0.9)：这确实让风险项在本分支失效，但 46 局的实测本身
    # 已经对对手明牌数与墙尾状态做了平均；真正危险的墙尾由下面
    # draws_left < 4 * remaining_chain 那道硬闸单独拦截。
    # 注意这是**单步**成功率，必须按剩余步数复利后才能用——见下方 chain_rate。
    # snapshot 显式给的 piao_success_rate 仍然优先（测试用它钉死护栏行为）。
    # 回滚：把 thresholds["baotou_piao_success"] 设成 0。
    piao_success_rate = snapshot.get("piao_success_rate")
    if piao_success_rate is None:
        piao_success_rate = max(_piao_success_rate(snapshot),
                                th.get("baotou_piao_success", 0))
    remaining_chain = 3 - chain - piao
    draws_left = snapshot.get("wall_remaining", 40) - 20
    if draws_left < 4 * remaining_chain:
        return {"action": "hu", "tile": drawn}
    # 2026-09-23 口径修正：piao_success_rate 是**一步**的成功率，而 piao_fan 已经
    # 按 2**remaining_chain 把整条剩余链的收益都算满了。原先只乘一次 rate，等于
    # 假设「飘一次成功 == 连飘到底成功」——剩余 3 步时把乐观算了三遍。
    # 这正是 91.3% 那份实测的含义所在：打掉后仍爆头的 46 手最终胡牌 42 手，但
    # **胡时平均只有 4.29 番**，而不是底番×2³。按步复利后两边口径才一致。
    # 副作用（正确的）：chain=2 只剩 1 步 → 0.9，越过 0.85 护栏可以续飘；
    # chain=0 还剩 3 步 → 0.9³=0.729，仍被护栏拦住，"已经是大牌就别赌"不变。
    chain_rate = piao_success_rate ** remaining_chain
    direct_fan = max(1, hu_result.get("fan", 1))
    direct_value = direct_fan * _payout_factor(snapshot)
    piao_fan = direct_fan * (2 ** remaining_chain)
    piao_value = piao_fan * _payout_factor(snapshot) * chain_rate
    if snapshot.get("piao_force_hu"):
        return {"action": "hu", "tile": drawn}
    if direct_fan >= th["high_fan_force_fan"] and chain_rate <= th["high_fan_force_success"]:
        return {"action": "hu", "tile": drawn}
    if piao_success_rate < th["success_floor"] or piao_value <= direct_value + th["fail_cost"]:
        return {"action": "hu", "tile": drawn}
    return {"action": "discard", "tile": piao_tile}
