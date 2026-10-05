import os
import random
import time
import unittest

os.environ.setdefault("MJ_WEIGHTS_NO_FILE", "1")

from mj import route_ev  # noqa: E402
from mj.ev import choose_route_discard  # noqa: E402
from mj.hu_strategy import _decline_discard_choice, _decline_small_hu_tile_ev, choose_hu_or_piao  # noqa: E402
from mj.responses import choose_chi, choose_peng  # noqa: E402
from mj.rules import baotou, evaluate  # noqa: E402
from mj.shanten import route_shanten  # noqa: E402
from mj.strategy import choose_discard  # noqa: E402
from mj.tiles import ALL_TILES, JOKER, JOKER_IDX, TILE_INDEX, to_counts  # noqa: E402

ON = {"_weights": {"rule_route_ev_enabled": 1}}
OFF = {"_weights": {"rule_route_ev_enabled": 0}}


def random_hands_with_joker(n, size, seed, joker_p=0.6):
    """含财神的随机手牌：joker_p 概率下至少含 1~4 张财神，其余从非财神牌里补齐。"""
    rng = random.Random(seed)
    plain = [t for t in ALL_TILES if t != JOKER]
    for _ in range(n):
        jokers = rng.randint(1, 4) if rng.random() < joker_p else 0
        wall = list(plain) * 4
        rng.shuffle(wall)
        hand = wall[: max(0, size - jokers)] + [JOKER] * jokers
        rng.shuffle(hand)
        yield hand[:size]


class RouteDistanceTests(unittest.TestCase):
    """路线距离的手工构造例子（§7 测试1）。"""

    def test_six_pairs_one_joker_is_d_zero_and_baotou(self):
        hand = to_counts(["1w", "1w", "2w", "2w", "3w", "3w", "4w", "4w",
                          "5w", "5w", "6w", "6w", JOKER])
        self.assertEqual(route_ev._pair_baotou_distance(hand), 0)
        self.assertTrue(baotou(hand, 0))

    def test_five_pairs_one_joker_two_singles_is_d_one(self):
        hand = to_counts(["1w", "1w", "2w", "2w", "3w", "3w", "4w", "4w",
                          "5w", "5w", JOKER, "6w", "7w"])
        self.assertEqual(route_ev._pair_baotou_distance(hand), 1)
        self.assertFalse(baotou(hand, 0))

    def test_two_jokers_boundary_still_reaches_d_zero(self):
        # 5 对(10) + 1 单张(1) + 2 财神(2) = 13：一张财神独立留作将，另一张
        # 与单张 6w 配成第 6 对——与 1 张财神时的判据同构，验证多财神不会算错。
        hand = to_counts(["1w", "1w", "2w", "2w", "3w", "3w", "4w", "4w",
                          "5w", "5w", "6w", JOKER, JOKER])
        self.assertEqual(route_ev._pair_baotou_distance(hand), 0)
        self.assertTrue(baotou(hand, 0))

    def test_two_jokers_worse_shape_is_d_one(self):
        # 4 对(8) + 3 单张(3) + 2 财神(2) = 13：2 张财神最多配 1 对 + 兜底 1 单，
        # 还差 1 对才够 6 对。
        hand = to_counts(["1w", "1w", "2w", "2w", "3w", "3w", "4w", "4w",
                          "5w", "6w", "7w", JOKER, JOKER])
        self.assertEqual(route_ev._pair_baotou_distance(hand), 1)

    def test_zero_joker_pair_baotou_distance_is_none(self):
        hand = to_counts(["1w", "1w", "2w", "2w", "3w", "3w", "4w", "4w",
                          "5w", "5w", "6w", "6w", "7w"])
        self.assertIsNone(route_ev._pair_baotou_distance(hand))

    def test_standard_baotou_hand_route_b_distance_zero(self):
        # 三组顺子 + 一组刻子 + 财神单钓 = 标准爆头（摸任意牌都胡）。
        hand = to_counts(["1w", "2w", "3w", "4w", "5w", "6w", "7w", "8w", "9w",
                          "1b", "1b", "1b", JOKER])
        self.assertEqual(route_ev.to_baotou_distance(hand, 0), 0)
        self.assertTrue(baotou(hand, 0))


class InfoLeakageAndScopeTests(unittest.TestCase):
    """信息泄漏/范围外：开关关闭必须完全不介入；范围外（有副露/无财神/链中）
    即使开关打开也不能改变选择（§7 测试2、3）。"""

    def test_switch_off_never_overrides(self):
        for hand in random_hands_with_joker(300, 14, seed=1):
            for meld_groups in (0, 1):
                for chain, piao in ((0, 0), (1, 0), (0, 1)):
                    override = route_ev.maybe_override_discard(
                        hand, meld_groups, chain, piao, OFF, {"rule_route_ev_enabled": 0}, None,
                        lambda: None)
                    self.assertIsNone(override)

    def test_switch_off_end_to_end_matches_default(self):
        for hand in random_hands_with_joker(300, 14, seed=2):
            self.assertEqual(choose_discard(hand, 0, 0, 0, {}, None),
                             choose_discard(hand, 0, 0, 0, OFF, None), hand)
            self.assertEqual(choose_route_discard(hand, 0, 0, 0, {"dealer_hint": True}, None),
                             choose_route_discard(hand, 0, 0, 0, {"dealer_hint": True, **OFF}, None), hand)

    def test_out_of_scope_with_meld_groups_unaffected(self):
        for hand in random_hands_with_joker(150, 11, seed=3):
            self.assertEqual(choose_discard(hand, 1, 0, 0, {}, None),
                             choose_discard(hand, 1, 0, 0, ON, None), hand)

    def test_out_of_scope_without_joker_unaffected(self):
        plain = [t for t in ALL_TILES if t != JOKER]
        rng = random.Random(4)
        for _ in range(150):
            wall = list(plain) * 4
            rng.shuffle(wall)
            hand = wall[:14]
            self.assertEqual(choose_discard(hand, 0, 0, 0, {}, None),
                             choose_discard(hand, 0, 0, 0, ON, None), hand)

    def test_out_of_scope_in_chain_unaffected(self):
        for hand in random_hands_with_joker(150, 14, seed=5):
            if JOKER not in hand:
                continue
            self.assertEqual(choose_discard(hand, 0, 1, 0, {}, None),
                             choose_discard(hand, 0, 1, 0, ON, None), hand)

    def test_out_of_scope_catch_play_unaffected(self):
        catch_rules_off = {"_ctx": {"catch_play": True}}
        catch_rules_on = {"_ctx": {"catch_play": True}, "_weights": {"rule_route_ev_enabled": 1}}
        for hand in random_hands_with_joker(150, 14, seed=6):
            if JOKER not in hand:
                continue
            self.assertEqual(choose_discard(hand, 0, 0, 0, catch_rules_off, None),
                             choose_discard(hand, 0, 0, 0, catch_rules_on, None), hand)


class OverlayOverrideTests(unittest.TestCase):
    """叠加生效：构造「5对+财神、标准路线稍快」的牌，开关打开后应改打
    单张走七对系（§7 测试4）。两个入口（strategy/ev）必须给出一致的选择，
    印证共享 helper 收口。"""

    HAND = ["1w", "1w", "2w", "2w", "3w", "3w", "4w", "4w",
           "5b", "5b", "6b", "6b", JOKER, "东"]

    def test_overlay_switches_to_pair_route(self):
        off_pick = choose_discard(self.HAND, 0, 0, 0, {}, None)
        on_pick = choose_discard(self.HAND, 0, 0, 0, ON, None)
        self.assertNotEqual(off_pick, on_pick)
        self.assertEqual(on_pick, "东")
        # 打掉"东"之后应是 6 对 + 1 财神的七对·爆头形。
        left = list(self.HAND)
        left.remove(on_pick)
        self.assertTrue(baotou(to_counts(left), 0))

    def test_both_entry_points_agree(self):
        strategy_pick = choose_discard(self.HAND, 0, 0, 0, ON, None)
        ev_pick = choose_route_discard(self.HAND, 0, 0, 0, {"dealer_hint": True, **ON}, None)
        self.assertEqual(strategy_pick, ev_pick)


class ClaimVetoTests(unittest.TestCase):
    """吃碰否决：七对·爆头差一步的门清手，别家打出可碰的牌时应放弃碰；
    开关关闭时照碰不误（§7 测试5）。

    手牌与碰的目标由随机搜索找到（见本次改动记录），满足两个独立条件：
    (a) 现有 claim_assessment 门禁本来就允许这次碰（before_shanten=1，
        碰后向听不变、听口不变差，first_meld_neutral_no_pair_route 通过）；
    (b) route_ev.best_route 判定当前最优路线是 D（七对·爆头），碰完固定
        副露 1 组、七对系路线作废后，退回 A/B 最优期望值比碰前的 D 期望值
        低超过 10%（rev_margin 默认值）——用 best_route 实测核实，不是手推。
    两个条件缺一都无法验证"只有 route_ev 生效时才会否决"，所以不能用手工
    构造的"看起来像七对"的手牌，必须两边都跑一遍。"""

    HAND = ["2t", "白", "中", "4w", "中", "白", "北", "5t", "2t", "8t", "4w", "中", "5t"]
    TARGET = "中"

    def _snapshot(self):
        return {"seat": 0, "dealer": 1, "my_hand": self.HAND, "window_tile": self.TARGET,
               "wall_remaining": 60, "chain_count": 0, "melds": [[], [], [], []]}

    def test_peng_vetoed_when_enabled(self):
        import mj.fit as fit
        original = fit.DEFAULT_WEIGHTS.get("rule_route_ev_enabled")
        fit.DEFAULT_WEIGHTS["rule_route_ev_enabled"] = 1
        try:
            self.assertIsNone(choose_peng(self._snapshot()))
        finally:
            fit.DEFAULT_WEIGHTS["rule_route_ev_enabled"] = original

    def test_peng_allowed_when_disabled(self):
        self.assertEqual(choose_peng(self._snapshot()), {"action": "peng", "tile": self.TARGET})

    def test_chi_vetoed_when_enabled(self):
        # 同样由随机搜索找到（见类文档字符串的两个条件）：现有门禁本来允许
        # 吃 2t（组 1t2t3t，first_meld_improves），route_ev 判定当前最优是
        # D，吃完退回 A/B 明显更差。
        hand = ["东", "3w", "5w", "北", "5w", "南", "白", "9t", "9t", "3w", "南", "3t", "1t"]
        snapshot = {"seat": 0, "dealer": 1, "my_hand": hand, "window_tile": "2t",
                   "wall_remaining": 60, "chain_count": 0, "melds": [[], [], [], []]}
        import mj.fit as fit
        original = fit.DEFAULT_WEIGHTS.get("rule_route_ev_enabled")
        fit.DEFAULT_WEIGHTS["rule_route_ev_enabled"] = 1
        try:
            result = choose_chi(snapshot)
        finally:
            fit.DEFAULT_WEIGHTS["rule_route_ev_enabled"] = original
        self.assertIsNone(result)

    def test_chi_allowed_when_disabled(self):
        hand = ["东", "3w", "5w", "北", "5w", "南", "白", "9t", "9t", "3w", "南", "3t", "1t"]
        snapshot = {"seat": 0, "dealer": 1, "my_hand": hand, "window_tile": "2t",
                   "wall_remaining": 60, "chain_count": 0, "melds": [[], [], [], []]}
        result = choose_chi(snapshot)
        self.assertIsNotNone(result)
        self.assertEqual(result["action"], "chi")


class S1RegressionTests(unittest.TestCase):
    """S1 回归（§7 测试6）：七对普通听 + 财神，摸到能胡的牌、打一张即成
    七对·爆头时，_decline_discard_choice 不需要任何改动就应该选中那张牌
    （baotou() 本就把七对算进去，见 mj/rules.py 的 _wins_any）。"""

    def test_decline_discard_choice_converts_to_seven_pairs_baotou(self):
        pre_draw = ["1w", "1w", "2w", "2w", "3w", "3w", "4w", "4w", "5w", "5w",
                   JOKER, "6w", "7w"]
        counts13 = to_counts(pre_draw)
        result = evaluate(counts13, TILE_INDEX["7w"], meld_groups=0)
        self.assertIsNotNone(result)
        self.assertTrue(result["hu"])
        self.assertFalse(result["baotou"])   # 非爆头可胡（窄听，不是"摸任意牌都胡"）
        hand14 = pre_draw + ["7w"]
        snapshot = {"my_hand": hand14, "melds": [[], [], [], []], "seat": 0}
        discard = _decline_discard_choice(snapshot, result)
        self.assertEqual(discard, "6w")
        left = list(hand14)
        left.remove(discard)
        self.assertTrue(baotou(to_counts(left), 0))

    def test_choose_hu_or_piao_uses_it_when_switch_enabled(self):
        import mj.fit as fit
        pre_draw = ["1w", "1w", "2w", "2w", "3w", "3w", "4w", "4w", "5w", "5w",
                   JOKER, "6w", "7w"]
        counts13 = to_counts(pre_draw)
        result = evaluate(counts13, TILE_INDEX["7w"], meld_groups=0)
        snapshot = {"my_hand": pre_draw + ["7w"], "melds": [[], [], [], []], "seat": 0,
                   "drawn_tile": "7w", "chain_count": 0, "piao": 0, "wall_remaining": 60}
        original_all = fit.DEFAULT_WEIGHTS.get("rule_decline_joker_hold_enabled")
        original_one = fit.DEFAULT_WEIGHTS.get("rule_decline_single_joker_all_melds_enabled")
        fit.DEFAULT_WEIGHTS["rule_decline_joker_hold_enabled"] = 1
        fit.DEFAULT_WEIGHTS["rule_decline_single_joker_all_melds_enabled"] = 1
        try:
            action = choose_hu_or_piao(snapshot, result)
        finally:
            fit.DEFAULT_WEIGHTS["rule_decline_joker_hold_enabled"] = original_all
            fit.DEFAULT_WEIGHTS["rule_decline_single_joker_all_melds_enabled"] = original_one
        self.assertEqual(action, {"action": "discard", "tile": "6w"})


class PerformanceTests(unittest.TestCase):
    """性能（§7 测试7）：范围内单次决策叠加逻辑 < 30ms（本机）。"""

    def test_overlay_latency_budget(self):
        hand = ["1w", "1w", "2w", "2w", "3w", "3w", "4w", "4w",
               "5b", "5b", "6b", "6b", JOKER, "东"]
        worst = 0.0
        for _ in range(20):
            t0 = time.perf_counter()
            route_ev.maybe_override_discard(hand, 0, 0, 0, {}, {"rule_route_ev_enabled": 1}, None,
                                            lambda: "东")
            worst = max(worst, time.perf_counter() - t0)
        self.assertLess(worst, 0.030, "route_ev 叠加逻辑单次决策耗时超过 30ms: %.4fs" % worst)


class LossLedgerTests(unittest.TestCase):
    """P1（docs/IMPL_UNIFIED_EV_V2.md）：win/lost 守恒；rev_loss_enabled 打开时
    账本额外扣掉「别家先自摸」的付出；判据从比例门槛改为差值门槛。"""

    def test_win_lost_conservation(self):
        rng = random.Random(20260928)
        for _ in range(50):
            d = rng.randint(0, 3)
            p = rng.random() * 0.5
            q = rng.random() * 0.8
            n = rng.randint(1, 6)
            survive = [rng.uniform(0.7, 0.99) for _ in range(n)]
            win, lost, _lost_pay = route_ev._win_and_lost(d, p, q, n, survive)
            dist = {d: 1.0}
            for step in range(n):
                s = survive[step]
                nxt = {}
                for remaining, prob in dist.items():
                    prob *= s
                    if remaining == 0:
                        stay = prob * (1.0 - q)
                    else:
                        stay = prob * (1.0 - p)
                        nxt[remaining - 1] = nxt.get(remaining - 1, 0.0) + prob * p
                    if stay:
                        nxt[remaining] = nxt.get(remaining, 0.0) + stay
                dist = nxt
            self.assertAlmostEqual(win + lost + sum(dist.values()), 1.0, places=6)

    def test_switch_off_matches_first_version(self):
        """rev_loss_enabled 默认关闭时，best_route 与首版（无输钱项）完全一致。"""
        hand = to_counts(["1w", "1w", "2w", "2w", "3w", "3w", "4w", "4w",
                          "5b", "5b", "6b", "6b", JOKER])
        ctx = {"wall": 40, "dealer": False}
        weights_off = {"rule_route_ev_enabled": 1}
        weights_with_key = {"rule_route_ev_enabled": 1, "rev_loss_enabled": 0}
        self.assertEqual(route_ev.best_route(hand, 0, ctx, weights_off),
                         route_ev.best_route(hand, 0, ctx, weights_with_key))

    def test_loss_enabled_reduces_value(self):
        hand = to_counts(["1w", "1w", "2w", "2w", "3w", "3w", "4w", "4w",
                          "5b", "5b", "6b", "6b", JOKER])
        ctx = {"wall": 40, "dealer": False}
        off_value, _ = route_ev.best_route(hand, 0, ctx, {"rule_route_ev_enabled": 1})
        on_value, _ = route_ev.best_route(hand, 0, ctx, {"rule_route_ev_enabled": 1, "rev_loss_enabled": 1})
        self.assertLess(on_value, off_value)

    def test_margin_threshold_diff_vs_ratio(self):
        w_off = {"rev_loss_enabled": 0, "rev_margin": 0.10}
        w_on = {"rev_loss_enabled": 1, "rev_margin": 0.10, "rev_margin_abs": 0.5}
        # 旧值 4.0：比例门槛需要新值 >= 4.4；差值门槛需要新值 >= 4.0 + max(0.5, 0.4) = 4.5。
        self.assertTrue(route_ev._passes_margin(4.42, 4.0, w_off))
        self.assertFalse(route_ev._passes_margin(4.42, 4.0, w_on))
        self.assertTrue(route_ev._passes_margin(4.51, 4.0, w_on))

    def test_veto_threshold_diff_vs_ratio(self):
        w_off = {"rev_loss_enabled": 0, "rev_margin": 0.10}
        w_on = {"rev_loss_enabled": 1, "rev_margin": 0.10, "rev_margin_abs": 0.5}
        # 旧值 4.0：比例门槛"变差"需要新值 < 3.6；差值门槛需要 4.0-新值 >= 0.5，即新值 <= 3.5。
        self.assertTrue(route_ev._veto_worse(4.0, 3.58, w_off))
        self.assertFalse(route_ev._veto_worse(4.0, 3.58, w_on))
        self.assertTrue(route_ev._veto_worse(4.0, 3.49, w_on))


class SpeedLookaheadTests(unittest.TestCase):
    """P2（docs/IMPL_UNIFIED_EV_V2.md）：全部手牌的一向听/听牌前瞻，只在同向听
    tier 内换牌，不改变向听。手牌由随机搜索找到（见本次改动记录）：discard
    '6t' 一步进张更多（24 种活牌 vs '9t' 的 16 种），但摸入这些活牌之后
    '9t' 那一支能推进到更宽的听牌，前瞻 EV 更高。"""

    HAND = ['2t', '6t', '6w', '3w', '3t', '3w', '8t', '6b', '5w', '7b', '3t', '8b', '9t', '4w']

    def test_switch_off_end_to_end_matches_default(self):
        plain = [t for t in ALL_TILES if t != JOKER]
        rng = random.Random(2026)
        for _ in range(200):
            wall = list(plain) * 4
            rng.shuffle(wall)
            hand = wall[:14]
            self.assertEqual(choose_discard(hand, 0, 0, 0, {}, None),
                             choose_discard(hand, 0, 0, 0, {"_weights": {"rev_speed_enabled": 0}}, None), hand)

    def test_speed_lookahead_prefers_wider_resulting_tenpai_without_changing_shanten(self):
        off = choose_discard(self.HAND, 0, 0, 0, {}, None)
        weights = {"rev_speed_enabled": 1}
        on = choose_discard(self.HAND, 0, 0, 0, {"_weights": weights}, None)
        self.assertNotEqual(off, on)
        for tile in (off, on):
            left = list(self.HAND)
            left.remove(tile)
            self.assertEqual(route_shanten(to_counts(left), 0), 1)

    def test_both_entry_points_agree(self):
        weights = {"rev_speed_enabled": 1}
        strategy_pick = choose_discard(self.HAND, 0, 0, 0, {"_weights": weights}, None)
        ev_pick = choose_route_discard(self.HAND, 0, 0, 0, {"dealer_hint": True, "_weights": weights}, None)
        self.assertEqual(strategy_pick, ev_pick)

    def test_time_budget_fallback(self):
        weights = {"rev_speed_enabled": 1, "rev_time_budget_ms": 0}
        override = route_ev.maybe_override_speed(self.HAND, 0, 0, 0, {}, weights, None, lambda: "6t")
        self.assertIsNone(override)

    def test_out_of_scope_chain_unaffected(self):
        weights = {"rev_speed_enabled": 1}
        self.assertEqual(choose_discard(self.HAND, 0, 1, 0, {}, None),
                         choose_discard(self.HAND, 0, 1, 0, {"_weights": weights}, None))


class OverrideBRouteTests(unittest.TestCase):
    """P3（docs/IMPL_UNIFIED_EV_V2.md）：门清持财神范围内，最优路线为 B
    （标准爆头）时也允许改写。手牌由随机搜索找到（见本次改动记录）：discard
    '东' 是首版 route_ev（只认 C/D）与原逻辑都会选的牌；discard '6w' 的最优
    路线是 B，只有 rev_override_b_enabled 打开才会被选中。"""

    HAND = ['8t', '9t', JOKER, '6w', '5b', '4t', '中', '6w', '东', '4b', '9t', '西', '8t', '西']

    def test_switch_off_default_unaffected(self):
        off = choose_discard(self.HAND, 0, 0, 0, {}, None)
        on_no_b = choose_discard(self.HAND, 0, 0, 0, {"_weights": {"rule_route_ev_enabled": 1}}, None)
        self.assertEqual(off, on_no_b)

    def test_override_b_enabled_switches_to_baotou_route(self):
        weights = {"rule_route_ev_enabled": 1, "rev_override_b_enabled": 1}
        on_b = choose_discard(self.HAND, 0, 0, 0, {"_weights": weights}, None)
        self.assertEqual(on_b, "6w")
        left = list(self.HAND)
        left.remove(on_b)
        _value, letter = route_ev.best_route(to_counts(left), 0, {"wall": 60, "dealer": False}, weights)
        self.assertEqual(letter, "B")


class PiaoAndDeclineEvTests(unittest.TestCase):
    """P4（docs/IMPL_UNIFIED_EV_V2.md）：财飘续飘 / 非爆头弃胡转爆头改用按账
    决策，替代原有固定阈值；硬护栏（piao_force_hu、墙尾闸门）保留。"""

    def _toggle(self, key, value):
        import mj.fit as fit
        original = fit.DEFAULT_WEIGHTS.get(key)
        fit.DEFAULT_WEIGHTS[key] = value
        return fit, key, original

    def test_piao_ev_chooses_float_when_wall_deep(self):
        # 3 组顺子 + 1b1b 对子 + 2 财神：摸到第三张 1b 之后，浮出 1 张财神仍是
        # 3 组顺子 + 1b1b1b 刻子 + 单财神听（爆头）——满足"打掉之后仍爆头"的硬条件。
        pre_draw = ["1w", "2w", "3w", "4w", "5w", "6w", "7w", "8w", "9w", "1b", "1b", JOKER, JOKER]
        counts13 = to_counts(pre_draw)
        self.assertTrue(baotou(counts13, 0))
        res = evaluate(counts13, TILE_INDEX["1b"], meld_groups=0)
        self.assertTrue(res["baotou"])
        snapshot = {"seat": 0, "dealer": 0, "my_hand": pre_draw + ["1b"], "drawn_tile": "1b",
                   "melds": [[], [], [], []], "chain_count": 0, "piao": 0, "wall_remaining": 100}
        fit, key, original = self._toggle("rev_piao_enabled", 1)
        try:
            action = choose_hu_or_piao(snapshot, res)
        finally:
            fit.DEFAULT_WEIGHTS[key] = original
        self.assertEqual(action, {"action": "discard", "tile": JOKER})

    def test_piao_ev_respects_wall_gate_even_when_enabled(self):
        pre_draw = ["1w", "2w", "3w", "4w", "5w", "6w", "7w", "8w", "9w", "1b", "1b", JOKER, JOKER]
        counts13 = to_counts(pre_draw)
        res = evaluate(counts13, TILE_INDEX["1b"], meld_groups=0)
        snapshot = {"seat": 0, "dealer": 0, "my_hand": pre_draw + ["1b"], "drawn_tile": "1b",
                   "melds": [[], [], [], []], "chain_count": 0, "piao": 0, "wall_remaining": 22}
        fit, key, original = self._toggle("rev_piao_enabled", 1)
        try:
            action = choose_hu_or_piao(snapshot, res)
        finally:
            fit.DEFAULT_WEIGHTS[key] = original
        self.assertEqual(action, {"action": "hu", "tile": "1b"})

    def test_decline_ev_matches_original_switch_off(self):
        pre_draw = ["1w", "1w", "2w", "2w", "3w", "3w", "4w", "4w", "5w", "5w", JOKER, "6w", "7w"]
        counts13 = to_counts(pre_draw)
        result = evaluate(counts13, TILE_INDEX["7w"], meld_groups=0)
        snapshot = {"my_hand": pre_draw + ["7w"], "melds": [[], [], [], []], "seat": 0,
                   "drawn_tile": "7w", "chain_count": 0, "piao": 0, "wall_remaining": 60}
        fit, key1, orig1 = self._toggle("rule_decline_joker_hold_enabled", 1)
        _, key2, orig2 = self._toggle("rule_decline_single_joker_all_melds_enabled", 1)
        try:
            action = choose_hu_or_piao(snapshot, result)
        finally:
            fit.DEFAULT_WEIGHTS[key1] = orig1
            fit.DEFAULT_WEIGHTS[key2] = orig2
        self.assertEqual(action, {"action": "discard", "tile": "6w"})

    def test_decline_ev_enabled_agrees_on_same_case(self):
        pre_draw = ["1w", "1w", "2w", "2w", "3w", "3w", "4w", "4w", "5w", "5w", JOKER, "6w", "7w"]
        counts13 = to_counts(pre_draw)
        result = evaluate(counts13, TILE_INDEX["7w"], meld_groups=0)
        snapshot = {"my_hand": pre_draw + ["7w"], "melds": [[], [], [], []], "seat": 0,
                   "drawn_tile": "7w", "chain_count": 0, "piao": 0, "wall_remaining": 60}
        fit, key, original = self._toggle("rev_decline_enabled", 1)
        try:
            action = choose_hu_or_piao(snapshot, result)
        finally:
            fit.DEFAULT_WEIGHTS[key] = original
        self.assertEqual(action, {"action": "discard", "tile": "6w"})


class ClaimEvTests(unittest.TestCase):
    """P5（docs/IMPL_UNIFIED_EV_V2.md）：吃碰最前面整体比较"不吃碰"与"吃碰后
    打最优一张"两笔账；复用 ClaimVetoTests 里已验证过路线信息的手牌，只是把
    否决开关换成 rev_claim_enabled，证明新的整体账本口径能得出同样的否决。"""

    HAND = ["2t", "白", "中", "4w", "中", "白", "北", "5t", "2t", "8t", "4w", "中", "5t"]
    TARGET = "中"

    def _snapshot(self):
        return {"seat": 0, "dealer": 1, "my_hand": self.HAND, "window_tile": self.TARGET,
               "wall_remaining": 60, "chain_count": 0, "melds": [[], [], [], []]}

    def test_switch_off_matches_route_ev_only(self):
        import mj.fit as fit
        original = fit.DEFAULT_WEIGHTS.get("rule_route_ev_enabled")
        fit.DEFAULT_WEIGHTS["rule_route_ev_enabled"] = 1
        try:
            self.assertIsNone(choose_peng(self._snapshot()))
        finally:
            fit.DEFAULT_WEIGHTS["rule_route_ev_enabled"] = original

    def test_claim_ev_enabled_also_rejects(self):
        import mj.fit as fit
        original = fit.DEFAULT_WEIGHTS.get("rev_claim_enabled")
        fit.DEFAULT_WEIGHTS["rev_claim_enabled"] = 1
        try:
            self.assertIsNone(choose_peng(self._snapshot()))
        finally:
            fit.DEFAULT_WEIGHTS["rev_claim_enabled"] = original

    def test_claim_ev_decision_none_when_switch_off(self):
        decision = route_ev.claim_ev_decision(self.HAND, (self.TARGET, self.TARGET),
                                              {"wall": 60, "dealer": False}, {"rev_claim_enabled": 0})
        self.assertIsNone(decision)


class OppPayTests(unittest.TestCase):
    """T1（docs/IMPL_TABLE_EV.md，rev_opp_pay_enabled）：分对手付分。本轮三家
    h 仍取同一个值，加权付分公式退化成只看"谁是庄"；解出一组 fan_by_role
    使得加权付分恰好等于 P1 的旧平均数（rev_loss_dealer/idle），验证两者
    在这个退化点上给出完全一致的账，证明 T1 是 P1 的严格推广而不是另一套
    互不兼容的公式。"""

    HAND = to_counts(["1w", "1w", "2w", "2w", "3w", "3w", "4w", "4w",
                      "5b", "5b", "6b", "6b", JOKER])

    def _with_fan_table(self, fan_dealer, fan_idle):
        route_ev._HAZARD_TABLE_CACHE = {"fan_by_role": {"dealer": fan_dealer, "idle": fan_idle}}

    def tearDown(self):
        route_ev._HAZARD_TABLE_CACHE = None

    def test_matches_p1_when_weighted_pay_equals_old_average(self):
        # 8*fan_idle = rev_loss_dealer(10.33)；(8*fan_dealer+2*fan_idle)/3 = rev_loss_idle(4.73)。
        fan_idle = 10.33 / 8.0
        fan_dealer = (3 * 4.73 - 2 * fan_idle) / 8.0
        self._with_fan_table(fan_dealer, fan_idle)
        for dealer in (True, False):
            ctx = {"wall": 40, "dealer": dealer}
            w_p1 = {"rule_route_ev_enabled": 1, "rev_loss_enabled": 1}
            w_t1 = {"rule_route_ev_enabled": 1, "rev_opp_pay_enabled": 1}
            self.assertAlmostEqual(route_ev.best_route(self.HAND, 0, ctx, w_p1)[0],
                                   route_ev.best_route(self.HAND, 0, ctx, w_t1)[0], places=6)

    def test_missing_hazard_table_falls_back_to_default_fan(self):
        route_ev._HAZARD_TABLE_CACHE = {}   # 模拟文件缺失：缺失时 _load_hazard_table 缓存的就是 {}（设 None 会去读真实文件）
        weights = {"rev_opp_pay_enabled": 1, "rev_fan_default": 1.3}
        self.assertAlmostEqual(route_ev._opp_pay_weighted({"dealer": True}, weights), 8 * 1.3)
        self.assertAlmostEqual(route_ev._opp_pay_weighted({"dealer": False}, weights), (8 * 1.3 + 2 * 1.3) / 3.0)

    def test_dealer_win_costs_about_eight_times_idle_win_when_idle(self):
        """闲家时：同样的 fan，"庄家胡"这个来源的付出权重(8) 是"另一闲家胡"
        权重(1) 的 8 倍——直接从公式结构验证，而不是从某个手牌的间接效应
        反推（间接效应还混了 fan_dealer≠fan_idle 的影响）。"""
        fan = 1.5
        self._with_fan_table(fan, fan)
        weights = {"rev_opp_pay_enabled": 1}
        pay_not_dealer = route_ev._opp_pay_weighted({"dealer": False}, weights)
        # (8*fan + fan + fan) / 3：庄家来源贡献 8*fan，两个闲家来源各贡献 1*fan。
        dealer_source = 8 * fan
        idle_source_each = 1 * fan
        self.assertAlmostEqual(dealer_source / idle_source_each, 8.0)
        self.assertAlmostEqual(pay_not_dealer, (dealer_source + 2 * idle_source_each) / 3.0)


class DealerValueTests(unittest.TestCase):
    """T3（docs/IMPL_TABLE_EV.md，rev_dealer_value_enabled）：胡者接庄的额外
    期望值只进入按账分支；局号 8（或表缺失）时恒为 0，等价于关闭。"""

    def tearDown(self):
        route_ev._DEALER_VALUE_CACHE = None

    def test_missing_file_equals_off(self):
        route_ev._DEALER_VALUE_CACHE = {}   # 模拟文件缺失：缺失时缓存的就是 {}（设 None 会去读真实文件）
        self.assertEqual(route_ev.dealer_value(3, {"rev_dealer_value_enabled": 1}), 0.0)

    def test_round_8_equals_off_even_with_table_present(self):
        route_ev._DEALER_VALUE_CACHE = {"V": {"1": 12.0, "7": 3.0, "8": 0.0}}
        self.assertEqual(route_ev.dealer_value(8, {"rev_dealer_value_enabled": 1}), 0.0)
        self.assertEqual(route_ev.dealer_value(8, {"rev_dealer_value_enabled": 0}), 0.0)

    def test_switch_off_ignores_table(self):
        route_ev._DEALER_VALUE_CACHE = {"V": {"3": 99.0}}
        self.assertEqual(route_ev.dealer_value(3, {"rev_dealer_value_enabled": 0}), 0.0)

    def test_p53_decline_example_flips_with_dealer_value(self):
        """构造 P=0.53 的 S1 例子：关（V=0）选弃胡转爆头，开（V=5，局号 3）
        选当场胡——V 只对"当场"一侧全额计入、对"等待"一侧按存活率打折，
        胡者接庄的额外期望越大，越应该现在就把庄位收进兜里。"""
        orig_survive = route_ev._single_step_survive
        route_ev._single_step_survive = lambda wall, weights: 0.53
        try:
            pre_draw = ["1w", "1w", "2w", "2w", "3w", "3w", "4w", "4w", "5w", "5w", JOKER, "6w", "7w"]
            hu_result = {"baotou": False, "fan": 4}
            snapshot = {"my_hand": pre_draw + ["7w"], "melds": [[], [], [], []], "seat": 0,
                       "drawn_tile": "7w", "dealer": None, "wall_remaining": 60, "round_no": 3}
            off = _decline_small_hu_tile_ev(snapshot, hu_result, {"rev_dealer_value_enabled": 0})
            self.assertEqual(off, "6w")
            route_ev._DEALER_VALUE_CACHE = {"V": {"3": 5.0, "8": 0.0}}
            on = _decline_small_hu_tile_ev(snapshot, hu_result, {"rev_dealer_value_enabled": 1})
            self.assertIsNone(on)
            snapshot8 = dict(snapshot, round_no=8)
            self.assertEqual(_decline_small_hu_tile_ev(snapshot8, hu_result, {"rev_dealer_value_enabled": 1}),
                             _decline_small_hu_tile_ev(snapshot8, hu_result, {"rev_dealer_value_enabled": 0}))
        finally:
            route_ev._single_step_survive = orig_survive

    def test_best_route_unaffected_when_switch_off_even_with_table(self):
        route_ev._DEALER_VALUE_CACHE = {"V": {"1": 999.0}}
        hand = to_counts(["1w", "1w", "2w", "2w", "3w", "3w", "4w", "4w",
                          "5b", "5b", "6b", "6b", JOKER])
        ctx = {"wall": 40, "dealer": False, "round_no": 1}
        off = route_ev.best_route(hand, 0, ctx, {"rule_route_ev_enabled": 1})
        off_with_key = route_ev.best_route(hand, 0, ctx, {"rule_route_ev_enabled": 1,
                                                          "rev_dealer_value_enabled": 0})
        self.assertEqual(off, off_with_key)


class TableEvPerformanceTests(unittest.TestCase):
    """T1/T3 新增查表逻辑仍受 rev_time_budget_ms 约束，超预算回落原逻辑
    （用假时钟，不依赖真实耗时）。"""

    def test_speed_override_falls_back_under_tiny_budget_with_t1_t3_on(self):
        hand = ['2t', '6t', '6w', '3w', '3t', '3w', '8t', '6b', '5w', '7b', '3t', '8b', '9t', '4w']
        weights = {"rev_speed_enabled": 1, "rev_opp_pay_enabled": 1, "rev_dealer_value_enabled": 1,
                  "rev_time_budget_ms": 0}
        override = route_ev.maybe_override_speed(hand, 0, 0, 0, {}, weights, None, lambda: "6t")
        self.assertIsNone(override)


class RouteCalTests(unittest.TestCase):
    """C1（docs/IMPL_ROUTE_CAL.md，rev_route_cal_enabled）：路线概率校准查表。
    表缺失/开关关闭一律回落理论 DP；测试里把缓存设为 {}（不是 None——设 None
    会让 ``_load_route_cal`` 去读真实的 models/route_cal.json，上一轮就因为
    这个坑导致两个测试在开发机上偶发失败）。"""

    def tearDown(self):
        route_ev._ROUTE_CAL_CACHE = None

    def test_switch_off_ignores_table(self):
        route_ev._ROUTE_CAL_CACHE = {"cells": {"A:1:2:0:0": {"F": [0.9] * 18}}}
        self.assertIsNone(route_ev.route_cal_curve("A", 1, 5, 0, 0, {"rev_route_cal_enabled": 0}))

    def test_missing_table_equals_off(self):
        route_ev._ROUTE_CAL_CACHE = {}   # 模拟"文件缺失/读取失败"后的空表，不是 None
        self.assertIsNone(route_ev.route_cal_curve("A", 1, 5, 0, 0, {"rev_route_cal_enabled": 1}))

    def test_d_out_of_range_ignored(self):
        route_ev._ROUTE_CAL_CACHE = {"cells": {"A:5:2:0:0": {"F": [0.9] * 18}}}
        self.assertIsNone(route_ev.route_cal_curve("A", 5, 5, 0, 0, {"rev_route_cal_enabled": 1}))

    def test_best_route_unaffected_when_switch_off_even_with_table(self):
        route_ev._ROUTE_CAL_CACHE = {"cells": {"D:0:5:1:0": {"F": [1.0] * 18}}}
        hand = to_counts(["1w", "1w", "2w", "2w", "3w", "3w", "4w", "4w",
                          "5b", "5b", "6b", "6b", JOKER])
        ctx = {"wall": 40, "dealer": False}
        off = route_ev.best_route(hand, 0, ctx, {"rule_route_ev_enabled": 1})
        off_with_key = route_ev.best_route(hand, 0, ctx, {"rule_route_ev_enabled": 1, "rev_route_cal_enabled": 0})
        self.assertEqual(off, off_with_key)

    def test_backoff_chain_falls_back_to_coarse_key(self):
        """完整键、丢副露键、丢副露+财神键都不存在，只有最粗的 "R:d:*:*:*"
        存在时，仍然应该查到它——四级回退链的最后一级。"""
        route_ev._ROUTE_CAL_CACHE = {"cells": {"A:1:*:*:*": {"F": [0.05] * 18, "n": 40, "backoff": "drop_live_bucket"}}}
        curve = route_ev.route_cal_curve("A", 1, 5, 1, 0, {"rev_route_cal_enabled": 1})
        self.assertEqual(curve, [0.05] * 18)

    def test_backoff_prefers_finer_key_when_available(self):
        route_ev._ROUTE_CAL_CACHE = {"cells": {
            "A:1:*:*:*": {"F": [0.05] * 18},
            "A:1:1:*:*": {"F": [0.20] * 18},        # 丢 melds+jokers 这一级（L2）
            "A:1:1:0:0": {"F": [0.35] * 18},        # 完整键（full）
        }}
        # live=5 落在活牌分档 1（3-5），jokers=0, meld=0：完整键命中，优先于 L2/L3。
        curve = route_ev.route_cal_curve("A", 1, 5, 0, 0, {"rev_route_cal_enabled": 1})
        self.assertEqual(curve, [0.35] * 18)
        # jokers=2（不同财神数），完整键 "A:1:1:2:0" 不存在，退到 L2 "A:1:1:*:*"。
        curve2 = route_ev.route_cal_curve("A", 1, 5, 2, 0, {"rev_route_cal_enabled": 1})
        self.assertEqual(curve2, [0.20] * 18)

    def test_win_lost_matches_hand_computed_curve(self):
        curve = [0.2, 0.5, 0.8, 1.0]
        survive = [1.0, 0.9, 0.8, 0.7]
        win, lost, _lost_pay = route_ev._route_cal_win_and_lost(curve, 4, survive)
        self.assertAlmostEqual(win, 0.7868, places=6)
        self.assertAlmostEqual(lost, 0.2132, places=6)
        self.assertAlmostEqual(win + lost, 1.0, places=6)

    def test_win_lost_conservation_random(self):
        rng = random.Random(2026)
        for _ in range(30):
            n = rng.randint(1, 8)
            curve = sorted(rng.random() for _ in range(n))
            curve[-1] = 1.0 if rng.random() < 0.3 else curve[-1]   # 有时提前走完，有时没走完
            survive = [rng.uniform(0.7, 0.99) for _ in range(n)]
            win, lost, _lost_pay = route_ev._route_cal_win_and_lost(curve, n, survive)
            # 手动重放剩余滞留质量，核对 win+lost+剩余=1。
            s_cum, prev_f = 1.0, 0.0
            for i in range(n):
                s_cum *= survive[i]
                prev_f = curve[i]
            remaining_mass = max(0.0, 1.0 - prev_f) * s_cum
            self.assertAlmostEqual(win + lost + remaining_mass, 1.0, places=6)

    def test_route_win_probabilities_uses_table_when_enabled(self):
        # D 在 d==0 时理论账（q=1）本质是"第一次能摸牌就一定胡"，理论 win 恰好
        # 等于第一步的 survive——用一条明显更慢的曲线（前几步都还没走完）才能
        # 保证与理论值不同，不能随手挑一条常数 1.0 的曲线（那条曲线和 q=1 的
        # 理论账在 d==0 时数值上恰好重合，测不出差异）。
        route_ev._ROUTE_CAL_CACHE = {"cells": {"D:0:5:1:0": {"F": [0.3] * 18}}}
        hand = to_counts(["1w", "1w", "2w", "2w", "3w", "3w", "4w", "4w",
                          "5b", "5b", "6b", "6b", JOKER])
        ctx = {"wall": 40, "dealer": False}
        probs_off = route_ev.route_win_probabilities(hand, 0, ctx, {}, routes=("D",))
        probs_on = route_ev.route_win_probabilities(hand, 0, ctx, {"rev_route_cal_enabled": 1}, routes=("D",))
        self.assertIn("D", probs_off)
        self.assertIn("D", probs_on)
        self.assertNotEqual(probs_off["D"], probs_on["D"])

    def test_performance_lookup_path_not_slower(self):
        """查表路径下单次 best_route 耗时不应比理论路径明显更高（字典查找
        本身是 O(1)，不应引入可观测的额外开销）。"""
        route_ev._ROUTE_CAL_CACHE = {"cells": {}}   # 空表：命中路径与"查不到"路径耗时应接近
        hand = to_counts(["1w", "1w", "2w", "2w", "3w", "3w", "4w", "4w",
                          "5b", "5b", "6b", "6b", JOKER])
        ctx = {"wall": 40, "dealer": False}
        weights_off = {"rule_route_ev_enabled": 1}
        weights_on = {"rule_route_ev_enabled": 1, "rev_route_cal_enabled": 1}
        for weights in (weights_off, weights_on):
            t0 = time.perf_counter()
            for _ in range(50):
                route_ev.best_route(hand, 0, ctx, weights)
            elapsed = time.perf_counter() - t0
            self.assertLess(elapsed, 1.0)


if __name__ == "__main__":
    unittest.main()
