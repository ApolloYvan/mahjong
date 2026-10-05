"""4 座位对抗离线竞技场：自摸制、赢家坐庄、实战计分模型，用于策略 A/B。

计分（实战数据实证）：
- 闲家胡 fan：赢家 +10×fan，庄家 -8×fan，另一闲家 -1×fan
- 庄家胡 fan：赢家 +24×fan，其余每家 -8×fan
- 赢家坐庄，流局庄家连庄
"""
import hashlib
import random
import sys
from collections import Counter

from .ev import choose_route_discard
from .observability import build_hash
from .rules import evaluate
from .shanten import pair_shanten, shanten
from .strategy import choose_discard as base_discard
from .tiles import ALL_TILES, INDEX_TILE, JOKER, TILE_INDEX, to_counts

# 评测器 schema 版本（独立于 mj.observability.SCHEMA_VERSION / mj.datastore.
# SCHEMA_VERSION——描述的是"评测报告 JSON 结构"本身的版本，不是日志或
# SQLite 表结构）。本轮（P0：财神响应限制 + 两摊吃上限 + 能力声明诚实化）
# 修复了硬规则违例，评测报告结构本身也新增字段，version 从 1 升到 2。
EVALUATOR_SCHEMA_VERSION = 2

# 本评测器（离线部分规则评测入口）对官方硬规则的模拟覆盖度声明——
# True 表示该规则已在 play_hand()/resolve_claim()/SeatStrategy 中真实
# 实现并有测试覆盖；False 表示尚未模拟，报告统计结论不覆盖该规则的
# 影响。禁止把本表当作"官方规则完整实现"的证明——它只是诚实的能力边界
# 声明，供报告消费者判断结果的适用范围。
RULE_COVERAGE = {
    "self_draw": True,           # 自摸制：evaluate() 只在摸牌方胡牌时判定，无点炮胡。
    "chi_peng_priority": True,   # resolve_claim()：peng 优先于 chi，且严格按 offset 顺序。
    "joker_claim_restriction": True,  # 财神（JOKER）不可被吃/碰（本轮修复，peng_take/chi_take 双重拦截）。
    "max_two_chi": True,         # 单座位最多两摊吃（本轮修复，resolve_claim() 用 _chi_count 拦截）。
    "gang": False,               # 杠（暗杠/明杠/补杠）与抢杠胡：play_hand() 未实现杠动作，本轮不实现。
    "catch_play_circle": False,  # 抓打圈（打出财神那一圈其余玩家只能打刚摸到的牌）：未实现。
    "chain_piao": "partial",     # 摸白构成爆头时的 hu-vs-弃白续飘 决策（复用生产
                                  # mj.hu_strategy.choose_hu_or_piao）+ evaluate() 按
                                  # 2**chain_count 计番，已模拟；但抓打圈对其余三家
                                  # 的吃/碰/出牌限制未模拟（其余三家在续飘的这一轮
                                  # 仍按平时逻辑响应），且不模拟"吃碰后再打财神"触发
                                  # 的链（只模拟"摸白后主动续飘"这一条路径）。据此得出
                                  # 的续飘阈值结论，真实生产环境下可能因抓打圈保护
                                  # 缺失而偏乐观。
}


def wall(rng):
    tiles = [tile for tile in ALL_TILES for _ in range(4)]
    rng.shuffle(tiles)
    return tiles


class SeatStrategy:
    """kind: base(速度) / current(现行: 庄家走爆头 route) / route / pure
    claims: real(生产门控) / all(无脑全吃碰) / peng(只碰) / none(不声明)"""

    def __init__(self, kind, claims="real", weights=None):
        self.kind = kind
        self.claims = claims
        self.weights = weights

    def choose_hu_or_piao(self, snapshot, hu_result):
        """摸白且构成爆头听时，直接胡还是弃白续飘——复用生产决策函数
        mj.hu_strategy.choose_hu_or_piao；``weights`` 里若含保留键
        ``_piao_thresholds``（dict），作为该函数的 thresholds 覆盖参数，
        供 mj.arena 网格评测不同续飘阈值（不传则与生产默认行为一致）。"""
        from .hu_strategy import choose_hu_or_piao as _choose
        thresholds = (self.weights or {}).get("_piao_thresholds")
        return _choose(snapshot, hu_result, thresholds=thresholds)

    def discard(self, hand, meld_groups, dealer, chain=0, piao=0):
        rules = {"_weights": self.weights} if self.weights else None
        if self.kind == "pure":
            best, best_key = None, None
            for tile in sorted(set(hand)):
                left = list(hand)
                left.remove(tile)
                key = -self._hand_value(left, meld_groups)
                if best_key is None or key > best_key:
                    best, best_key = tile, key
            return best
        if self.kind == "route":
            return choose_route_discard(hand, meld_groups, chain, piao)
        if self.kind == "current" and dealer:
            merged = {"dealer_hint": True}
            if self.weights:
                merged["_weights"] = self.weights
            return choose_route_discard(hand, meld_groups, chain, piao, merged)
        return base_discard(hand, meld_groups, chain, piao, rules)

    def _hand_value(self, left, meld_groups):
        counts = to_counts(left)
        value = shanten(counts, meld_groups)
        if meld_groups == 0:
            value = min(value, pair_shanten(counts))
        return value

    def peng_take(self, hand, tile, meld_groups=0, melds_all=None, seat=0, wall=50):
        if tile == JOKER:
            return None  # 官方硬规则：财神不可被吃/碰/杠/胡，任何 claims 模式一律禁止。
        if self.claims == "none" or hand.count(tile) < 2:
            return None
        if self.claims == "real":
            from .responses import choose_peng
            snapshot = {"my_hand": list(hand), "window_tile": tile, "seat": seat,
                        "wall_remaining": wall, "melds": melds_all}
            return [tile, tile] if choose_peng(snapshot) else None
        if self.claims == "ev":
            before = self._hand_value(hand, meld_groups)
            left = list(hand)
            left.remove(tile)
            left.remove(tile)
            if self._hand_value(left, meld_groups + 1) > before:
                return None
        if self.claims == "loose":
            before = self._hand_value(hand, meld_groups)
            left = list(hand)
            left.remove(tile)
            left.remove(tile)
            if self._hand_value(left, meld_groups + 1) > before + 1:
                return None
        return [tile, tile]

    def chi_take(self, hand, tile, meld_groups=0, melds_all=None, seat=0, wall=50):
        if tile == JOKER:
            return None  # 字牌本不可吃（下方 idx>=27 分支已排除），此处显式声明
            # 防止未来吃牌路径改动时无意放行财神——不依赖隐含的分支顺序。
        if self.claims not in ("all", "real", "loose"):
            return None
        idx = TILE_INDEX[tile]
        if idx >= 27:
            return None
        if self.claims == "real":
            from .responses import choose_chi
            snapshot = {"my_hand": list(hand), "window_tile": tile, "seat": seat,
                        "wall_remaining": wall, "melds": melds_all}
            decision = choose_chi(snapshot)
            return list(decision["tiles"]) if decision else None
        suit, low = idx // 9, idx % 9
        allowance = 0 if self.claims == "all" else 1
        best = None
        for a, b in ((low - 2, low - 1), (low - 1, low + 1), (low + 1, low + 2)):
            if a < 0 or b > 8:
                continue
            first = INDEX_TILE[suit * 9 + a]
            second = INDEX_TILE[suit * 9 + b]
            if hand.count(first) and hand.count(second):
                if self.claims == "all":
                    return [first, second]
                before = self._hand_value(hand, meld_groups)
                left = list(hand)
                left.remove(first)
                left.remove(second)
                after = self._hand_value(left, meld_groups + 1)
                if after <= before + allowance and (best is None or after < best[0]):
                    best = (after, [first, second])
        return best[1] if best else None


def _chi_count(melds_for_seat):
    """安全统计某座位已有的 chi 摊数：入参不是 list、或其中某条目不是
    dict / 缺失 "kind" 字段（历史 fixture 常见形态）时，一律不计入且不
    抛异常——保守返回已能确认的 chi 计数，不让异常数据中断评测。"""
    count = 0
    if not isinstance(melds_for_seat, list):
        return count
    for meld in melds_for_seat:
        if isinstance(meld, dict) and meld.get("kind") == "chi":
            count += 1
    return count


def settle(winner, dealer, fan, scores):
    for seat in range(4):
        if seat == winner:
            scores[seat] += fan * (24 if winner == dealer else 10)
        elif seat == dealer or winner == dealer:
            scores[seat] -= fan * 8
        else:
            scores[seat] -= fan * 1


def resolve_claim(seat, discard, hands, melds, strategies, tiles_remaining):
    """需求一返修：出牌后的响应裁决，返回 (claimed_seat, claim_kind,
    claim_tiles)（均为 None 表示无人响应）。

    规则（按确定优先顺序，peng 优先于 chi）：
    1. 按 offset=(1,2,3)（下家→对家→上家）依次检查其余三家是否 peng；
       只要有人 peng，响应竞争立即结束，不再检查 chi。
    2. 只有无人 peng 时，且只有出牌者下家 next_seat=(seat+1)%4 才会被
       检查是否 chi；另外两个座位绝不会调用 chi_take（不是"检查后拒绝"，
       而是根本不进入检查——修复此前对 chi 也遍历 offset=(1,2,3) 导致
       任意座位都能吃牌的 bug）。
    3. 调用 peng_take/chi_take 时，meld_groups 一律使用响应者自己的
       len(melds[responding_seat])，不得沿用出牌者的 meld_groups（修复
       此前把出牌者 meld_groups 错误传给响应者的 bug）。
    4. 两摊吃上限（官方硬规则）：响应者自身 melds 中 kind=="chi" 的数量
       达到 2 时，即使总副露数<4、且持有合法搭子，也绝不调用其
       chi_take（不是"调用后拒绝结果"，而是根本不进入调用）。此限制
       只作用于 chi，不影响 peng——已有两摊吃、总副露<4 时仍可正常碰
       普通牌。
    """
    for offset in (1, 2, 3):
        other = (seat + offset) % 4
        if len(melds[other]) >= 4:
            continue
        use = strategies[other].peng_take(
            hands[other], discard, len(melds[other]), melds, other, tiles_remaining)
        if use:
            return other, "peng", use
    next_seat = (seat + 1) % 4
    if len(melds[next_seat]) < 4 and _chi_count(melds[next_seat]) < 2:
        use = strategies[next_seat].chi_take(
            hands[next_seat], discard, len(melds[next_seat]), melds, next_seat, tiles_remaining)
        if use:
            return next_seat, "chi", use
    return None, None, None


def play_hand(rng, strategies, dealer):
    tiles = wall(rng)
    hands = [tiles[:13], tiles[13:26], tiles[26:39], tiles[39:52]]
    tiles = tiles[52:]
    melds = [[], [], [], []]
    # chain_count[seat]：本手该座位摸白后主动"弃白续飘"的次数（简化模型，
    # 见 RULE_COVERAGE["chain_piao"] 的能力边界声明——只模拟"摸白时
    # hu-vs-continue 决策 + 番数按 2**chain_count 计算"，不模拟抓打圈对其
    # 余三家的吃碰/出牌限制）。
    chain_count = [0, 0, 0, 0]
    turn = dealer
    must_discard = False
    draws = 0
    while True:
        seat = turn
        meld_groups = len(melds[seat])
        expected = 13 - 3 * meld_groups
        if not must_discard:
            if len(tiles) <= 20 or draws > 400:
                return None
            drawn = tiles.pop()
            draws += 1
            hands[seat].append(drawn)
            hand = hands[seat]
            win_hand = hand[:-1] if len(hand) == expected + 1 else hand
            if len(win_hand) == expected:
                result = evaluate(to_counts(win_hand), TILE_INDEX[drawn],
                                   chain_count=chain_count[seat], meld_groups=meld_groups)
                if result:
                    if drawn == JOKER and result.get("baotou"):
                        snapshot = {
                            "drawn_tile": drawn, "chain_count": chain_count[seat], "piao": 0,
                            "seat": seat, "dealer": dealer, "melds": melds,
                            "wall_remaining": len(tiles),
                        }
                        decision = strategies[seat].choose_hu_or_piao(snapshot, result)
                        if decision and decision.get("action") == "discard":
                            hand.remove(drawn)
                            chain_count[seat] += 1
                            # 简化：不模拟抓打圈限制，其余三家按正常响应
                            # 窗口处理这张弃牌（财神天生不可被吃/碰，见
                            # joker_claim_restriction，故 resolve_claim 恒
                            # 返回 (None, None, None)）。
                            resolve_claim(seat, drawn, hands, melds, strategies, len(tiles))
                            turn = (seat + 1) % 4
                            must_discard = False
                            continue
                    return {"winner": seat, "dealer": dealer, "fan": result["fan"], "draws": draws}
        hand = hands[seat]
        if not hand:
            return None
        discard = strategies[seat].discard(hand, meld_groups, seat == dealer)
        if discard not in hand:
            discard = hand[-1]
        hand.remove(discard)
        claimed, claim_kind, claim_tiles = resolve_claim(seat, discard, hands, melds, strategies, len(tiles))
        if claimed is not None:
            for name in claim_tiles:
                hands[claimed].remove(name)
            melds[claimed].append({"kind": claim_kind, "tiles": sorted(claim_tiles + [discard])})
        if claimed is None:
            turn = (seat + 1) % 4
            must_discard = False
        else:
            turn = claimed
            must_discard = True


def run_session(rng, strategies, hands_per_batch=8, batches=1):
    """[LEGACY / 非官方评测结果] 旧版跨批会话：一个 ``rng`` 贯穿全部
    ``hands_per_batch * batches`` 手，scores/dealer 在批次之间连续延续，
    不做批次边界重置。已知偏差（阶段3评审确认）：上一批的赢家会影响
    下一批的初庄，随机序列在批次之间没有独立性。

    仅为向后兼容保留（``compare()``/``baotou_grid()`` 仍然调用它），
    **不得**把这里的输出当作正式 A/B 评测结论——官方入口见
    ``run_arena()``（独立批次语义 + balanced seat schedule + 确定性
    per-hand seed，见模块顶部 EVALUATOR_SCHEMA_VERSION 说明）。"""
    scores = [0, 0, 0, 0]
    dealer = 0
    stats = Counter()
    fans = Counter()
    for _ in range(hands_per_batch * batches):
        result = play_hand(rng, strategies, dealer)
        if result is None:
            stats["draws"] += 1
            continue
        stats[f"win_{result['winner']}"] += 1
        fans[result["fan"]] += 1
        settle(result["winner"], result["dealer"], result["fan"], scores)
        dealer = result["winner"]
    return scores, stats, fans


def compare(kind_a, kind_b, claims_a="all", claims_b="all", batches=40, seed=20260916, weights_a=None):
    """[LEGACY / 非官方评测结果] 向后兼容入口，保留旧签名供
    ``tools/baohua_experiment.py``、``tools/slow1_experiment.py``、
    ``baotou_grid()`` 继续调用。已知偏差：A 固定坐 0/1 号座位、B 固定坐
    2/3 号座位（固定席位偏差），且全程共用同一个 ``random.Random``
    实例（``run_session`` 的跨批语义，见其文档）。

    这些偏差正是本轮重建要解决的问题（见需求一/二/三），因此本函数的
    输出**不得**作为正式 A/B 评测结论使用——正式评测请用
    ``run_arena()``。"""
    rng = random.Random(seed)
    strategies = [
        SeatStrategy(kind_a, claims_a, weights_a), SeatStrategy(kind_a, claims_a, weights_a),
        SeatStrategy(kind_b, claims_b), SeatStrategy(kind_b, claims_b),
    ]
    scores, stats, fans = run_session(rng, strategies, hands_per_batch=8, batches=batches)
    total_hands = 8 * batches - stats["draws"]
    return {
        "a": f"{kind_a}/{claims_a}", "b": f"{kind_b}/{claims_b}",
        "scores": scores,
        "a_score": scores[0] + scores[1], "b_score": scores[2] + scores[3],
        "a_win_rate": round((stats["win_0"] + stats["win_1"]) / total_hands, 3),
        "b_win_rate": round((stats["win_2"] + stats["win_3"]) / total_hands, 3),
        "draws": stats["draws"], "fans": dict(fans),
    }


# ============================================================
# 离线部分规则评测入口（v2）：独立批次语义 + balanced seat schedule +
# 确定性 per-hand seed + 可信统计输出。见类/函数级文档。本评测器只模拟
# 部分官方硬规则（见 RULE_COVERAGE），不是官方规则的完整实现，也不能
# 替代生产环境的完整校验。
# ============================================================

class ScheduleValidationError(Exception):
    """validate_schedule() 发现排班/seed 设计不满足公平性要求时抛出。
    离线部分规则评测（run_arena）遇到此异常必须让调用方（含 CLI）非零
    退出，不允许把不均衡排班下产出的结果当作正式结论。"""


# 4 种 balanced seat 阵型：每种阵型内 A/B 各占 2 个座位（座位号
# 0/1/2/3），四种阵型合起来覆盖 AABB/BBAA/ABAB/BABA，且对每个具体座位
# 号，四种阵型中恰好 2 次分给 A、2 次分给 B（座位维度均衡，见
# validate_schedule 的数值证明）。
SEAT_PATTERNS = (
    ("A", "A", "B", "B"),
    ("B", "B", "A", "A"),
    ("A", "B", "A", "B"),
    ("B", "A", "B", "A"),
)

# schedule_id（4 种阵型）与 initial_dealer_seat（0/1/2/3 座位号）各自
# 独立循环，两者以 16 为周期做满笛卡尔积（4x4）：每种阵型搭配每个初始
# 庄家座位号恰好出现一次。因为每种阵型里 4 个座位对 A/B 恰好 2:2，对
# 固定阵型遍历 4 个庄家座位号时，"初始庄家所在座位的策略" 必然是 A
# 两次、B 两次；对 4 种阵型求和，16 批里 A/B 各拿到 8 次初始庄。这就是
# "初庄必须按 0/1/2/3 均衡轮换"（座位维度）与"A/B 初始庄次数相等"
# （策略维度）同时成立的构造性证明。batches 必须是该周期的正整数倍，
# 否则均衡在批次边界处无法收口。
SCHEDULE_PERIOD = len(SEAT_PATTERNS) * 4  # 16


def seat_pattern_for_batch(batch_id):
    """返回 (schedule_id, pattern, initial_dealer_seat)，纯函数、只依赖
    batch_id，不依赖任何运行时状态或此前批次的结果。"""
    schedule_id = batch_id % len(SEAT_PATTERNS)
    initial_dealer_seat = (batch_id // len(SEAT_PATTERNS)) % 4
    return schedule_id, SEAT_PATTERNS[schedule_id], initial_dealer_seat


def derive_hand_seed(root_seed, batch_id, hand_id, schedule_id):
    """需求三：每一手牌使用由 root_seed/batch_id/hand_id/schedule_id
    确定性派生的独立 seed——纯函数（同输入总是同输出），不读取/写入
    任何全局或跨调用状态，因此"上一手策略行为"（无论输赢、声明与否）
    不可能改变"下一手"的派生结果，天然保证手与手之间的牌墙独立。

    用 SHA-256 而非简单加法/异或组合，避免小整数输入下的弱雪崩效应
    （例如 batch_id/hand_id 相邻时 seed 也几乎相邻，可能引入不易察觉的
    序列相关性）。截取前 8 字节转成非负整数喂给 ``random.Random``。"""
    material = f"{root_seed}:{batch_id}:{hand_id}:{schedule_id}".encode("utf-8")
    digest = hashlib.sha256(material).digest()
    return int.from_bytes(digest[:8], "big")


def _instantiate_seats(pattern, kind_a, claims_a, weights_a, kind_b, claims_b, weights_b):
    """按给定 pattern（4 元组，元素为 'A'/'B'）在 4 个物理座位上实例化
    SeatStrategy，返回 (strategies, policy_by_seat)。纯函数，不涉及
    batch_id/schedule 选择逻辑，供 build_seat_assignment() 与镜像配对
    (mirror pairing) 共用。"""
    strategies, policy_by_seat = [], []
    for role in pattern:
        if role == "A":
            strategies.append(SeatStrategy(kind_a, claims_a, weights_a))
            policy_by_seat.append("a")
        else:
            strategies.append(SeatStrategy(kind_b, claims_b, weights_b))
            policy_by_seat.append("b")
    return strategies, policy_by_seat


def _mirror_pattern(pattern):
    """把 pattern 中每个座位的 A/B 互换，物理座位号不变——用于镜像配对
    实验的 session_mirror：同一批物理座位，交换 A/B 两个策略。"""
    return tuple("B" if role == "A" else "A" for role in pattern)


def build_seat_assignment(batch_id, kind_a, claims_a, weights_a, kind_b, claims_b, weights_b):
    """按 batch_id 对应的阵型实例化 4 个座位的 SeatStrategy，并返回每个
    座位归属哪个策略标签（'a'/'b'），供后续按 seat/policy 双维度统计。"""
    schedule_id, pattern, initial_dealer_seat = seat_pattern_for_batch(batch_id)
    strategies, policy_by_seat = _instantiate_seats(
        pattern, kind_a, claims_a, weights_a, kind_b, claims_b, weights_b)
    return strategies, policy_by_seat, initial_dealer_seat, schedule_id


def derive_paired_hand_seed(root_seed, batch_id, hand_id):
    """需求二.3：镜像配对实验专用的牌墙 seed 派生——纯函数，只依赖
    (root_seed, batch_id, hand_id)，**不包含** schedule_id、pattern、
    forward/mirror 方向、A/B 标签中的任何一个。因此同一个 paired batch
    里的 session_forward 与 session_mirror 天然共享完全相同的逐手牌墙
    seed 序列（调用时用同一个 batch_id），互换 A/B 标签（即改变传入的
    kind_a/kind_b）也不会改变这里派生出的 seed——这是"座位噪声真正被
    结构性抵消"而非"偶然符号翻转"的前提。"""
    material = f"paired:{root_seed}:{batch_id}:{hand_id}".encode("utf-8")
    digest = hashlib.sha256(material).digest()
    return int.from_bytes(digest[:8], "big")


def run_batch(root_seed, batch_id, kind_a, claims_a, weights_a, kind_b, claims_b, weights_b,
              hands_per_batch):
    """需求一：单个 batch 完全独立——scores/dealer/统计全部是本函数的
    局部变量，不读取任何模块级/闭包可变状态，也不接受"上一批结果"作为
    输入；纯函数（只依赖显式参数），因此天然满足"batch 之间状态独立"
    与"相同输入重复调用结果一致"。"""
    strategies, policy_by_seat, dealer, schedule_id = build_seat_assignment(
        batch_id, kind_a, claims_a, weights_a, kind_b, claims_b, weights_b)
    scores = [0, 0, 0, 0]
    seat_stats = [{"score": 0, "wins": 0, "dealer_hands": 0, "draws": 0} for _ in range(4)]
    fans = Counter()
    draws_total = 0
    for hand_id in range(hands_per_batch):
        seed = derive_hand_seed(root_seed, batch_id, hand_id, schedule_id)
        rng = random.Random(seed)
        seat_stats[dealer]["dealer_hands"] += 1
        result = play_hand(rng, strategies, dealer)
        if result is None:
            draws_total += 1
            for seat in range(4):
                seat_stats[seat]["draws"] += 1
            continue  # 流局庄家连庄（dealer 不变）——下一手仍用独立派生 seed。
        winner = result["winner"]
        fans[result["fan"]] += 1
        settle(winner, result["dealer"], result["fan"], scores)
        seat_stats[winner]["wins"] += 1
        dealer = winner
    for seat in range(4):
        seat_stats[seat]["score"] = scores[seat]
    a_score = sum(scores[s] for s in range(4) if policy_by_seat[s] == "a")
    b_score = sum(scores[s] for s in range(4) if policy_by_seat[s] == "b")
    a_wins = sum(seat_stats[s]["wins"] for s in range(4) if policy_by_seat[s] == "a")
    b_wins = sum(seat_stats[s]["wins"] for s in range(4) if policy_by_seat[s] == "b")
    a_dealer_hands = sum(seat_stats[s]["dealer_hands"] for s in range(4) if policy_by_seat[s] == "a")
    b_dealer_hands = sum(seat_stats[s]["dealer_hands"] for s in range(4) if policy_by_seat[s] == "b")
    return {
        "batch_id": batch_id, "schedule_id": schedule_id, "policy_by_seat": policy_by_seat,
        "seat_stats": seat_stats, "scores": scores,
        "a_score": a_score, "b_score": b_score, "score_delta": a_score - b_score,
        "a_wins": a_wins, "b_wins": b_wins,
        "a_dealer_hands": a_dealer_hands, "b_dealer_hands": b_dealer_hands,
        "draws": draws_total, "hands": hands_per_batch, "fans": dict(fans),
    }


def validate_schedule(batches, hands_per_batch=1, root_seed=20260916,
                       policy_a="base", claims_a="all", weights_a=None,
                       policy_b="base", claims_b="all", weights_b=None):
    """需求五：评测公平性自检，供 run_arena() 在正式评测前自动调用，
    也可单独调用。任何一项失败都抛出 ScheduleValidationError（非零
    退出的调用方应捕获此异常并以非零码退出，不得把结果当正式结论）。

    检查项（需求三：只检查 initial_dealer_opportunity——排班/阵型决定
    的结构性初庄机会，不检查 realized_dealer_hands——那是比赛结果决定
    的，不要求相等，也不属于"排班公平性"的范畴）：
    1. 每个策略的 seat 次数相等；
    2. 每个策略的 initial_dealer_opportunity 相等；
    3. 全部实验单元 seed 无重复；
    4. 同一 (root_seed,batch_id,hand_id) 重复派生 paired seed 一致
       （seed 派生函数是确定性纯函数）；
    5. paired batch 之间状态独立——同一 batch_id 单独跑一次 run_paired_
       batch()、与跑在另一个不同 batch_id 之后再跑一次，结果必须逐
       字段一致（验证的是 run_arena() 实际使用的入口，而不是仅供向后
       兼容的 legacy run_batch()）。
    """
    problems = []
    if batches <= 0 or batches % SCHEDULE_PERIOD != 0:
        problems.append(
            f"batches={batches} 不是 SCHEDULE_PERIOD({SCHEDULE_PERIOD}) 的正整数倍，"
            f"座位/庄家次数无法在批次边界处均衡收口"
        )
    else:
        seat_count = {"a": 0, "b": 0}
        initial_dealer_opportunity = {"a": 0, "b": 0}
        for batch_id in range(batches):
            _schedule_id, pattern, dealer_seat = seat_pattern_for_batch(batch_id)
            mirror_pattern = _mirror_pattern(pattern)
            for role in pattern:
                seat_count[role.lower()] += 1
            # 每个 paired batch 里 session_forward/session_mirror 在同一
            # 物理初庄座位上的角色互补，天然各贡献 1 次初庄机会给 A、
            # 1 次给 B——见 run_paired_batch() 的构造性证明。
            initial_dealer_opportunity[pattern[dealer_seat].lower()] += 1
            initial_dealer_opportunity[mirror_pattern[dealer_seat].lower()] += 1
        if seat_count["a"] != seat_count["b"]:
            problems.append(f"座位次数不均衡: a={seat_count['a']} b={seat_count['b']}")
        if initial_dealer_opportunity["a"] != initial_dealer_opportunity["b"]:
            problems.append(
                f"initial_dealer_opportunity 不均衡: "
                f"a={initial_dealer_opportunity['a']} b={initial_dealer_opportunity['b']}"
            )

    seen_seeds = set()
    duplicate = False
    probe_batches = batches if 0 < batches <= SCHEDULE_PERIOD else SCHEDULE_PERIOD
    for batch_id in range(probe_batches):
        for hand_id in range(max(1, hands_per_batch)):
            seed = derive_paired_hand_seed(root_seed, batch_id, hand_id)
            if seed in seen_seeds:
                duplicate = True
            seen_seeds.add(seed)
    if duplicate:
        problems.append("检测到重复的实验单元 seed（batch_id/hand_id 组合派生冲突）")

    probe_seed_1 = derive_paired_hand_seed(root_seed, 0, 0)
    probe_seed_2 = derive_paired_hand_seed(root_seed, 0, 0)
    if probe_seed_1 != probe_seed_2:
        problems.append("seed 派生函数不是确定性的（同输入产出不同 seed）")

    probe_hands = min(max(1, hands_per_batch), 2)
    result_alone = run_paired_batch(root_seed, 0, policy_a, claims_a, weights_a,
                                     policy_b, claims_b, weights_b, probe_hands)
    if batches > 1:
        run_paired_batch(root_seed, (0 + 1) % max(batches, 2), policy_a, claims_a, weights_a,
                          policy_b, claims_b, weights_b, probe_hands)
    result_after_other = run_paired_batch(root_seed, 0, policy_a, claims_a, weights_a,
                                           policy_b, claims_b, weights_b, probe_hands)
    if result_alone != result_after_other:
        problems.append("run_paired_batch() 结果依赖执行顺序/跨批状态，paired batch 独立性校验失败")

    if problems:
        raise ScheduleValidationError("; ".join(problems))
    return {"ok": True, "batches": batches, "schedule_period": SCHEDULE_PERIOD,
            "hands_probed": probe_hands}


_Z_SCORE_95 = 1.959963984540054  # 标准正态分布 97.5% 分位数（stdlib 无 scipy，写死常量）


def _mean_ci95(deltas):
    """需求四：以独立采样单位（自本轮起改为 paired batch 的
    ``paired_delta``，而非单个 session batch 的 score_delta——见需求二.5）
    为样本，用显式均值置信区间公式（正态近似，纯 stdlib，不依赖
    scipy）：se = sample_std / sqrt(n)，ci95 = mean ± 1.959964 * se。

    样本不足（n<2，方差无法估计）时明确返回 unavailable + 原因，不允许
    输出伪精度；结果不含 NaN/Infinity（除法前已保证分母>=1）。"""
    n = len(deltas)
    mean = sum(deltas) / n if n else None
    if n < 2:
        return {
            "mean": mean, "n": n, "se": None,
            "ci95": {"status": "unavailable",
                     "reason": f"insufficient_batches: need at least 2 independent paired batches "
                               f"to estimate variance, got {n}"},
        }
    variance = sum((x - mean) ** 2 for x in deltas) / (n - 1)
    se = variance ** 0.5 / (n ** 0.5)
    return {
        "mean": mean, "n": n, "se": se,
        "ci95": {"status": "ok", "low": mean - _Z_SCORE_95 * se, "high": mean + _Z_SCORE_95 * se,
                 "method": "normal_approx"},
    }


def _conclusion_from_ci(ci):
    """需求四：统计结论必须诚实——绝不允许根据单次 score_delta 的正负
    宣称策略稳定胜出，一律从置信区间边界推导：
    - CI 不可用（样本不足）: insufficient_samples；
    - CI 下界 > 0: a_better；
    - CI 上界 < 0: b_better；
    - CI 跨 0（下界<=0<=上界）: inconclusive。"""
    ci95 = ci["ci95"]
    if ci95["status"] == "unavailable":
        return "insufficient_samples"
    if ci95["low"] > 0:
        return "a_better"
    if ci95["high"] < 0:
        return "b_better"
    return "inconclusive"


def _run_session(root_seed, batch_id, pattern, initial_dealer_seat,
                  kind_a, claims_a, weights_a, kind_b, claims_b, weights_b, hands_per_batch):
    """按给定物理座位 pattern（A/B 分配）与 initial_dealer_seat 独立跑一个
    session：scores/dealer 只在本 session 内部演化，session 之间互不
    影响。每一手的牌墙 seed 用 derive_paired_hand_seed(root_seed,
    batch_id, hand_id)——只依赖 (root_seed,batch_id,hand_id)，不含
    pattern/方向/A-B 标签，因此 forward/mirror 两个 session（乃至
    A/B 互换后的 forward'/mirror'）在同一个 batch_id 下始终共享完全
    相同的逐手牌墙 seed 序列。"""
    strategies, policy_by_seat = _instantiate_seats(
        pattern, kind_a, claims_a, weights_a, kind_b, claims_b, weights_b)
    scores = [0, 0, 0, 0]
    seat_stats = [{"score": 0, "wins": 0, "dealer_hands": 0, "draws": 0} for _ in range(4)]
    fans = Counter()
    draws_total = 0
    dealer = initial_dealer_seat
    for hand_id in range(hands_per_batch):
        seed = derive_paired_hand_seed(root_seed, batch_id, hand_id)
        rng = random.Random(seed)
        seat_stats[dealer]["dealer_hands"] += 1
        result = play_hand(rng, strategies, dealer)
        if result is None:
            draws_total += 1
            for seat in range(4):
                seat_stats[seat]["draws"] += 1
            continue  # 流局庄家连庄——下一手仍用独立派生 seed。
        winner = result["winner"]
        fans[result["fan"]] += 1
        settle(winner, result["dealer"], result["fan"], scores)
        seat_stats[winner]["wins"] += 1
        dealer = winner
    for seat in range(4):
        seat_stats[seat]["score"] = scores[seat]
    a_score = sum(scores[s] for s in range(4) if policy_by_seat[s] == "a")
    b_score = sum(scores[s] for s in range(4) if policy_by_seat[s] == "b")
    a_wins = sum(seat_stats[s]["wins"] for s in range(4) if policy_by_seat[s] == "a")
    b_wins = sum(seat_stats[s]["wins"] for s in range(4) if policy_by_seat[s] == "b")
    a_dealer_hands = sum(seat_stats[s]["dealer_hands"] for s in range(4) if policy_by_seat[s] == "a")
    b_dealer_hands = sum(seat_stats[s]["dealer_hands"] for s in range(4) if policy_by_seat[s] == "b")
    initial_dealer_policy = policy_by_seat[initial_dealer_seat]
    return {
        "policy_by_seat": policy_by_seat, "seat_stats": seat_stats, "scores": scores,
        "a_score": a_score, "b_score": b_score, "score_delta": a_score - b_score,
        "a_wins": a_wins, "b_wins": b_wins,
        "a_dealer_hands": a_dealer_hands, "b_dealer_hands": b_dealer_hands,
        "initial_dealer_policy": initial_dealer_policy,
        "draws": draws_total, "fans": dict(fans),
    }


def run_paired_batch(root_seed, batch_id, kind_a, claims_a, weights_a, kind_b, claims_b, weights_b,
                      hands_per_batch):
    """需求二：真正的镜像配对实验——一个 paired batch = 两个镜像
    session：
    - session_forward：按 SEAT_PATTERNS[schedule_id] 放置 A/B；
    - session_mirror：同一批物理座位，把 A/B 互换（_mirror_pattern）。

    两个 session 共享相同的逐手牌墙 seed 序列（derive_paired_hand_seed，
    与 A/B 标签、forward/mirror 方向无关）与相同的 initial_dealer_seat，
    但 scores/dealer 各自独立演化（``_run_session`` 内部局部变量，互不
    读写）。

    paired_delta 定义：两个镜像 session 合并后的策略差值，即
    ``forward.score_delta + mirror.score_delta``（等价于
    ``(forward.a_score+mirror.a_score) - (forward.b_score+mirror.b_score)``）。

    构造性证明（详见模块内测试）：
    - 同一策略 A/B 对战（kind_a==kind_b 等）时，forward/mirror 两个
      session 在相同种子/庄家下是完全相同的手牌演化，唯一区别只是
      "A/B 标签贴在哪两个物理座位"——forward.score_delta 与
      mirror.score_delta 逐手严格互为相反数，paired_delta 严格等于 0。
    - 交换 policy_a/policy_b 输入后，新 forward' 的座位放置与旧
      mirror 完全相同、新 mirror' 的座位放置与旧 forward 完全相同
      （因为 _mirror_pattern 是对合操作），所以
      paired_delta' == -paired_delta（逐 batch 严格反号，非仅符号）。
    - 每个 paired batch 里，initial_dealer_seat 在 forward 中归属的
      policy 与在 mirror 中归属的 policy 互补（_mirror_pattern 保证
      每个座位角色必然翻转），因此 initial_dealer_opportunity 在
      任意单个 paired batch 内部就已经严格 a=1,b=1，天然均衡，不依赖
      比赛结果（对应需求三：只由排班决定）。
    """
    schedule_id, pattern, initial_dealer_seat = seat_pattern_for_batch(batch_id)
    mirror_pattern = _mirror_pattern(pattern)
    forward = _run_session(root_seed, batch_id, pattern, initial_dealer_seat,
                            kind_a, claims_a, weights_a, kind_b, claims_b, weights_b, hands_per_batch)
    mirror = _run_session(root_seed, batch_id, mirror_pattern, initial_dealer_seat,
                           kind_a, claims_a, weights_a, kind_b, claims_b, weights_b, hands_per_batch)
    initial_dealer_opportunity = {"a": 0, "b": 0}
    initial_dealer_opportunity[forward["initial_dealer_policy"]] += 1
    initial_dealer_opportunity[mirror["initial_dealer_policy"]] += 1
    fans_total = Counter(forward["fans"])
    fans_total.update(mirror["fans"])
    return {
        "batch_id": batch_id, "schedule_id": schedule_id,
        "forward": forward, "mirror": mirror,
        "paired_delta": forward["score_delta"] + mirror["score_delta"],
        "a_score": forward["a_score"] + mirror["a_score"],
        "b_score": forward["b_score"] + mirror["b_score"],
        "a_wins": forward["a_wins"] + mirror["a_wins"],
        "b_wins": forward["b_wins"] + mirror["b_wins"],
        "initial_dealer_opportunity": initial_dealer_opportunity,
        "realized_dealer_hands": {
            "a": forward["a_dealer_hands"] + mirror["a_dealer_hands"],
            "b": forward["b_dealer_hands"] + mirror["b_dealer_hands"],
        },
        "draws": forward["draws"] + mirror["draws"],
        "hands": hands_per_batch, "fans": dict(fans_total),
    }


def _build_report(policy_a, claims_a, policy_b, claims_b, root_seed, hands_per_batch,
                   paired_batches, paired_results):
    deltas = [p["paired_delta"] for p in paired_results]
    ci = _mean_ci95(deltas)
    matches_per_pair = 2
    total_matches = paired_batches * matches_per_pair
    total_hands = hands_per_batch * total_matches
    total_draws = sum(p["draws"] for p in paired_results)
    decided_hands = total_hands - total_draws
    a_wins_total = sum(p["a_wins"] for p in paired_results)
    b_wins_total = sum(p["b_wins"] for p in paired_results)
    initial_dealer_total = {
        "a": sum(p["initial_dealer_opportunity"]["a"] for p in paired_results),
        "b": sum(p["initial_dealer_opportunity"]["b"] for p in paired_results),
    }
    realized_dealer_total = {
        "a": sum(p["realized_dealer_hands"]["a"] for p in paired_results),
        "b": sum(p["realized_dealer_hands"]["b"] for p in paired_results),
    }
    fans_total = Counter()
    for p in paired_results:
        fans_total.update(p["fans"])
    seat_breakdown = {
        "a": [{"score": 0, "wins": 0, "dealer_hands": 0, "draws": 0} for _ in range(4)],
        "b": [{"score": 0, "wins": 0, "dealer_hands": 0, "draws": 0} for _ in range(4)],
    }
    for p in paired_results:
        for session in (p["forward"], p["mirror"]):
            for seat in range(4):
                policy = session["policy_by_seat"][seat]
                bucket = seat_breakdown[policy][seat]
                src = session["seat_stats"][seat]
                bucket["score"] += src["score"]
                bucket["wins"] += src["wins"]
                bucket["dealer_hands"] += src["dealer_hands"]
                bucket["draws"] += src["draws"]
    return {
        "evaluator_schema_version": EVALUATOR_SCHEMA_VERSION,
        "build_hash": build_hash(),
        "policy_a": f"{policy_a}/{claims_a}", "policy_b": f"{policy_b}/{claims_b}",
        "root_seed": root_seed,
        "simulation_scope": "partial_rules",
        "official_fidelity": False,
        "rule_coverage": dict(RULE_COVERAGE),
        "paired_batches": paired_batches, "matches_per_pair": matches_per_pair,
        "total_matches": total_matches, "walls_per_pair": hands_per_batch,
        "hands_per_batch": hands_per_batch,
        "experiment_units": total_hands,
        "paired_delta_definition": (
            "paired_delta = session_forward.score_delta + session_mirror.score_delta, "
            "where session_forward/session_mirror share the identical per-hand wall seed "
            "sequence and initial dealer seat (derived only from root_seed/batch_id/hand_id, "
            "independent of A/B labels or forward/mirror direction) but evolve independent "
            "scores/dealer state; equivalently (forward.a_score+mirror.a_score) - "
            "(forward.b_score+mirror.b_score)."
        ),
        "batch_score_delta": deltas,
        "score_delta_mean": ci["mean"], "score_delta_se": ci["se"], "score_delta_ci95": ci["ci95"],
        "conclusion": _conclusion_from_ci(ci),
        "conclusion_scope": (
            "This conclusion (A/B score_delta comparison) is only valid under the rules "
            "actually simulated by this evaluator; see rule_coverage. Rules with "
            "rule_coverage=false (gang, catch_play_circle) are not modeled; chain_piao is "
            "only partial (see RULE_COVERAGE comment) and may be optimistic and "
            "may change real outcomes in production."
        ),
        "win_rate": {
            "a": (a_wins_total / decided_hands) if decided_hands else None,
            "b": (b_wins_total / decided_hands) if decided_hands else None,
        },
        "draw_rate": (total_draws / total_hands) if total_hands else None,
        "fans": dict(fans_total),
        "seat_breakdown": seat_breakdown,
        "initial_dealer_opportunity": initial_dealer_total,
        "realized_dealer_hands": realized_dealer_total,
        "draws": total_draws,
    }


def run_arena(policy_a, policy_b, hands_per_batch=8, batches=SCHEDULE_PERIOD, root_seed=20260916,
              claims_a="all", claims_b="all", weights_a=None, weights_b=None, validate=True,
              progress=False):
    """离线部分规则评测入口（v2）：解决固定席位、固定初庄、跨批状态泄漏、
    随机序列漂移、"伪镜像配对"（需求二）、"初庄/实际庄家混淆"（需求三），
    以及本轮修复的两项 P0 硬规则违例（财神响应限制 / 两摊吃上限）。

    诚实的能力边界（务必阅读）：本函数不是官方规则的完整实现，只模拟
    RULE_COVERAGE 中标记为 True 的规则子集（self_draw/chi_peng_priority/
    joker_claim_restriction/max_two_chi）；杠、抓打圈、连庄/漂分未模拟
    （见 RULE_COVERAGE 与返回报告中的 rule_coverage 字段）。返回报告的
    ``official_fidelity`` 恒为 False、``simulation_scope`` 恒为
    "partial_rules"，``conclusion_scope`` 显式声明结论只在已模拟规则下
    成立——不得据此声称"官方规则下的胜负结论"。

    - 每个 paired batch 用 run_paired_batch() 独立跑，内含 session_
      forward/session_mirror 两个真正镜像的 session（需求二）；
    - 座位用 SEAT_PATTERNS 按 batch_id 循环分配，balanced seat schedule；
    - 每一手用 derive_paired_hand_seed() 派生独立 seed，forward/mirror
      共享同一序列（需求二.2/二.3）；
    - CI 的采样单位是 paired_delta（需求二.5）；
    - 返回统一的可信统计报告，含 CI/conclusion/build_hash/schema_version
      （需求四），以及 initial_dealer_opportunity 与 realized_dealer_
      hands 的显式区分（需求三）。

    ``batches``（此处即 paired_batches 数量）必须是 SCHEDULE_PERIOD(16)
    的正整数倍，否则座位/庄家均衡在批次边界处无法收口——直接抛
    ValueError，不允许在不均衡排班下产出"正式评测结果"。
    ``validate=True``（默认）时先跑一遍 validate_schedule() 自检，
    自检失败直接抛 ScheduleValidationError。"""
    if batches <= 0 or batches % SCHEDULE_PERIOD != 0:
        raise ValueError(
            f"batches must be a positive multiple of SCHEDULE_PERIOD({SCHEDULE_PERIOD}) "
            f"for a balanced seat/dealer schedule, got {batches}"
        )
    if validate:
        validate_schedule(batches=batches, hands_per_batch=hands_per_batch, root_seed=root_seed,
                           policy_a=policy_a, claims_a=claims_a, weights_a=weights_a,
                           policy_b=policy_b, claims_b=claims_b, weights_b=weights_b)
    paired_results = []
    for batch_id in range(batches):
        paired_results.append(run_paired_batch(root_seed, batch_id, policy_a, claims_a, weights_a,
                                               policy_b, claims_b, weights_b, hands_per_batch))
        if progress and ((batch_id + 1) % 16 == 0 or batch_id + 1 == batches):
            print("  arena 进度 %d / %d 批" % (batch_id + 1, batches), flush=True)
    return _build_report(policy_a, claims_a, policy_b, claims_b, root_seed, hands_per_batch,
                          batches, paired_results)


def baotou_grid(batches=40):
    grid = [
        {"tag": "production", "weights": {}},
        {"tag": "baotou_x05", "weights": {"baotou_all_wait": 1500, "baotou_no_slow": 750, "baotou_faster": 1100, "baotou_slow1": 1500, "baotou_slow1_dealer": 4500}},
        {"tag": "baotou_x2", "weights": {"baotou_all_wait": 6000, "baotou_no_slow": 3000, "baotou_faster": 4400, "baotou_slow1": 6000, "baotou_slow1_dealer": 18000}},
        {"tag": "no_pair_route", "weights": {"pair_route": 0}},
        {"tag": "pair_route_x2", "weights": {"pair_route": 1200}},
    ]
    results = []
    for item in grid:
        row = compare("current", "current", claims_a="real", claims_b="real", batches=batches, weights_a=item["weights"])
        results.append({"tag": item["tag"], **{k: row[k] for k in ("a_score", "b_score", "a_win_rate", "b_win_rate")}})
    return results


if __name__ == "__main__":
    import json

    if len(sys.argv) > 1 and sys.argv[1] == "grid":
        for row in baotou_grid(batches=int(sys.argv[2]) if len(sys.argv) > 2 else 40):
            print(json.dumps(row, ensure_ascii=False))
    else:
        pairs = [
            ("base", "loose", "base", "real"),
            ("base", "loose", "base", "all"),
            ("base", "loose", "base", "peng"),
        ]
        for kind_a, claims_a, kind_b, claims_b in pairs:
            print(json.dumps(compare(kind_a, kind_b, claims_a, claims_b), ensure_ascii=False))
